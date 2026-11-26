# ruff: noqa: C408
import unittest
from datetime import UTC, datetime
from decimal import Decimal as D

from app.services.portfolio.capital_allocator import AllocationRequest, allocate

NOW = datetime(2026, 11, 1, tzinfo=UTC)
C1, C2, C3 = ("0x" + str(i) * 64 for i in (1, 2, 3))


def cand(cid, cond, p="0.70", unc="0.05", n=100, price="0.50", depth="1000", outcome="YES"):
    return {
        "candidate_id": cid,
        "condition_id": cond,
        "token_id": f"t-{cid}",
        "outcome": outcome,
        "ask_price": price,
        "depth_shares": depth,
        "p_calibrated": p,
        "p_uncertainty": unc,
        "calibration_oos_n": n,
    }


def req(cands, cash="1000", **kw):
    return AllocationRequest.model_validate(
        {
            "as_of": NOW,
            "account_cash": cash,
            "max_worst_case_loss": "400",
            "candidates": cands,
            **kw,
        }
    )


class Allocator(unittest.TestCase):
    def test_never_authorizes_execution(self):
        r = allocate(req([cand("a", C1)]))
        self.assertIs(r.execution_eligible, False)
        self.assertEqual(r.decision, "advisory_only")

    def test_cash_never_double_allocated_and_reservations_count(self):
        r = allocate(
            req([cand("a", C1), cand("b", C2), cand("c", C3)], cash="1000", reserved_cash="900")
        )
        self.assertEqual(r.cash_available_before, D(100))
        self.assertLessEqual(r.capital_required, D(100))
        self.assertGreaterEqual(r.cash_remaining, 0)
        self.assertEqual(r.capital_required + r.cash_remaining, r.cash_available_before)

    def test_worst_case_loss_bounded(self):
        r = allocate(req([cand("a", C1), cand("b", C2)], cash="100000", max_worst_case_loss="50"))
        self.assertLessEqual(r.worst_case_loss, D(50))

    def test_uncertainty_reduces_sizing(self):
        lo = allocate(req([cand("a", C1, unc="0.02")]))
        hi = allocate(req([cand("a", C1, unc="0.15")]))
        self.assertGreater(lo.sizes[0].shares, hi.sizes[0].shares)

    def test_no_forecast_or_edge_or_calibration_rejected(self):
        r = allocate(req([cand("a", C1, p=None), cand("b", C2, p="0.52"), cand("c", C3, n=3)]))
        self.assertEqual(r.sizes, [])
        reasons = {x.candidate_id: x.reasons[0] for x in r.rejected}
        self.assertEqual(reasons["a"], "MISSING_CALIBRATED_FORECAST")
        self.assertEqual(reasons["b"], "NO_POSITIVE_EDGE_AFTER_UNCERTAINTY")
        self.assertEqual(reasons["c"], "INSUFFICIENT_OOS_CALIBRATION")

    def test_kelly_cap_is_upper_limit(self):
        r = allocate(req([cand("a", C1, p="0.99", unc="0")], cash="1000"))
        self.assertLessEqual(r.sizes[0].cost, D("250.01"))

    def test_identical_events_do_not_diversify(self):
        rel = [{"relation_type": "identical", "left_condition_id": C1, "right_condition_id": C2}]
        sep = allocate(
            req([cand("a", C1), cand("b", C2)], cash="100000", max_worst_case_loss="200")
        )
        lnk = allocate(
            req(
                [cand("a", C1), cand("b", C2)],
                cash="100000",
                max_worst_case_loss="200",
                relations=rel,
            )
        )
        self.assertLessEqual(lnk.worst_case_loss, D(200))
        self.assertLessEqual(sum(s.shares for s in lnk.sizes), sum(s.shares for s in sep.sizes))

    def test_depth_caps_size(self):
        r = allocate(req([cand("a", C1, depth="5")]))
        self.assertEqual(r.sizes[0].shares, D("5.00"))

    def test_comparison_with_equal_risk_and_oos(self):
        r = allocate(
            req([cand("a", C1), cand("b", C2, p="0.60")], oos_outcomes={"a": True, "b": False})
        )
        names = [p.name for p in r.comparison]
        self.assertEqual(names, ["scenario_allocator", "equal_risk_equal_cash", "no_trade"])
        self.assertIsNotNone(r.comparison[0].oos_realized_pnl)
        self.assertIsNotNone(r.comparison[1].oos_realized_pnl)
        none = allocate(req([cand("a", C1)]))
        self.assertIsNone(none.comparison[0].oos_realized_pnl)

    def test_extra_fields_rejected(self):
        with self.assertRaises(ValueError):
            req([cand("a", C1)], wallet_key="x")


if __name__ == "__main__":
    unittest.main()
