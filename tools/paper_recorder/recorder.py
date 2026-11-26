#!/usr/bin/env python3
"""Standalone PAPER-ONLY recorder for the Polymarket BTC 5m up/down latency-arb idea.

What it does
  * Finds the current and next BTC 5m window on Gamma (read-only, public).
  * Streams the Polymarket CLOB market websocket (public, no auth) and keeps a
    local order book (bid and ask ladders) per outcome token, same wire format
    the #195 live book feed uses (book snapshot + price_change deltas).
  * Streams BTC trades from Binance (public) and builds 1m closes for realized sigma.
  * Every second it writes a SNAPSHOT row (spot, sigma, model probability, both
    books' top levels). Snapshots let you re-simulate any filter or latency offline.
  * It also runs ONE default paper strategy: when the net edge on the ask >= --threshold
    it "fills" at ts + --latency seconds by walking the REAL ask ladder of that moment.
    Fee is the market's own fee schedule (rate * p * (1 - p) per share, taker).
  * After each window it records the resolved outcome from Gamma.

What it can NOT do (by construction)
  * No wallet, no private key, no API key, no order endpoint. It imports no
    trading code and never sends anything but public read requests and websocket
    subscriptions. Output is local JSONL files only.

Needs: python 3.10+, `pip install websockets`. Nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import sys
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field

import websockets

GAMMA = "https://gamma-api.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
SPOT_URLS = [
    "wss://stream.binance.com:9443/ws/btcusdt@trade",
    "wss://data-stream.binance.vision/ws/btcusdt@trade",
]
KLINES = "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=40"
WINDOW_S = 300
DEPTH = 10
MIN_SIGMA = 1e-6
DEFAULT_SIGMA = 0.002


# ---------------------------------------------------------------- model (same math as latency_arb_service)
def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def realized_sigma(closes: list[float], window_minutes: int = 5) -> float:
    rets = [math.log(b / a) for a, b in zip(closes[:-1], closes[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return DEFAULT_SIGMA
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return max(math.sqrt(var) * math.sqrt(window_minutes), MIN_SIGMA)


def p_up(distance: float, sigma: float, t_remaining: float) -> float:
    if t_remaining <= 0:
        return 1.0 if distance > 0 else 0.0 if distance < 0 else 0.5
    return normal_cdf(distance / (sigma * math.sqrt(t_remaining)))


# ---------------------------------------------------------------- order book
@dataclass
class Book:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    has_snapshot: bool = False
    last_update: float = 0.0

    def apply_snapshot(self, ev: dict) -> None:
        self.bids = {float(x["price"]): float(x["size"]) for x in ev.get("bids", []) if float(x["size"]) > 0}
        self.asks = {float(x["price"]): float(x["size"]) for x in ev.get("asks", []) if float(x["size"]) > 0}
        self.has_snapshot = True
        self.last_update = time.time()

    def apply_change(self, ch: dict) -> None:
        side = self.bids if str(ch.get("side", "")).upper() == "BUY" else self.asks
        price, size = float(ch["price"]), float(ch["size"])
        if size <= 0:
            side.pop(price, None)
        else:
            side[price] = size
        self.last_update = time.time()

    def top(self, n: int = DEPTH):
        bids = sorted(self.bids.items(), reverse=True)[:n]
        asks = sorted(self.asks.items())[:n]
        return bids, asks

    def crossed(self) -> bool:
        return bool(self.bids and self.asks and max(self.bids) >= min(self.asks))


def walk_asks(asks: list[tuple[float, float]], shares: float, fee_rate: float):
    """Buy `shares` by walking the ask ladder. Returns (avg_price, fee_total, filled) or None
    if the visible ladder cannot fill it (a real order would be rejected or partial)."""
    need, cost, fee = shares, 0.0, 0.0
    for price, size in asks:
        take = min(need, size)
        cost += take * price
        fee += take * fee_rate * price * (1.0 - price)
        need -= take
        if need <= 1e-9:
            break
    if need > 1e-9:
        return None
    return cost / shares, fee, shares


# ---------------------------------------------------------------- state
@dataclass
class Market:
    start: int
    cond: str
    up: str
    down: str
    fee_rate: float
    open_price: float | None = None
    traded: bool = False
    resolved: bool = False
    skipped: bool = False


class State:
    def __init__(self, a):
        self.a = a
        self.books: dict[str, Book] = {}
        self.markets: dict[int, Market] = {}
        self.spot: float | None = None
        self.spot_ts: float = 0.0
        self.minute_closes: deque[tuple[int, float]] = deque(maxlen=60)
        self.pending: list[dict] = []  # trade signals waiting out the latency
        self.sub_queue: asyncio.Queue = asyncio.Queue()
        self.stop = asyncio.Event()
        os.makedirs(a.out, exist_ok=True)
        self.f_snap = open(os.path.join(a.out, "snapshots.jsonl"), "a", buffering=1)
        self.f_trade = open(os.path.join(a.out, "trades.jsonl"), "a", buffering=1)
        self.f_out = open(os.path.join(a.out, "outcomes.jsonl"), "a", buffering=1)
        self.f_log = open(os.path.join(a.out, "recorder.log"), "a", buffering=1)

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        self.f_log.write(line + "\n")

    def sigma(self) -> float:
        closes = [c for _, c in self.minute_closes]
        return realized_sigma(closes, 5)


def http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-recorder/1"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


async def aget(url: str):
    return await asyncio.get_running_loop().run_in_executor(None, http_json, url)


# ---------------------------------------------------------------- tasks
async def spot_task(st: State):
    # seed 1m closes from REST history so sigma is usable immediately
    try:
        for k in (await aget(KLINES))[:-1]:
            st.minute_closes.append((int(k[0]) // 60000, float(k[4])))
        st.log(f"seeded {len(st.minute_closes)} 1m closes")
    except Exception as e:
        st.log(f"kline seed failed ({e!r}); sigma uses default until 3 minutes pass")
    i = 0
    while not st.stop.is_set():
        url = SPOT_URLS[i % len(SPOT_URLS)]
        try:
            async with websockets.connect(url, open_timeout=10, ping_interval=20) as ws:
                st.log(f"spot connected {url}")
                async for raw in ws:
                    d = json.loads(raw)
                    p, ts = float(d["p"]), d["T"] / 1000.0
                    st.spot, st.spot_ts = p, ts
                    minute = int(ts // 60)
                    if st.minute_closes and st.minute_closes[-1][0] == minute:
                        st.minute_closes[-1] = (minute, p)
                    else:
                        st.minute_closes.append((minute, p))
                    for m in st.markets.values():  # first trade at/after window start = open price
                        # open price = first trade within 2s after the window start. A window the
                        # recorder joined mid-way has no valid open and is skipped (never guessed).
                        if m.open_price is None and not m.skipped and ts >= m.start:
                            if ts - m.start <= 2.0:
                                m.open_price = p
                            else:
                                m.skipped = True
                                st.log(f"window {m.start} joined mid-way, skipped")
        except Exception as e:
            st.log(f"spot feed error {e!r}; switching source")
            i += 1
            await asyncio.sleep(1)


async def book_task(st: State):
    subscribed: set[str] = set()
    while not st.stop.is_set():
        try:
            async with websockets.connect(CLOB_WS, open_timeout=10, ping_interval=None) as ws:
                st.log("clob ws connected")
                for b in st.books.values():
                    b.has_snapshot = False
                subscribed.clear()
                first = True

                async def pinger():
                    while True:
                        await asyncio.sleep(10)
                        await ws.send("PING")

                async def subscriber():
                    nonlocal first
                    while True:
                        tokens = await st.sub_queue.get()
                        new = [t for t in tokens if t not in subscribed]
                        if not new:
                            continue
                        if first:
                            await ws.send(json.dumps({"assets_ids": new, "type": "market"}))
                            first = False
                        else:
                            await ws.send(json.dumps({"assets_ids": new, "operation": "subscribe"}))
                        subscribed.update(new)

                # re-queue everything we know after a reconnect
                known = [t for m in st.markets.values() for t in (m.up, m.down)]
                if known:
                    st.sub_queue.put_nowait(known)
                tasks = [asyncio.create_task(pinger()), asyncio.create_task(subscriber())]
                try:
                    async for raw in ws:
                        if raw == "PONG":
                            continue
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        for ev in data if isinstance(data, list) else [data]:
                            et = ev.get("event_type")
                            if et == "book":
                                st.books.setdefault(ev["asset_id"], Book()).apply_snapshot(ev)
                            elif et == "price_change":
                                for ch in ev.get("price_changes", []):
                                    b = st.books.get(ch.get("asset_id"))
                                    if b and b.has_snapshot:
                                        b.apply_change(ch)
                finally:
                    for t in tasks:
                        t.cancel()
        except Exception as e:
            st.log(f"clob ws error {e!r}; reconnect in 2s")
            await asyncio.sleep(2)


async def discover_task(st: State):
    while not st.stop.is_set():
        now = int(time.time())
        cur = now // WINDOW_S * WINDOW_S
        for start in (cur, cur + WINDOW_S):
            if start in st.markets:
                continue
            try:
                ev = await aget(f"{GAMMA}/events?slug=btc-updown-5m-{start}")
                if not ev:
                    continue
                m = ev[0]["markets"][0]
                toks = json.loads(m["clobTokenIds"])
                outs = json.loads(m["outcomes"])
                up, down = (toks[0], toks[1]) if outs[0].lower() == "up" else (toks[1], toks[0])
                fs = m.get("feeSchedule") or {}
                fee = float(fs.get("rate", 0.05)) if m.get("feesEnabled", True) else 0.0
                st.markets[start] = Market(start, m["conditionId"], up, down, fee)
                st.sub_queue.put_nowait([up, down])
                st.log(f"market {start} ready fee_rate={fee}")
            except Exception as e:
                st.log(f"discover {start} failed {e!r}")
        await asyncio.sleep(5)


def levels(book: Book):
    b, a = book.top()
    return [[p, s] for p, s in b], [[p, s] for p, s in a]


async def snapshot_task(st: State):
    a = st.a
    while not st.stop.is_set():
        await asyncio.sleep(max(0.0, 1.0 - (time.time() % 1.0)))
        now = time.time()
        if st.spot is None:
            continue
        # settle latency-delayed paper fills first
        due = [p for p in st.pending if now >= p["fill_at"]]
        st.pending = [p for p in st.pending if now < p["fill_at"]]
        for p in due:
            fill_paper(st, p, now)
        for start, m in list(st.markets.items()):
            if not (start <= now < start + WINDOW_S) or m.open_price is None:
                continue
            bu, bd = st.books.get(m.up), st.books.get(m.down)
            if not (bu and bd and bu.has_snapshot and bd.has_snapshot):
                continue
            fresh = (now - max(bu.last_update, bd.last_update)) <= a.max_age
            t_rem = (start + WINDOW_S - now) / WINDOW_S
            dist = (st.spot - m.open_price) / m.open_price
            sig = st.sigma()
            pm = p_up(dist, sig, t_rem)
            ub, ua = levels(bu)
            db, da = levels(bd)
            row = {"ts": round(now, 3), "win": start, "t_rem": round(t_rem, 4), "spot": st.spot,
                   "spot_age": round(now - st.spot_ts, 2), "open": m.open_price, "dist": dist, "sigma": sig,
                   "p_model_up": pm, "fee_rate": m.fee_rate, "fresh": fresh, "crossed": bu.crossed() or bd.crossed(),
                   "up_bids": ub, "up_asks": ua, "down_bids": db, "down_asks": da}
            st.f_snap.write(json.dumps(row) + "\n")
            # default strategy: at most one paper trade per window, only on fresh uncrossed books
            if m.traded or not fresh or row["crossed"] or t_rem < a.min_t_rem or not ua or not da:
                continue
            for side, asks, pmod in (("up", ua, pm), ("down", da, 1.0 - pm)):
                # price the signal on the ladder walked for our size, not on a thin top-of-book sliver
                w = walk_asks([tuple(x) for x in asks], a.shares, m.fee_rate)
                if w is None:
                    continue
                avg, fee_tot, _ = w
                net_edge = pmod - avg - fee_tot / a.shares
                if net_edge >= a.threshold:
                    m.traded = True
                    st.pending.append({"win": start, "side": side, "signal_ts": now, "fill_at": now + a.latency,
                                       "net_edge_at_signal": net_edge, "p_model": pmod, "ask_at_signal": asks[0][0],
                                       "avg_at_signal": avg, "t_rem": t_rem,
                                       "token": m.up if side == "up" else m.down, "fee_rate": m.fee_rate})
                    break


def fill_paper(st: State, p: dict, now: float) -> None:
    a = st.a
    b = st.books.get(p["token"])
    rec = dict(p, fill_ts=round(now, 3), shares=a.shares, latency=a.latency)
    asks = b.top()[1] if b and b.has_snapshot else []
    res = walk_asks(asks, a.shares, p["fee_rate"]) if asks else None
    if res is None:
        rec.update(status="no_fill", reason="ladder too thin or book missing")
    else:
        avg, fee, _ = res
        rec.update(status="filled", avg_price=avg, fee=fee, cost=avg * a.shares + fee,
                   levels_used=[[pp, ss] for pp, ss in asks[:5]])
    st.f_trade.write(json.dumps(rec) + "\n")
    st.log(f"PAPER {rec['status']} win={p['win']} {p['side']} edge={p['net_edge_at_signal']:.3f} "
           f"avg={rec.get('avg_price')}")


async def resolve_task(st: State):
    while not st.stop.is_set():
        await asyncio.sleep(20)
        now = time.time()
        for start, m in list(st.markets.items()):
            if m.resolved or now < start + WINDOW_S + 15:
                continue
            try:
                ev = await aget(f"{GAMMA}/events?slug=btc-updown-5m-{start}")
                mk = ev[0]["markets"][0]
                prices = [float(x) for x in json.loads(mk["outcomePrices"])]
                outs = json.loads(mk["outcomes"])
                if max(prices) >= 0.99:  # Gamma shows 1/0 only after settlement
                    winner = outs[prices.index(max(prices))].lower()
                    st.f_out.write(json.dumps({"win": start, "winner": winner, "src": "gamma",
                                               "resolved_at": round(now, 1)}) + "\n")
                    m.resolved = True
                    st.log(f"resolved {start} -> {winner}")
                elif now > start + WINDOW_S + 900:
                    m.resolved = True
                    st.f_out.write(json.dumps({"win": start, "winner": None, "src": "gamma_unresolved"}) + "\n")
            except Exception as e:
                st.log(f"resolve {start} failed {e!r}")
        for s in [s for s, m in st.markets.items() if m.resolved and time.time() > s + WINDOW_S + 1200]:
            st.markets.pop(s, None)


async def main():
    ap = argparse.ArgumentParser(description="Paper-only Polymarket BTC 5m recorder (no keys, no wallet, no orders)")
    ap.add_argument("--out", default="paper_data", help="output directory for JSONL files")
    ap.add_argument("--threshold", type=float, default=0.03, help="min NET edge (after fee) to paper-trade")
    ap.add_argument("--latency", type=float, default=1.0, help="seconds between signal and simulated fill")
    ap.add_argument("--shares", type=float, default=10.0, help="paper order size in shares (market minimum is 5)")
    ap.add_argument("--min-t-rem", type=float, default=0.05, help="skip the final fraction of the window")
    ap.add_argument("--max-age", type=float, default=3.0, help="book must have updated within this many seconds")
    ap.add_argument("--hours", type=float, default=24.0)
    a = ap.parse_args()
    st = State(a)
    st.log(f"PAPER ONLY. No keys, no wallet, no orders. out={a.out} hours={a.hours}")
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, st.stop.set)
        except NotImplementedError:  # windows
            pass
    tasks = [asyncio.create_task(f(st)) for f in (spot_task, book_task, discover_task, snapshot_task, resolve_task)]
    try:
        await asyncio.wait_for(st.stop.wait(), timeout=a.hours * 3600)
    except asyncio.TimeoutError:
        pass
    st.stop.set()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    st.log("stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
