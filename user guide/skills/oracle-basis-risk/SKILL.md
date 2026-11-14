# Oracle basis risk — oracle-basis-v1

## Objective and applicability

Quantify the empirical basis (in basis points) and threshold sign-reversal risk
between external spot feeds (e.g. Binance/Kraken) and a Polymarket market's
authoritative settlement oracle (#16). Spot candles are research features only
and never substitute for authoritative settlement.

## Mechanism and interpretation

1. Require `OracleSpecification` pinned to a verified `rule_hash` (`rule_review_status == "verified"`).
2. Align spot and settlement observations in UTC by `source_at` within `max_alignment_skew_seconds`,
   aggregating settlement candidates via `last`, `median`, or `twap` and applying `rounding_decimals`.
3. Compute `basis_bp`, `basis_mean_bp`, `basis_p95_abs_bp`, `basis_max_abs_bp`,
   `sign_reversal_count`, `false_win_rate`, and `feed_checksum_sha256`.
4. Hard-abstain (`status="abstain"`, `oracle_verified=false`) when settlement
   feeds are missing, stale/unaligned, or the resolution rule is unverified.

## Invocation and examples

- Backend: `POST /api/oracle-basis/analyze`, `GET /api/oracle-basis/schema`
  (gated by `ORACLE_BASIS_RISK_ENABLED`, authenticated, read-only).
- Research MCP: `oracle_basis_risk`, `describe_oracle_basis_risk`.

## Source identity, chronology and aggregation

API input must identify every observation's exact instrument and immutable `capture_id`. A source name containing “Chainlink” is insufficient. `/analyze` reads the recorded condition/rule version and server-owned `VenueObservation` receipts, checks provider/instrument/value, condition where present, event/receipt clocks, cutoff, strike/comparator, content hash and origin. No public endpoint inserts authoritative receipts. Polymarket displayed terminal references and legacy RTDS spot snapshots cannot acquire the `chainlink-data-streams` origin through client input.

`oracle_basis_archive.py` is the archive adapter; `venue_observations.py` accepts typed reference envelopes from trusted server capture/import code. Reference envelopes retain exact UTC ISO event timestamps, including 2025 microsecond precision. Licensed source capture/authorization must be supplied independently; this analytical tool requests no wallet, signing, broker or trading API credentials. Missing source history emits `source-unknown`/`ABSTAIN`, never an invented settlement observation.

Pairing requires reference event time at or before the external observation and reference publication at or before that external observation was available. Alignment tolerance never permits future knowledge. Study as-of and market cutoff are enforced separately. Any stale or missing segment prevents verified analysis; usable diagnostic pairs may still be returned with explicit abstention.

TWAP uses duration-weighted step integration over the complete declared interval, with gap/cadence checks and one final declared rounding operation. It is not an arithmetic mean. Rounding defaults to none; floor/ceiling/half-up/half-even are explicit diagnostic assumptions. Current recorded-rule verification supports the parser's verified point-rule/no-rounding grammar; unsupported TWAP/fallback rule forms remain quarantined. Caller-supplied streams can be explored diagnostically but do not self-establish `oracle_verified`.

Outputs include content hashes, raw used-point audits, latency, basis tails, classification error and conditional false-win rate. Wilson 95% intervals assume binomial sampling; correlated windows do not establish independent coverage. Audit output is bounded; oversized panels abstain rather than allocating unbounded memory. No numerical result is trade authorization.

## Recreated evidence and promotion gate

The shared public replay has eight actual exchange-rise/official-Down diagnostics, exact raw receipts and a frozen forward benchmark. Those historical exchange candles were retrieved after settlement and are excluded from forecast features. They are not a complete independently authenticated Chainlink TWAP series. `shadow-baseline.json` records zero complete authoritative historical series and null predictive quantile coverage; the required behavior is abstention. Synthetic goldens and real PostgreSQL API tests establish implementation/lineage correctness, not profitable alpha or historical production validation.

Before promoting this detector, independently obtain the exact authorized oracle series, freeze the study/hypotheses before evaluation, measure held-out coverage/false matches by regime and audit all source/receipt clocks. Production approval is not asserted. Monitor missing/stale/source-mismatch/coverage counts and roll back with `ORACLE_BASIS_RISK_ENABLED=false`. The existing latency engine, pre-trade authority, account caps, signing and emergency halt remain unchanged.

Keep typed numerical schemas behind a versioned adapter when migrating pure primitives to the shared financial-skills package. Venue/archive/rule adapters remain owned here. References: [Polymarket real-time sources](https://docs.polymarket.com/market-data/realtime-data), [current market definitions](https://docs.polymarket.com/market-data/market-details), and the exact Chainlink instrument URLs retained in the raw market receipts.
