# Prediction market microstructure — binary-book-v1

## Objective and applicability

Inspect captured token-side order books and calculate **counterfactual displayed
liquidity costs** for explicitly mapped binary YES/NO markets. This implemented
detector is research-only. It is not a wallet, order router, maker-fill model,
forecast, or production-approved execution signal.

Prerequisites: #1 observation archive, native paired CLOB/Gamma market metadata,
known minimum size and tick, token/rule identity, aware UTC cutoff, fresh book
receipts and explicit fee assumptions for monetary cost estimates. Missing fee
information never defaults to zero. Negative-risk market forms, non-binary or
ambiguous mappings, unknown size rules, and halted markets abstain.

## Mechanism and interpretation

1. Read immutable, hash-verified metadata and source events received by the cutoff.
   Metadata cannot be joined from the future. Recompute the native rule mapping
   and require matching CLOB/Gamma outcomes. Keep YES and NO books independent.
2. A full snapshot resets book state. Sort bids descending and asks ascending,
   validate positive prices, nonnegative size, tick alignment and non-crossing.
   Empty, stale, mismatched, malformed or gapped state cannot quote usable costs.
3. Apply a delta only when a versioned source contract establishes consecutive
   sequences, connection identity is unchanged and the next sequence is exact.
   Native market-channel fields do **not** currently establish that contract.
   Unknown continuity remains PARTIAL and requests a new public REST snapshot.
   Trade prints alone never prove book updates, aggressor flow or queue position.
4. Verify a supplied full-book checksum using the pinned py-clob-client-v2 1.2.1
   pure utilities. If required fields are missing, flag it unverifiable. A hash
   mismatch invalidates the book. Absent checksums remain explicit uncertainty.
5. BUY walks asks; SELL walks bids. Round quantity down to the explicitly modeled
   share precision, apply native minimum size/tick constraints and optional limit,
   and stop at actual displayed depth. Return partial coverage, a reproducible
   book hash, per-level curve, fee-inclusive cash and VWAP. USDC means an inclusive
   BUY budget or target net SELL proceeds, not token shares.
6. Fees use a named **flat notional basis-point assumption**, not an automatically
   verified Polymarket fee tier. Same-token round trips report displayed spread
   and fee costs. Complement inequalities are warnings only: token books are not
   freely fungible and their capacity is never summed into executable liquidity.

VERIFIED_BOOK means a structurally validated **captured snapshot**, with checksum
validation where exposed. It does not assert uninterrupted history or an actual
fill. `continuous_history_verified=false`, snapshot-only sequence flags, capture
age and expiry retain that distinction for native data. PARTIAL, STALE and
NO_HISTORY withhold levels/costs and provide explicit reasons.

## Invocation and examples

The Research MCP tool is `binary_book`; `describe_book_research` publishes its
read-only input schema. Shared `ResearchMCPClient.binary_book_research` is available
to analysis/research callers and is deliberately uncached. It uses only a bounded
GET to the existing authenticated backend. Backend session authorization is
required; **never provide broker/wallet/signing credentials** to this skill.

```json
{"tool":"binary_book","args":{"condition_id":"0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","shares":"20","outcome":"YES","side":"BUY","fee_bps":"100","fee_version":"explicit-flat-assumption-v1"}}
```

The condition above is an illustrative placeholder, not a captured market.
A valid 10-share ask at 0.50 followed by 20 shares at 0.60 yields a 20-share
VWAP of 0.55, gross 11 and modeled cash 11.11 with a 100-bps flat fee assumption.
A 40-share request can consume at most 30 displayed shares. No queue probability,
measured price impact, realized PnL or actual fill follows from that calculation.

Backend surfaces: POST `/api/microstructure/analyze`, GET `/book`, GET `/schema`,
POST `/capture` (public data only), POST `/runs`, GET `/runs/{id}` and GET `/runs/{id}/executions/{trade_id}`.
The execution comparison reads existing account-owned settled gateway records;
only matching pre-order quote windows and sizes qualify. It reports gross observed
VWAP difference, not causal market impact, and never creates a live test order. Saved recipes
are strictly account-owned. Their public evidence manifest excludes private fee,
size and result inputs; replay recomputes and verifies the private result hash.
The browser freezes the exact inspected capture IDs to prevent query drift.

The existing maker service exposes a separate read-side diagnostic endpoint;
quote placement/cancellation does not call it. Orchestration can receive compact
server-generated book context when enabled, without trusting caller-supplied
`captured_microstructure` fields or changing risk/intent/halt authority.

## Failure modes and limits

- Unknown sequence, reconnect, checksum mismatch, disappearing liquidity, tick
  changes, late events and simultaneous unorderable updates require resnapshot.
- Public resnapshot jobs use the existing native capture client. At most two jobs
  run per WS manager, with one attempt per subscribed condition per minute and a
  25-second budget. Stop cancels pending work. Failed capture retains a negative
  source receipt when storage is available; database outages remain observable
  in logs and freshness until recovery.
- No historical depth is invented. Analysis inspects at most 100 latest events per
  token; a base snapshot outside that window cannot become a verified replay.
- Book snapshots can disappear before an order reaches the venue. Displayed depth
  costs are not measured market impact or implementation shortfall. Those fields,
  maker probabilities and actual execution remain null/unavailable.
- Metadata freshness is capped at 300 seconds; book freshness is explicitly
  bounded (default 60). A newly observed rule/tick/status change invalidates older
  book joins. Settlement/redemption and unsupported conversion forms are outside
  this skill. #34 owns a future queue/flow policy; the default policy abstains.
- Collateral, additional venue order constraints and actual settlement rounding
  are not verified by a displayed-depth cost estimate; the existing execution
  gateway remains the authority for any real order.
- Public endpoint access does not itself establish reuse rights. No TradingView
  data, provider substitution or credential-bearing endpoint is used.

## Evidence, review and promotion gate

See `evidence.json` and `shadow-baseline.json`. They describe synthetic fixtures,
not historical predictive edge. Run the demo from the backend with
`PYTHONPATH=.:../services/shared .venv/bin/python scripts/demo_binary_book.py`.

Human review must check token/rule identity and availability clocks, snapshot
hash parity with the pinned SDK, gap/expiry abstention, rounding/fees/coverage,
account isolation, and unchanged private execution gates. Independent replication
must reproduce the fixture book hash and VWAP from its levels and cutoff.
Promotion beyond implemented detector requires independently reviewed venue
captures and exchange-confirmed executions at comparable times/sizes, measured
coverage/slippage error with sample counts, operational monitoring and explicit
release approval. Passing fixtures cannot satisfy that gate or enable live orders.

## Integration, rollout and reusable package boundary

Enable `BINARY_BOOK_RESEARCH_ENABLED` for read-only analysis. Collection additionally
requires `NATIVE_CAPTURE_ENABLED` and `OBSERVATION_ARCHIVE_ENABLED`. All default off.
Rollback by disabling the book flag; cancel/restart workers to stop background
resnapshots. Existing trading gates, wallet access, order IDs and risk caps remain
unchanged. Export saved recipes before downgrading the additive run migration.

For a future shared financial-skills repository, preserve `MarketSpec`, `BookEvent`,
`BinaryBook`, `WalkRequest` and `CostEstimate` versioned JSON schemas. Put portable
Decimal reconstruction/depth-walk algorithms behind a versioned import adapter;
retain native CLOB/Gamma capture, rule joins, persistence, authorization and agent
HTTP integration in this application. Do not export venue credentials or silently
reinterpret old stored result versions.

## Verified references (2026-10-26)

- https://docs.polymarket.com/market-data/prices-order-books (official order-book reference; original `/trading/orderbook` redirects here)
- https://docs.polymarket.com/market-data/realtime-data (official WebSocket reference; original developer market-channel URL redirects here)
- https://github.com/Polymarket/py-clob-client-v2 — pinned installed 1.2.1, `utilities.py` pure hash implementation
- https://gamma-api.polymarket.com/docs — paired native market metadata
- Application: `binary_book.py`, `book_research.py`, `book_resnapshot.py`, `clob_ws_manager.py`; existing pre-trade and live policies are execution authorities.
