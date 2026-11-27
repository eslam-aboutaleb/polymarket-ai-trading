"""Issue #20 property/unit tests (seeded, no network)."""

import itertools
import random
from datetime import UTC, datetime
from fractions import Fraction as F

import pytest

from app.services.intelligence.event_logic import (
    And,
    EventConstraintGraph,
    Not,
    Or,
    Var,
    complement,
    conditional,
    evaluate,
    frechet_and,
    frechet_or,
)

RNG = random.Random(20)  # noqa: S311


def rand_p():
    return F(RNG.randint(0, 100), 100)


def test_de_morgan_over_all_worlds():
    a, b = Var("a"), Var("b")
    for va, vb in itertools.product([False, True], repeat=2):
        w = {"a": va, "b": vb}
        assert evaluate(Not(And((a, b))), w) == evaluate(Or((Not(a), Not(b))), w)
        assert evaluate(Not(Or((a, b))), w) == evaluate(And((Not(a), Not(b))), w)


def test_complement_symmetry_and_bounds_properties():
    for _ in range(300):
        pa, pb = rand_p(), rand_p()
        assert complement(complement(pa)) == pa
        lo, hi = frechet_and(pa, pb)
        assert 0 <= lo <= hi <= min(pa, pb)
        olo, ohi = frechet_or(pa, pb)
        # inclusion-exclusion consistency: P(A or B) = pa + pb - P(A and B)
        assert olo == pa + pb - hi and ohi == pa + pb - lo


def test_any_real_joint_distribution_satisfies_bounds():
    for _ in range(200):
        w = [rand_p() + F(1, 1000) for _ in range(4)]
        s = sum(w)
        w = [x / s for x in w]
        pa, pb, pab = w[0] + w[1], w[0] + w[2], w[0]
        lo, hi = frechet_and(pa, pb)
        assert lo <= pab <= hi


def test_conditional_zero_denominator_abstains():
    assert conditional(F(0), F(0))["reason"] == "CONDITIONING_EVENT_ZERO_PROBABILITY"
    assert conditional("0.2", "0.4")["value"] == F(1, 2)
    assert conditional("0.5", "0.4")["status"] == "abstain"


def test_logical_contradiction_and_reversed_implication_abstain():
    g = EventConstraintGraph()
    g.add_marginal("a", "0.7")
    g.add_marginal("b", "0.5")
    g.implies("a", "b")
    r = g.check()
    assert r["status"] == "abstain" and r["conflicts"][0]["code"] == "IMPLICATION_VIOLATED"
    ok = EventConstraintGraph()
    ok.add_marginal("a", "0.3")
    ok.add_marginal("b", "0.5")
    ok.implies("a", "b")
    assert ok.check()["status"] == "ok"


def test_inconsistent_marginals_complement_and_groups():
    g = EventConstraintGraph()
    g.add_marginal("y", "0.6")
    g.add_marginal("n", "0.5")
    g.complement_pair("y", "n")
    assert g.check()["conflicts"][0]["code"] == "COMPLEMENT_SUM_NOT_ONE"
    h = EventConstraintGraph()
    for n, p in [("x", "0.5"), ("y", "0.4")]:
        h.add_marginal(n, p)
    h.exclusive_group(["x", "y"], exhaustive=True)
    assert h.check()["status"] == "abstain"
    h.exclusive_group(["x", "y"], exhaustive=False)
    h2 = EventConstraintGraph()
    h2.add_marginal("x", "0.5")
    h2.add_marginal("y", "0.4")
    h2.exclusive_group(["x", "y"], exhaustive=False)
    assert h2.check()["status"] == "ok"
    assert any("not exhaustive" in a for a in h2.check()["assumptions"])


def test_nested_thresholds_monotone():
    g = EventConstraintGraph()
    for n, p in [("ge90k", "0.6"), ("ge100k", "0.3"), ("ge110k", "0.4")]:
        g.add_marginal(n, p)
    g.nested_thresholds(["ge90k", "ge100k", "ge110k"])
    assert g.check()["status"] == "abstain"


def test_undefined_overlap_returns_range_not_independence():
    g = EventConstraintGraph()
    g.add_marginal("a", "0.6")
    g.add_marginal("b", "0.7")
    r = g.joint_bounds("a", "b")
    assert (r["lower"], r["upper"]) == (F(3, 10), F(3, 5))
    assert r["dependence"] == "unknown"
    assert r["lower"] != F(6, 10) * F(7, 10)


def test_temporal_leakage_and_missing_fields_rejected():
    g = EventConstraintGraph()
    g.add_marginal("a", "0.5", as_of=datetime(2026, 2, 1, tzinfo=UTC))
    g.implies("a", "ghost")
    codes = {c["code"] for c in g.check(datetime(2026, 1, 1, tzinfo=UTC))["conflicts"]}
    assert {"TEMPORAL_LEAKAGE", "MISSING_MARGINAL"} <= codes


def test_invalid_probabilities_rejected_and_never_executable():
    with pytest.raises(ValueError):
        EventConstraintGraph().add_marginal("a", "1.2")
    with pytest.raises(ValueError):
        EventConstraintGraph().add_marginal("a", True)
    g = EventConstraintGraph()
    g.add_marginal("a", "0.5")
    assert g.check()["execution_eligible"] is False


def test_coherent_distributions_never_flagged():
    for _ in range(100):
        w = [RNG.randint(1, 20) for _ in range(3)]
        s = sum(w)
        g = EventConstraintGraph()
        for n, x in zip("abc", w, strict=True):
            g.add_marginal(n, F(x, s))
        g.exclusive_group(["a", "b", "c"], exhaustive=True)
        assert g.check()["status"] == "ok"
