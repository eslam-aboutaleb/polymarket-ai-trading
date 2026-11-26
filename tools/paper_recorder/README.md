# Paper recorder (paper only, no keys, no wallet)

Standalone test of the #195 order-book idea. It records the real Polymarket BTC 5m up/down order books and
BTC spot, and simulates paper fills by walking the real ask ladder. It cannot trade: no private key, no API
key, no order endpoint, no DB, no imports from the bot. It only reads public data and writes local JSONL.

## Run (needs Python 3.10+)
    pip install websockets
    python3 tools/paper_recorder/recorder.py --out paper_data --hours 24
Leave it running 24h or more. Ctrl+C is safe. If a source drops it reconnects. If Binance is geo-blocked it
switches to data-stream.binance.vision on its own. Disk: about 100-150 MB per day.

Options: `--threshold 0.03` (net edge to paper-trade), `--latency 1.0` (seconds signal to fill),
`--shares 10`, `--min-t-rem 0.05`, `--max-age 3`.

## Files in paper_data/
- `snapshots.jsonl`: 1 row per second per window: spot, sigma, model P(Up), top 10 bid/ask levels of both tokens.
- `trades.jsonl`: the default strategy's paper fills (one per window), with avg fill price, fee, levels used, or `no_fill`.
- `outcomes.jsonl`: how each window resolved (from Gamma after settlement).
- `recorder.log`: connection events.

## Analyze with a train/test split
    python3 tools/paper_recorder/analyze.py --data paper_data --latency 1.0
Rules the script enforces:
1. Earlier 60% of windows = TRAIN, later 40% = TEST (by time, never shuffled).
2. A fixed grid of filters (edge threshold, edge >= 3x fee, price band, max spread, min time left) is scored on TRAIN only.
3. The single best TRAIN filter is evaluated once on TEST. Only the TEST number counts.
4. Verdict is "proven" only if TEST mean minus 2 standard errors is above zero with enough trades.
Do not re-tune after seeing TEST. For a second opinion, record a new later period and run the same filter there.

## What this does not tell you
- Spot here is Binance. The market settles on Chainlink BTC/USD TWAP, so the open price and outcome proxy can differ a little. Outcomes come from Gamma (the real result).
- The fill is simulated at the later snapshot's ask ladder. It does not model your order's own impact on the book, queue position, or rejections.
- Windows the recorder joins mid-way are skipped (no valid open price).
- 24h is one regime. A single day cannot prove an edge.

## Optional: rebuild from public history
    python3 tools/paper_recorder/history_backtest.py --hours 48 --out rows.json
    python3 tools/paper_recorder/analyze.py --rows rows.json
History has only ~1 midpoint per minute, so spread and depth are assumptions (`--spread`, `--slip`). Use it for fee and price-band questions only.
