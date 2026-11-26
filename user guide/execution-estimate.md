# Execution cost estimate and implementation shortfall (read-only)

Feature flag: `EXECUTION_ESTIMATOR_ENABLED` (default off). Requires the binary-book
research flag and captured native books (see `binary-book-research.md`).

* `POST /api/execution/estimate` walks the captured, validated token-side book for a
  marketable limit, fill-or-kill or maker-rest request and returns an `ExecutionEstimate`
  (VWAP, fees, depth coverage, displayed-walk price impact, quote expiry, maximum
  acceptable price). It stores the estimate for later reconciliation. It never calls
  order submission, signing or cancellation.
* `GET /api/execution/estimates/{id}/revalidate` re-reads the book: a changed best quote,
  changed depth, unusable book or elapsed quote window marks the prior estimate invalid.
* `GET /api/execution/quality` matches stored estimates to exchange-settled gateway orders
  (same account, token, side and size, inside the quote window, exactly one candidate each
  way) and reports estimated versus measured shortfall against the **same arrival midpoint**,
  fill rate, and a cost waterfall. Percentiles appear only with 20+ matched samples.

Rules enforced: off-tick price, off-lot size and below-minimum size are rejected, not
rounded; a taker order with no price bound is rejected; fees are never defaulted to zero;
insufficient depth is never priced as a full fill (FOK returns no price); maker rests that
cross the spread are rejected and carry no fill probability.

Limits: displayed depth only, no queue or fill-probability model, no measured price impact
until settled fills exist. `compare_matched_cohorts` (matched decisions, mean/p50/p95 cost
delta and fill-rate delta, minimum-pair guard) is implemented and tested on fixtures; a real
baseline-versus-policy result needs forward data and is not claimed. No profitability claim.
The preview in the trade modal needs a user-entered fee assumption.
