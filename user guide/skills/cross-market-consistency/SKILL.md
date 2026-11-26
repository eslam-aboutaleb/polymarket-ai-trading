---
name: cross-market-consistency
description: Read-only detector for logical inconsistencies between verified related prediction markets (YES/NO complements, exhaustive partitions, nested thresholds, implications). Alerts only; never executable.
version: cross-market-consistency-v1
---

# cross-market-consistency

## Objective
Tell a research agent whether the quoted bid/ask bands of related markets admit any set of probabilities that satisfies a declared logical relation, and if not, by how much and which constraints conflict.

## Applicability and prerequisites
- Relation templates: `complement` (YES/NO of one condition), `partition` (`exhaustive_exclusive` sums to 1, `exclusive` sums to at most 1), `threshold` (P(X>k) non-increasing in strike), `implication` (P(A) <= P(B)).
- Every member needs: condition and token id, rules hash, oracle, settlement venue, end time, bid and ask, snapshot time. The relation must be marked `verified` (rule mapping checked out of band). The skill never infers equivalence from titles.
- Synchronous quotes (within 5 s of each other), no older than 60 s at `as_of`, none after `as_of`.
- No credentials, wallet or broker access. Not needed and not accepted.

## Mechanism
Each probability must sit in its market's [bid, ask] band. The relation adds linear constraints. Exact rational Fourier-Motzkin elimination decides feasibility. If infeasible, the violation magnitude is the smallest uniform widening of every band that restores feasibility, and a deletion filter returns a minimal inconsistent subset of quote and relation constraints. If feasible but the midpoints violate the relation, the result is a `pricing_anomaly` (display prices only, never fills).

## Output
`logical_inconsistency` (no fit inside the bands, enough displayed depth), `pricing_anomaly` (midpoint only), `executable_opportunity_not_established` (consistent, abstained, or depth too thin). `executable` is always false. Abstain codes: RULES_NOT_VERIFIED, ORACLE_OR_SETTLEMENT_MISMATCH, CUTOFF_MISMATCH, WRONG_CONDITION_JOIN, SAME_TOKEN, RULES_HASH_MISMATCH, OVERLAPPING_OUTCOMES, MISSING_QUOTE, CROSSED_BOOK, STALE_QUOTE, LOOK_AHEAD_SNAPSHOT, NON_SYNCHRONOUS_BOOKS, INSUFFICIENT_BASKET_DEPTH, SOLVER_LIMIT. Also returned: assumption chain, the original rules text for review, source ids, rules hashes, and a one-leg loss bound per share (a delayed or partial leg can lose up to the first leg's ask).

## Limitations and non-applicable forms
Different oracle, cutoff or settlement rules are never equated. Overlapping or non-exhaustive groups are never forced to sum to 1. Conditional and scalar markets are unsupported. VOID or disputed resolution states and fees are not modeled here; #9 owns basket cost, depth, fees and leg risk. At most 12 markets.

## Failure modes
Stale or delayed quotes, a wrongly verified relation, a rules change after the hash was taken. Passing tests do not make this a validated or production-approved signal.

## Access
`POST /api/v1/consistency/analyze` and `GET /api/v1/consistency/schema`, behind `CROSS_MARKET_CONSISTENCY_ENABLED` (default off, 503 when off). Telemetry is logged per call. Rollback: turn the flag off. No database changes.

## Future shared skills package
The solver and schemas in `backend/app/services/analytics/consistency/` have no app imports other than the Contract base class, so a versioned import adapter can move them; Polymarket adapters stay in the app.

## References
https://docs.polymarket.com/ , https://gamma-api.polymarket.com/docs
