# BTC latency-arb: what is built and what is not

Issue #153 asks whether a BTC 15-minute up/down bot has positive expected value after fees.
**No edge has been shown.** "No edge found, do not trade live" is a valid outcome, and nothing
here claims profit.

## Built in code
- Fee-aware gate: PR for #154 (net edge after fee, spread, slippage, margin; rejects logged).
- Promotion gate: PR for #155 (frozen parameter hash, bootstrap CI, demotion).
- Latency telemetry and feasibility: PR for #12.
- `latency_arb_btc_rules.py`: late-window entry rule, a basis-risk skip, and a per-bucket report
  (last 5, 3, 1 minutes). The 8 exchange-vs-official disagreements in the 13 November replay are
  regression tests: every one has a move under 2 bps and is skipped.
  The basis floor (5 bps) is a placeholder, not a measured value.

## Not done
- **Chainlink reader.** No Chainlink source is read. It needs licensed Data Streams credentials
  and cannot be tested here without them. BTC is still priced from Binance.
- **`oracle_verified`.** Stays false. The rule specs are variable-threshold, Chainlink TWAP rules
  that the current grammar quarantines. Binance-vs-Chainlink basis is not logged per window.
- **Late-window backtest by time bucket.** `bucket_report` exists; there is no data to run it on.
  A bucket has not been chosen.
- **Latency report from Cairo and a VPS** with edge-vs-latency curves: needs measurements from
  both hosts and a VPS. Not available.
- **Data capture** of hundreds of resolved BTC windows with order books: needs weeks of capture.
- **2-4 week frozen paper test, out-of-sample CI and Brier results, results report, live pilot:**
  not done. Live trading stays disabled.
