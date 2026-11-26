#!/usr/bin/env python3
"""Rebuild decision rows for analyze.py from PUBLIC HISTORY (no live feed needed).

COARSE and optimistic-about-nothing: Polymarket's price history is ~1 point/minute and midpoints only,
so spread and book depth are ASSUMED (--spread, --slip). Use it for fee / price-band questions, not for
proving an edge. The real test is recorder.py (real ask ladders). Public read-only endpoints only.

  python3 history_backtest.py --hours 48 --out rows.json && python3 analyze.py --rows rows.json
"""
import argparse, json, math, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

G = "https://gamma-api.polymarket.com"; C = "https://clob.polymarket.com"
K = "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1000"


def get(u):
    for _ in range(3):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "bt/1"}), timeout=15))
        except Exception:
            time.sleep(0.5)
    return None


def klines(t0, t1):
    out = {}
    t = t0
    while t < t1:
        d = get(f"{K}&startTime={t*1000}&endTime={min(t+60000, t1)*1000}") or []
        for k in d: out[int(k[0]) // 60000] = (float(k[1]), float(k[4]))
        if not d: break
        t = d[-1][0] // 1000 + 60
    return out


def cdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=48); ap.add_argument("--out", default="rows.json")
    ap.add_argument("--spread", type=float, default=0.02); ap.add_argument("--slip", type=float, default=0.01)
    a = ap.parse_args()
    end = int(time.time()) // 300 * 300 - 900
    wins = list(range(end - int(a.hours * 3600), end, 300))
    kl = klines(wins[0] - 3600, end + 400)
    print("klines", len(kl))

    def one(w):
        ev = get(f"{G}/events?slug=btc-updown-5m-{w}")
        if not ev: return []
        m = ev[0]["markets"][0]
        try: pr = [float(x) for x in json.loads(m["outcomePrices"])]; outs = json.loads(m["outcomes"]); tok = json.loads(m["clobTokenIds"])
        except Exception: return []
        if max(pr) < 0.99: return []
        up_won = outs[pr.index(max(pr))].lower() == "up"
        up_i = 0 if outs[0].lower() == "up" else 1
        h = get(f"{C}/prices-history?market={tok[up_i]}&startTs={w}&endTs={w+300}&fidelity=1")
        h = (h or {}).get("history", [])
        if w // 60 not in kl: return []
        open_px = kl[w // 60][0]
        rows = []
        for pt in h:
            t, mid = pt["t"], pt["p"]
            tr = (w + 300 - t) / 300
            if tr < 0.1 or tr > 0.95 or not 0.02 < mid < 0.98: continue
            mnow = t // 60
            if mnow - 1 not in kl: continue
            spot = kl[mnow - 1][1]  # last fully known close
            cl = [kl[x][1] for x in range(mnow - 31, mnow) if x in kl]
            if len(cl) < 20: continue
            r = [math.log(y / x) for x, y in zip(cl[:-1], cl[1:])]
            mu = sum(r) / len(r); sig = math.sqrt(sum((q - mu) ** 2 for q in r) / (len(r) - 1)) * math.sqrt(5)
            pm = cdf(((spot - open_px) / open_px) / (max(sig, 1e-6) * math.sqrt(tr)))
            for side, pmod, mp in (("up", pm, mid), ("down", 1 - pm, 1 - mid)):
                ask = min(mp + a.spread / 2, 0.99); fill = min(ask + a.slip, 0.99)
                rows.append(dict(win=w, ts=t, t_rem=tr, side=side, p_model=pmod, signal_ask=ask,
                                 signal_fee=0.05 * ask * (1 - ask), fill_ask=fill, fill_fee=0.05 * fill * (1 - fill),
                                 spread=a.spread, won=1.0 if (up_won == (side == "up")) else 0.0))
        return rows
    with ThreadPoolExecutor(12) as ex: res = list(ex.map(one, wins))
    rows = [r for x in res for r in x]
    json.dump(rows, open(a.out, "w"))
    print("windows with rows", sum(1 for x in res if x), "of", len(wins), "rows", len(rows))


if __name__ == "__main__":
    main()
