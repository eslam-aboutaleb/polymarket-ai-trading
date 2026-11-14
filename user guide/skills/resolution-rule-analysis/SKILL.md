# Resolution rule analysis — resolution-rule-v1

## Objective and applicability

Parse Polymarket binary market metadata, outcome-token mappings, oracle sources,
UTC cutoffs, comparators, thresholds, and void/dispute clauses into a versioned
`ContractSpecification` with exact `EvidenceSpan` citations. Unverified,
ambiguous, disputed, or voided rules categorically block payoff resolution and
tradable-edge claims (`review_status != "verified"` -> `ABSTAIN`).

Prerequisites: explicit binary `YES`/`NO` (or paired `Up`/`Down`) token mapping,
non-empty resolution text, explicit authoritative oracle, and unambiguous UTC
cutoff. Multi-outcome markets, missing rules, missing oracles, DST-ambiguous
timezones, and title-vs-rules mismatches abstain.

## Mechanism and interpretation

1. **Identity & token mapping**: Validate `condition_id` equality and explicit
   two-token `YES`/`NO` mapping (no positional inference). Compute `source_hash`
   over canonical `{condition_id, question, rules, end_date, oracle, tokens}`.
2. **Deterministic extraction with spans**: Extract `>=`/`<=` before `>`/`<` so
   inclusive thresholds are never misclassified as strict; record `EvidenceSpan`
   with `(field, quote, start, end, source_url)`.
3. **Timezone & DST validation**: Convert explicit UTC/IANA/fixed-offset cutoffs
   to UTC; flag `ET`/`PT`/`CT` without offset as `AMBIGUOUS_DAYLIGHT_SAVING_TIMEZONE`
   and flag `EST`/`EDT` mismatches against the calendar date.
4. **Mismatch & change detection**: Compare title vs rules comparators/thresholds,
   metadata `resolutionSource` vs rules oracle, and `end_date_iso` vs rules cutoff.
   `detect_rule_change` invalidates prior forecasts/valuations when `source_hash`
   or token mappings change.
5. **Deterministic payoff**: `resolve_payoff(spec, observation)` returns `YES`,
   `NO`, `VOID`, or `ABSTAIN`. It never resolves a payoff when `review_status != "verified"`.

## Invocation and examples

The Research MCP tool is `resolution_rule_analysis`; `describe_resolution_rule_analysis`
publishes its read-only schema. Backend endpoints (gated by
`RESOLUTION_RULE_ANALYSIS_ENABLED`, authenticated, read-only):

- `GET /api/resolution-rules/explain?condition_id=...&as_of=...`
- `POST /api/resolution-rules/analyze`
- `GET /api/resolution-rules/schema`

```json
{"tool":"resolution_rule_analysis","args":{"condition_id":"0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}
```

Never provide wallet, private-key, or order-signing credentials to this skill.
`execution_eligible` is always `false`.

## Failure modes and non-applicable forms

- **Ambiguous comparator** ("reaches", "hits") -> `ABSTAIN_AMBIGUOUS_COMPARATOR`
- **DST / missing timezone / missing cutoff** -> `ABSTAIN_AMBIGUOUS_CUTOFF`
- **Missing or mismatched oracle** -> `ABSTAIN_MISSING_ORACLE` / `ABSTAIN_METADATA_RULES_MISMATCH`
- **Multi-outcome or flipped token mapping** -> `ABSTAIN_NON_BINARY_OR_MISSING_TOKENS` / `TOKEN_MAPPING_FLIPPED_OR_CHANGED`
- **Active dispute or voided market** -> `ABSTAIN_DISPUTED_MARKET` / `ABSTAIN_VOID_MARKET`

## References & migration path

- Backend owner: `backend/app/services/intelligence/resolution_rules.py`
- Integration: `forecast_resolution.py` (`verify_rule_before_settlement`)
- Future shared SDK adapter: `ContractSpecification` and `resolve_payoff` are pure
  Pydantic/Decimal functions ready to extract to `financial-skills` while keeping
  Polymarket CLOB/Gamma field adapters in this repository.

## Capture integrity and supported grammar

`POST /api/resolution-rules/refresh` fetches public official metadata server-side and records its actual receipt time. `/analyze` is hypothetical: caller-supplied rules cannot establish an official snapshot or tradable-edge permission. An `as_of` miss never fetches current metadata or backdates a caller payload. A→B→A amendments retain all three receipt-time versions; ordinary finalization does not masquerade as a rule amendment, while dispute/void changes invalidate assumptions.

The initial verified grammar supports fixed quantitative thresholds at a single explicit UTC cutoff. Variable opening thresholds, TWAP/median/average/window contracts, unknown rounding and fallback policies abstain. Paired Up/Down token identity alone is insufficient to verify their payout semantics. Current TWAP crypto markets in the public replay are deliberately quarantined.

Payoff observations require exact provider/instrument identity and the cutoff event timestamp, separately from publication/receipt time. Delayed publication can carry the correct cutoff observation; a later price cannot replace it. Void payout requires explicit 50/50 rule text and the same identity/time checks. Unspecified or unsupported policies abstain.

When the rule-analysis flag is enabled, the existing forecast resolver requires a recorded verified decision-time version and compares current official rules before scoring. Missing/unavailable or changed rules exclude the forecast. Quarantined scan forecasts are found by condition ID even without a trade-history row; later resolver passes cannot silently clear amendment quarantine. This is scoring protection, not a new trading or redemption authorization path.

`shadow-baseline.json` points to actual raw-source, receipt-time replay evidence. Golden fixtures are explicitly synthetic. Initial evidence remains an implemented detector. Monitor historical misses, amendments, source mismatches and unsupported grammar; disable `RESOLUTION_RULE_ANALYSIS_ENABLED` to roll back optional analytical/scoring integration. Existing execution and finalized-redemption controls remain authoritative.
