# ruff: noqa: C408
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from app.services.analytics.consistency.cross_market_consistency import (
    ConsistencyRequest,
    analyze,
)

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
END = datetime(2026, 12, 31, tzinfo=UTC)


def mk(i, bid, ask, size="100", **kw):
    d = dict(
        market_id=f"m{i}",
        condition_id=kw.pop("condition_id", f"c{i}"),
        token_id=f"t{i}",
        question=f"q{i}",
        rules_hash=kw.pop("rules_hash", "h"),
        oracle=kw.pop("oracle", "uma"),
        settlement_venue="polymarket",
        end_time=kw.pop("end_time", END),
        bid=bid,
        ask=ask,
        bid_size=size,
        ask_size=size,
        snapshot_at=kw.pop("snapshot_at", T0),
    )
    d.update(kw)
    return d


def run(template, quotes, verified=True, kind="exhaustive_exclusive", as_of=T0, **mkw):
    n = len(quotes)
    return analyze(
        ConsistencyRequest.model_validate(
            dict(
                as_of=as_of,
                markets=quotes,
                relation=dict(
                    relation_id="r",
                    template=template,
                    members=[f"m{i}" for i in range(n)],
                    verified=verified,
                    partition_kind=kind,
                ),
                **mkw,
            )
        )
    )


class Consistency(unittest.TestCase):
    def test_exhaustive_partition_underpriced_is_logical_inconsistency(self):
        r = run("partition", [mk(0, "0.20", "0.25"), mk(1, "0.20", "0.25"), mk(2, "0.20", "0.25")])
        self.assertEqual(r.classification, "logical_inconsistency")
        self.assertEqual(r.violation_magnitude, D("0.083333"))
        self.assertIn("relation:sum>=1", r.minimal_inconsistent_subset)
        self.assertFalse(r.executable)

    def test_partial_partition_is_not_forced_to_sum_one(self):
        r = run("partition", [mk(0, "0.20", "0.25"), mk(1, "0.20", "0.25")], kind="exclusive")
        self.assertEqual(r.classification, "executable_opportunity_not_established")
        self.assertFalse(r.abstained)

    def test_exclusive_overpriced_bids_flag(self):
        r = run("partition", [mk(0, "0.60", "0.65"), mk(1, "0.55", "0.60")], kind="exclusive")
        self.assertEqual(r.classification, "logical_inconsistency")

    def test_overlapping_outcomes_abstain(self):
        r = run("partition", [mk(0, "0.6", "0.65"), mk(1, "0.6", "0.65")], kind="overlapping")
        self.assertIn("OVERLAPPING_OUTCOMES", r.abstain_reasons)

    def test_complement_same_condition(self):
        q = [mk(0, "0.40", "0.42", condition_id="c"), mk(1, "0.40", "0.42", condition_id="c")]
        self.assertEqual(run("complement", q).classification, "logical_inconsistency")
        ok = [mk(0, "0.49", "0.51", condition_id="c"), mk(1, "0.49", "0.51", condition_id="c")]
        self.assertFalse(run("complement", ok).abstained)

    def test_complement_wrong_condition_join(self):
        r = run("complement", [mk(0, "0.4", "0.42"), mk(1, "0.4", "0.42")])
        self.assertIn("WRONG_CONDITION_JOIN", r.abstain_reasons)

    def test_threshold_monotonic_violation(self):
        r = run("threshold", [mk(0, "0.30", "0.32"), mk(1, "0.50", "0.52")])
        self.assertEqual(r.classification, "logical_inconsistency")
        self.assertEqual(r.violation_magnitude, D("0.090000"))

    def test_threshold_consistent(self):
        r = run("threshold", [mk(0, "0.50", "0.52"), mk(1, "0.30", "0.32")])
        self.assertEqual(r.classification, "executable_opportunity_not_established")

    def test_implication_violation(self):
        r = run("implication", [mk(0, "0.70", "0.72"), mk(1, "0.40", "0.42")])
        self.assertEqual(r.classification, "logical_inconsistency")

    def test_different_oracle_or_cutoff_never_implies_arbitrage(self):
        q = [mk(0, "0.70", "0.72"), mk(1, "0.40", "0.42", oracle="other")]
        self.assertIn("ORACLE_OR_SETTLEMENT_MISMATCH", run("implication", q).abstain_reasons)
        q = [mk(0, "0.70", "0.72"), mk(1, "0.40", "0.42", end_time=END + timedelta(days=1))]
        r = run("implication", q)
        self.assertIn("CUTOFF_MISMATCH", r.abstain_reasons)
        self.assertEqual(r.classification, "executable_opportunity_not_established")

    def test_contradictory_rules_hash_abstains(self):
        q = [
            mk(0, "0.4", "0.42", condition_id="c"),
            mk(1, "0.4", "0.42", condition_id="c", rules_hash="x"),
        ]
        self.assertIn("RULES_HASH_MISMATCH", run("complement", q).abstain_reasons)

    def test_unverified_relation_abstains(self):
        r = run("implication", [mk(0, "0.7", "0.72"), mk(1, "0.4", "0.42")], verified=False)
        self.assertIn("RULES_NOT_VERIFIED", r.abstain_reasons)

    def test_non_synchronous_books_not_executable(self):
        q = [mk(0, "0.7", "0.72"), mk(1, "0.4", "0.42", snapshot_at=T0 - timedelta(seconds=30))]
        r = run("implication", q)
        self.assertIn("NON_SYNCHRONOUS_BOOKS", r.abstain_reasons)
        self.assertIsNone(r.violation_magnitude)

    def test_stale_and_lookahead(self):
        q = [mk(0, "0.7", "0.72"), mk(1, "0.4", "0.42")]
        self.assertIn(
            "STALE_QUOTE", run("implication", q, as_of=T0 + timedelta(minutes=5)).abstain_reasons
        )
        self.assertIn(
            "LOOK_AHEAD_SNAPSHOT",
            run("implication", q, as_of=T0 - timedelta(seconds=1)).abstain_reasons,
        )

    def test_insufficient_depth_not_established_but_violation_reported(self):
        q = [mk(0, "0.70", "0.72", size="2"), mk(1, "0.40", "0.42", size="2")]
        r = run("implication", q)
        self.assertEqual(r.classification, "executable_opportunity_not_established")
        self.assertIn("INSUFFICIENT_BASKET_DEPTH", r.abstain_reasons)
        self.assertGreater(r.violation_magnitude, 0)

    def test_leg_loss_bound_and_missing_quote(self):
        r = run("implication", [mk(0, "0.70", "0.72"), mk(1, "0.40", "0.42")])
        self.assertEqual(r.one_leg_loss_bound_per_share, D("0.72"))
        r = run("implication", [mk(0, None, "0.72"), mk(1, "0.40", "0.42")])
        self.assertIn("MISSING_QUOTE", r.abstain_reasons)

    def test_midpoint_only_violation_is_pricing_anomaly(self):
        r = run("implication", [mk(0, "0.40", "0.80"), mk(1, "0.30", "0.50")])
        self.assertEqual(r.classification, "pricing_anomaly")
        self.assertFalse(r.minimal_inconsistent_subset)
        r = run("threshold", [mk(0, "0.30", "0.60"), mk(1, "0.45", "0.55")])
        self.assertEqual(r.classification, "pricing_anomaly")

    def test_chained_threshold_minimal_subset(self):
        q = [mk(0, "0.50", "0.52"), mk(1, "0.40", "0.42"), mk(2, "0.45", "0.47")]
        r = run("threshold", q)
        self.assertEqual(r.classification, "logical_inconsistency")
        self.assertEqual(
            r.minimal_inconsistent_subset, ["quote:m1:ask", "quote:m2:bid", "relation:order[1]"]
        )

    def test_no_guarantee_language_and_never_executable(self):
        r = run("partition", [mk(0, "0.2", "0.25"), mk(1, "0.2", "0.25")])
        self.assertNotIn("risk-free", r.model_dump_json().lower())
        self.assertIs(r.executable, False)


if __name__ == "__main__":
    unittest.main()
