# Recreated public-source acceptance replay

Captured on 13 November 2026 UTC (14 November in Cairo). `dataset.json.gz` contains 2,123 HTTP receipts, exact raw response bytes, content hashes, request/receipt clocks, RTDS spot snapshots, the capture hypothesis and the separately frozen frequency benchmark. Sources are public Gamma/CLOB, Polymarket's displayed terminal-reference endpoint and Coinbase. No account, wallet or provider credentials were used.

The training cohort contains 24 previously resolved windows per asset (BTC/ETH/SOL/XRP). Training labels were received before the frequency benchmark was frozen at 21:42:39 UTC. Its holdout begins at 21:45 UTC. Historical data retrieved today is not backdated into old decisions. The benchmark is reconstructed, not a record of historical bot forecasts or independently approved individual-event calibration.

Eight forward market outcomes were captured. Five have usable, checksum-verified contemporaneous books under the replay's ten-share depth/freshness checks. Final labels require both CLOB's unique winning token and Gamma's resolved status and matching 1/0 outcome map. The frequency benchmark's Brier score is approximately **0.30148**, versus **0.08732** for the market midpoint. This small study shows no improvement over market prices and establishes no profitable alpha.

The report includes size-aware quote costs, market-specific Gamma fee parameters, conservative five-decimal fee rounding, slippage sensitivities, an opportunity ranking and counterfactual settlement cashflows. These are displayed-depth models; no exchange fills, rebates, realized trading returns or order authorization are claimed. The ranking and cashflow sensitivity are diagnostic calculations, not a precommitted trading-strategy backtest.

Eight diagnostic cases show exchange minute candles rising while the corresponding official Polymarket outcome is Down. Those candles were fetched after settlement and are excluded from forecast features. A final-minute exchange candle is not the exact Chainlink TWAP. Polymarket displayed terminal references and legacy RTDS spot snapshots do not supply a complete authoritative TWAP series; `oracle_verified` remains false.

All 124 captured rule specifications are quarantined as ambiguous/unsupported: these contracts require variable opening thresholds and Chainlink TWAP semantics outside the initial fixed-threshold point-rule grammar. They must not inherit a verified fixed-threshold payout mapping. Supported fixed-threshold rules, equality, DST, amendment, dispute and void behavior have separate explicitly synthetic golden tests.

Reproduce offline from the repository root:

```sh
uv run --project backend --frozen python scripts/acceptance-replay-report.py \
  "user guide/acceptance-replay-2026-11-13/dataset.json.gz" \
  --output /tmp/acceptance-report.json
```

The script verifies raw-byte hashes, source origins, availability clocks, training-label availability and native book checksums before computing scores. CI runs the same replay without credentials. Inspect `report.json` for exact inputs, exclusions, source hashes and all limitations.

Primary methodology references: [market details and fee parameters](https://docs.polymarket.com/market-data/market-details), [price-dependent fees](https://docs.polymarket.com/trading/fees), [real-time data](https://docs.polymarket.com/market-data/realtime-data). The displayed terminal-reference endpoint is public first-party web data, not a documented historical Chainlink API or an independently authenticated Chainlink report.
