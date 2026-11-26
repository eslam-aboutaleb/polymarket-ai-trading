#!/usr/bin/env python3
"""Offline analysis for recorder output. Stdlib only. Paper only.

Protocol (no fitting to noise):
  1. Rows are split CHRONOLOGICALLY by window: first --train-frac = TRAIN, the rest = TEST (later period).
  2. A small FIXED grid of filters is scored on TRAIN only. The winner is the one with the best
     (mean PnL - 1 standard error), needing >= --min-trades trades. Number of grid cells is printed
     so you can see how much selection luck is possible.
  3. The winner is evaluated ONCE on TEST. Only the TEST number counts. If TEST mean minus 2 SE is
     not above zero, the honest conclusion is "no proven edge".
PnL per trade is per share, after the market's taker fee, settled at 1/0.

Inputs:
  --data DIR           recorder output dir (snapshots.jsonl + outcomes.jsonl). Uses real ask ladders.
  --rows FILE.json     pre-built decision rows (see history_backtest.py).
  --latency S          seconds between signal and fill (re-walks the ladder at the later snapshot).
"""
import argparse, bisect, itertools, json, math, os, statistics as stats
from collections import defaultdict


def walk(asks, shares, fee_rate):
    need, cost, fee = shares, 0.0, 0.0
    for p, s in asks:
        t = min(need, s); cost += t * p; fee += t * fee_rate * p * (1 - p); need -= t
        if need <= 1e-9: break
    return None if need > 1e-9 else (cost / shares, fee / shares)


def fetch_missing_outcomes(d):
    """Resolve windows that have snapshots but no recorded outcome (the last windows of a batch)."""
    import urllib.request
    have, wins = set(), set()
    p = os.path.join(d, "outcomes.jsonl")
    if os.path.exists(p):
        for l in open(p):
            o = json.loads(l)
            if o.get("winner"): have.add(o["win"])
    for l in open(os.path.join(d, "snapshots.jsonl")):
        wins.add(json.loads(l)["win"])
    with open(p, "a") as f:
        for w in sorted(wins - have):
            try:
                ev = json.load(urllib.request.urlopen(urllib.request.Request(
                    f"https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{w}", headers={"User-Agent": "a/1"}), timeout=15))
                mk = ev[0]["markets"][0]
                pr = [float(x) for x in json.loads(mk["outcomePrices"])]
                if max(pr) >= 0.99:
                    f.write(json.dumps({"win": w, "winner": json.loads(mk["outcomes"])[pr.index(max(pr))].lower(), "src": "gamma_late"}) + "\n")
            except Exception:
                pass


def rows_from_recorder(d, shares, latency):
    snaps = defaultdict(list)
    for l in open(os.path.join(d, "snapshots.jsonl")):
        r = json.loads(l)
        if r["fresh"] and not r["crossed"]:
            snaps[r["win"]].append(r)
    win = {}
    for l in open(os.path.join(d, "outcomes.jsonl")):
        o = json.loads(l)
        if o.get("winner") in ("up", "down"):
            win[o["win"]] = o["winner"]
    out = []
    for w, rs in snaps.items():
        if w not in win: continue
        ts = [r["ts"] for r in rs]
        for r in rs:
            j = bisect.bisect_left(ts, r["ts"] + latency)
            if j >= len(rs) or rs[j]["ts"] - (r["ts"] + latency) > 1.5: continue
            f = rs[j]
            for side, pm in (("up", r["p_model_up"]), ("down", 1 - r["p_model_up"])):
                a0 = r[side + "_asks"]; a1 = f[side + "_asks"]
                if not a0 or not a1: continue
                w0 = walk(a0, shares, r["fee_rate"]); w1 = walk(a1, shares, r["fee_rate"])
                if not w0 or not w1: continue
                bids = r[side + "_bids"]
                spread = a0[0][0] - bids[0][0] if bids else 1.0
                out.append(dict(win=w, ts=r["ts"], t_rem=r["t_rem"], side=side, p_model=pm,
                                signal_ask=w0[0], signal_fee=w0[1], fill_ask=w1[0], fill_fee=w1[1],
                                spread=spread, won=1.0 if win[w] == side else 0.0))
    return out


def trade_pnl(r):  # per share
    return r["won"] - r["fill_ask"] - r["fill_fee"]


def pick(rows, f):
    """first qualifying decision per window, chronological"""
    seen, out = set(), []
    for r in sorted(rows, key=lambda x: (x["win"], x["ts"])):
        if r["win"] in seen: continue
        net = r["p_model"] - r["signal_ask"] - r["signal_fee"]
        if net < f["thr"]: continue
        if f["k_fee"] and net < f["k_fee"] * r["signal_fee"]: continue
        if not (f["lo"] <= r["signal_ask"] <= f["hi"]): continue
        if r["spread"] > f["max_spread"]: continue
        if r["t_rem"] < f["min_t"]: continue
        seen.add(r["win"]); out.append(r)
    return out


def summ(trs):
    n = len(trs)
    if n == 0: return dict(n=0, mean=0, se=0, roi=0)
    p = [trade_pnl(t) for t in trs]
    m = sum(p) / n
    se = (stats.stdev(p) / math.sqrt(n)) if n > 1 else float("nan")
    cost = sum(t["fill_ask"] + t["fill_fee"] for t in trs) / n
    return dict(n=n, mean=m, se=se, roi=m / cost if cost else 0, hit=sum(t["won"] for t in trs) / n)


GRID = [dict(thr=a, k_fee=b, lo=c[0], hi=c[1], max_spread=d, min_t=e)
        for a, b, c, d, e in itertools.product([0.03, 0.05, 0.08, 0.12], [0, 3], [(0, 1), (0.2, 0.8), (0.3, 0.7)],
                                               [0.02, 1.0], [0.05, 0.25])]


def fmt(s):
    return f"n={s['n']:4d} mean/share={s['mean']:+.4f} SE={s['se']:.4f} ROI={s['roi']*100:+.1f}% hit={s.get('hit',0)*100:.0f}%"


def breakdown(rows, thr):
    base = [r for r in rows]
    tr = pick(base, dict(thr=thr, k_fee=0, lo=0, hi=1, max_spread=1.0, min_t=0.05))
    print(f"\nBaseline (thr={thr}, no other filter): {fmt(summ(tr))}")
    def by(name, keyf, edges):
        print(f"  by {name}:")
        for lo, hi in zip(edges[:-1], edges[1:]):
            g = [t for t in tr if lo <= keyf(t) < hi]
            if g: print(f"    [{lo:.2f},{hi:.2f}) {fmt(summ(g))}")
    by("signal ask price band", lambda t: t["signal_ask"], [0, .2, .3, .4, .5, .6, .7, 1.01])
    by("spread", lambda t: t["spread"], [0, .011, .021, .04, 1.01])
    by("fee per share", lambda t: t["signal_fee"], [0, .006, .010, .0126])
    by("slippage fill-vs-signal (ask)", lambda t: t["fill_ask"] - t["signal_ask"], [-1, 0.0001, .01, .03, 1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data"); ap.add_argument("--rows")
    ap.add_argument("--fetch-missing-outcomes", action="store_true", help="look up outcomes for windows without one (network)")
    ap.add_argument("--shares", type=float, default=10); ap.add_argument("--latency", type=float, default=1.0)
    ap.add_argument("--train-frac", type=float, default=0.6); ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--baseline-thr", type=float, default=0.03)
    a = ap.parse_args()
    if a.data and a.fetch_missing_outcomes: fetch_missing_outcomes(a.data)
    rows = json.load(open(a.rows)) if a.rows else rows_from_recorder(a.data, a.shares, a.latency)
    wins = sorted({r["win"] for r in rows})
    print(f"{len(rows)} decision rows over {len(wins)} resolved windows")
    if len(wins) < 20: print("Too few windows for anything but a smoke test."); 
    cut = wins[int(len(wins) * a.train_frac)] if wins else 0
    train = [r for r in rows if r["win"] < cut]; test = [r for r in rows if r["win"] >= cut]
    print(f"TRAIN windows < {cut} ({len({r['win'] for r in train})}), TEST windows >= {cut} ({len({r['win'] for r in test})})")
    breakdown(train, a.baseline_thr)
    scored = []
    for f in GRID:
        s = summ(pick(train, f))
        if s["n"] >= a.min_trades: scored.append((s["mean"] - s["se"], f, s))
    print(f"\nGrid cells: {len(GRID)}; eligible on TRAIN (>= {a.min_trades} trades): {len(scored)}")
    if not scored:
        print("No filter has enough TRAIN trades. Record longer."); return
    scored.sort(key=lambda x: -x[0])
    _, f, s = scored[0]
    print("Best on TRAIN:", f, "\n  TRAIN:", fmt(s))
    t = summ(pick(test, f)); print("  TEST (counts):", fmt(t))
    base = summ(pick(test, dict(thr=a.baseline_thr, k_fee=0, lo=0, hi=1, max_spread=1.0, min_t=0.05)))
    print("  TEST unfiltered baseline:", fmt(base))
    ok = t["n"] >= a.min_trades and t["mean"] - 2 * t["se"] > 0
    print("\nVERDICT:", "TEST edge above zero by >2 SE (still paper; repeat on a later period)" if ok else
          "NOT PROVEN. TEST mean is within noise of zero or negative. Do not trade this.")


if __name__ == "__main__":
    main()
