# Net-edge gate for taker strategies

The latency-arb, inverse-bot and copy-trade strategies place taker orders. A taker order pays a
price-dependent fee, crosses half the spread and can slip through the book. Before any order
(paper or live) the strategy now computes:

    net edge = gross edge - taker fee - half spread - expected slippage - safety margin

and places the order only when net edge is greater than zero and at least
`TAKER_MIN_NET_EDGE` (default 0.0). `TAKER_SAFETY_MARGIN` defaults to 0.02.

- **Fee.** Priced from the market's own Gamma fee parameters with the same function used for
  archived valuation (`archived_taker_fee`). The fee is highest near 0.50 and falls toward 0 and 1.
- **Unknown fee, no book or thin depth.** The gate abstains. Unknown never becomes zero.
- **Gross edge.** Latency arb: model probability of the chosen side minus the best ask. Inverse
  bot: new token price minus held token price, and both the sell and buy legs are charged.
  Copy trade: AI probability minus price. A BUY with no probability estimate abstains. A copy SELL
  (exit) is logged but never blocked.
- **Audit log.** Every decision writes one `taker_net_edge_decision` line with gross edge, fee,
  half spread, slippage, margin, net edge and the reason.

The gate removes trades whose edge is smaller than their costs. It does not show that any trade
that passes is profitable.

## Replay of the 13 November capture

`acceptance-replay-2026-11-13/report.json` holds 5 usable reconstructed forecasts. Gross edge
(forecast minus ten-share VWAP) is negative for all 5, so the gate skips 5 of 5. This is a
reconstructed frequency benchmark, not a record of past bot decisions, and the report has no
spread, so real costs are higher than the ones counted. It cannot say how many decisions the live
bots would have skipped.
