# Latency-arb telemetry and feasibility

`GET /api/v1/latency-arb/telemetry` returns p50/p95/p99 per stage (event received, signal, book,
quote selected, risk, signing, submit, ack, fill), end-to-end and submit-to-ack time, outcome
counts, signals dropped before submit and traces with an unknown clock offset.
`GET /api/v1/latency-arb/feasibility` returns edge decay, miss rate and a promotion verdict.
Both need a logged-in user.

Rules:
- Stage stamps are monotonic. A trace whose stamps go backwards is counted as invalid and left
  out of percentiles. Stage times must add up to the total.
- Edge is measured at the quote obtainable on arrival after the delay, minus fee. The trigger
  quote is shown only as `original_edge`, so a stale trigger price never shows as profit.
- A missed fill stays in the report as a miss and has no edge.
- A non-positive mean net edge, unknown fills, or no explicit canary approval blocks live
  promotion. The endpoint always reports `canary_approved=false`.

This is measurement. It does not show the strategy is or will be profitable.

## Not covered yet
The engine does not yet record traces or reports into these stores, so both endpoints return
empty summaries until `_execute_opportunity` is instrumented. There is no database persistence,
no UI, and no cross-host clock sync.
