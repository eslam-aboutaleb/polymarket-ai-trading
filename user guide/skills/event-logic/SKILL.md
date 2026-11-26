# Event logic — event-logic-v1

## Objective and applicability

Express relations between contracts explicitly instead of combining unrelated probability
estimates. Given marginal probabilities (observed prices, subjective forecasts or historical
estimates, each labelled) and approved relations, it answers three questions exactly:

1. Are the inputs jointly coherent? If not, which minimal set of constraints conflicts?
2. What range can P(A and B), P(A | B) or any and/or/not/xor/implies expression take when
   dependence is not known?
3. What is the posterior range after a Bayes update, when likelihood provenance supports one?

It is advisory only (`execution_eligible = false`). `pre_trade_gate.py` and
`live_trading_policy.py` stay authoritative and are not imported.

## Prerequisites

- Binary events with a decision time and a UTC `as_of` per input. Missing, naive, future
  (`TEMPORAL_LEAKAGE`) or older than `max_age_seconds` (`STALE_INPUT`) inputs abstain.
- Relations (`implies`, `prerequisite`, `complement`, `exclusive_group`, `thresholds`,
  `identical`) are used only when `approved = true` with an `evidence_hash` from the
  resolution-rule skill. Otherwise `UNAPPROVED_RELATION`. A relation may pin each event's
  `rule_hash`; if the rule changed, `RULE_REVISED` invalidates it. `identical` needs equal
  rule hashes, so the same market under a different oracle is never treated as the same event.
- At most 10 events (exact simplex over at most 1024 joint worlds, bounded runtime).

## Mechanism

- Marginals are equality constraints. Relations remove impossible worlds. An exact
  Fraction-based two-phase simplex (Bland's rule) minimises and maximises the probability of
  any expression over every feasible joint distribution. No independence, no normalisation.
- Without a relation the range equals the Frechet bounds, `max(0, pA+pB-1) <= P(A and B) <=
  min(pA, pB)`. Implication pins `P(A and B) = P(A)`. Exclusive groups pin it to 0.
- `P(A | B)` is `P(A and B) / P(B)` only for `P(B) > 0`; at 0 it abstains.
- Infeasible input returns the pairwise conflicts and a minimal conflicting constraint set
  (deletion filter), never a repaired answer.
- Bayes updating needs likelihood intervals (never defaulted), provenance `observed` or
  `historical` (a forecast is not a likelihood), `calibrated = true` and at least 30 samples.
  The posterior is a range; independence is not assumed.
- The response carries a serialised evidence graph: node probabilities with provenance,
  source IDs, condition/token IDs, rule hashes and as-of times, plus relation evidence hashes.

## Invocation and examples

- Backend: `POST /api/analysis/event-logic/analyze`, `GET /api/analysis/event-logic/schema`
  (authenticated, read-only, gated by `EVENT_LOGIC_ENABLED`, default off).
- Research MCP: `event_logic`, `describe_event_logic`; shared
  `ResearchMCPClient.prediction_market_skill("event_logic", ...)` rejects execution-capable
  or wrong-version responses.

```json
{"decision_time": "2026-11-20T12:00:00Z",
 "events": [{"name": "ge90k", "p": "0.6", "source": "observed", "as_of": "2026-11-20T11:59:00Z"},
            {"name": "ge100k", "p": "0.3", "source": "observed", "as_of": "2026-11-20T11:59:00Z"}],
 "relations": [{"type": "thresholds", "events": ["ge90k", "ge100k"],
                "approved": true, "evidence_hash": "sha256:..."}],
 "joint": [["ge90k", "ge100k"]]}
```

returns `P(ge90k and ge100k) = 0.3` exactly, `dependence: implied_by_constraints`.

## Not applicable

Non-binary or scalar markets without a rule-verified binary mapping, events beyond 10 per
request, relations inferred only from question similarity, and anything needing a joint that
cannot be observed at settlement.

## Limitations and failure modes

- Ranges can be wide; width is the honest answer when dependence is unknown.
- Garbage marginals give coherent-looking ranges: coherence is not correctness.
- A revised oracle or rule after the evidence hash was taken invalidates the relation.
- Scoring (`score_joint_forecasts`) counts one effective event per settlement cluster and
  compares the independence product with the Frechet-clipped joint. It reports
  `INSUFFICIENT_SAMPLE` below 30 clusters and `NO_IMPROVEMENT` when coherence does not help.

## Evidence and promotion

`shadow-baseline.json` is reproduced by `scripts/event-logic-shadow.py` from the frozen
2026-11-13 replay dataset. It has one settlement window, so a single effective independent
event: independence was inside the Frechet range for every pair, the coherent joint equals
the baseline, and no improvement can be claimed. Status stays implemented detector; promotion
requires human approval and at least 30 independent resolved events with observable joints.

Monitor `event_logic` log lines (status, reason, event and relation counts, elapsed ms) and
the rate of `UNAPPROVED_RELATION` / `RULE_REVISED`. Roll back with `EVENT_LOGIC_ENABLED=false`.

## Replication checklist

1. Run `uv run pytest backend/tests/test_event_logic.py backend/tests/test_event_logic_skill.py`.
2. Re-run `scripts/event-logic-shadow.py` and diff against `shadow-baseline.json`.
3. Confirm no import of execution, signing or network modules in the two service files.

## Future shared-package mapping

The pure Fraction LP, expression AST and Bayes range can move to the versioned financial-skills
package behind an import adapter; relation approval, rule hashes and Polymarket IDs stay in
this app. No venue credentials belong in this file.

## References

- https://en.wikipedia.org/wiki/Boole%27s_inequality
- https://en.wikipedia.org/wiki/Fr%C3%A9chet_inequalities
- https://gamma-api.polymarket.com/docs
