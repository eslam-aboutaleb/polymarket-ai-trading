# Latency live execution evidence (#104 / #105)

`LATENCY_ARB_LIVE=false` and `LIVE_LATENCY_ARB_ENABLED=false` are the defaults
in Python and both Compose deployments. This change does not activate live trading.
The edge board's Gamma probability is indicative; it cannot authorize an order.

| State | Decision |
| --- | --- |
| Disabled flag / disconnected Binance source | Skip / reject, no signing or order intent |
| Provider timeout, connection error, non-200 or malformed JSON | Reject with a stable provider reason |
| `INVALID_BOOK` | Reject: identity, status, mapping, price, tick, checksum, duplicate or crossed-book failure |
| `STALE` | Reject: missing/future source clock, expired receipt or source timestamp |
| `UNKNOWN_COST` | Reject: missing or unsupported venue fee policy |
| `INSUFFICIENT_DEPTH` | Reject: eligible asks cannot meet minimum order size |
| `READY` | Authorize a bounded, expiring FOK plan; all existing gateway controls still apply |

The shared `binary-book-v1` adapter consumes native object levels (`price`, `size`)
and paired CLOB/Gamma metadata. Both providers must agree on condition and token
identity, explicitly active/open/accepting status and outcome mapping. Explicit
paired Up/Down labels map to the binary YES/NO axes; raw receipts remain unchanged.
Tuple fixtures, guessed positional mappings and Gamma execution-price fallbacks
are not accepted. The shared adapter sorts levels and rejects duplicates, invalid
numbers, crossing and off-tick prices. Bid depth is diagnostic for a BUY; a small
bid does not have to absorb the BUY. Spread still must meet the configured limit.

Receipt and provider timestamps must be known and no more than one second old at
validation. Fetch completion is included in the existing feed-to-order latency
budget. The plan expires at the earlier of the book deadline and the remaining
feed budget. The gateway checks expiration on entry, before signing and before
submission; a slow preparation can leave a rejected audit record, never a post.
It also checks current SDK tick/negative-risk settings against the validated plan.

Money is USDC; book sizes and signed order quantities are shares. The shared depth
walker computes the ask curve and VWAP. The FOK limit is the worst eligible ask,
not VWAP. Shares round down to the supported lot, and **shares × limit + known fees
must stay within the authorized cash cap**. Insufficient total depth downsizes the
order when the remainder meets the minimum: $50 at $0.50 with 60 displayed shares
authorizes 60 shares / $30, never 100 shares. Signing cannot increase the share cap
or reserved cash. VWAP, share size, limit, fee, receipt hash, source identity and
clocks are retained in successful order preparation's calculation details.

Only an explicit `base_fee=0` native `/fee-rate` response is currently supported.
Positive, missing or malformed fees abstain: the research flat-fee assumption is
not a verified venue-specific live fee model. This is a conservative authorization
limit, not a claim about execution quality, profitability or fill probability.

Every runner outcome emits a structured `latency_execution_outcome` log. Controlled
book rejection reasons increment `latency_book_<reason>_total` counters exposed by
the existing admin metrics endpoint. No provider exception strings or wallet keys
are logged by the adapter. The authenticated opportunities API includes only the
caller's last execution outcome; the UI shows its reason, time and source.
The last-outcome cache expires after one hour (shared Redis in deployment; local
memory during development). Logs and committed order history are the audit record;
cache loss/restart never grants execution authority or reuses an old plan.

To roll back execution, set either latency flag to `false` on **web and worker**
and restart those services. The gateway rechecks its strategy flag before post.
This does not delete existing orders, reservations, fills or audit history;
reconcile any previously ambiguous order through the existing gateway workflow.
A new cycle always fetches new evidence. Existing activation, wallet credentials,
risk limits, emergency halt, scheduler lock and economic idempotency remain required.

Validation: `pytest tests/test_latency_book.py tests/test_latency_outcomes.py
 tests/test_latency_arb_service_coverage.py tests/test_latency_arb_paper.py
 tests/test_live_gateway.py tests/test_clob_transport.py tests/test_binary_book.py
 tests/test_book_research.py tests/test_binance_ws_service_coverage.py`; browser
`npm run test:latency` (registered in CI). The browser harness uses the real SQL,
runner, API, adapter and gateway, controlled public HTTP responses and a simulated
private transport. It holds no funded wallet or production credentials.
