"""Issue #9 acceptance: payoff-verified, depth-aware arbitrage feasibility."""

import json
import random
import re
from decimal import Decimal
from pathlib import Path

import pytest
from arbitrage_fixtures import CID, NOW, leg, pair, rules, spec

from app.services.analytics.arbitrage.arbitrage_solver import CostAssumptions
from app.services.analytics.arbitrage.execution_feasibility import (
    assess_candidate,
    indicative_candidate,
)
from app.services.analytics.arbitrage.payout_validator import (
    build_payoff_matrix,
    validate_binary_complement,
    verify_cross_venue,
)

ZERO = CostAssumptions(gas_usdc=Decimal(0), settlement_usdc=Decimal(0))
FULL = CostAssumptions(
    gas_usdc=Decimal(0),
    settlement_usdc=Decimal(0),
    withdrawal_usdc=Decimal(0),
    bridge_fx_usdc=Decimal(0),
)


def run(legs, size="100", costs=ZERO, rule_pair=None, **kw):
    rule_pair = rule_pair or [rules(legs[0].spec), rules(legs[1].spec)]
    return assess_candidate(
        legs=legs, rules=rule_pair, requested_size=Decimal(size), costs=costs, as_of=NOW, **kw
    )


# ---- AC: midpoint-only positive arb with bad executable asks is rejected ----
def test_midpoint_positive_but_asks_bad_is_rejected():
    # midpoints .45 + .50 look like a 5% gap; executable asks are .55 / .60.
    _, legs = pair(
        [("0.55", 500)], [("0.60", 500)], yes_bids=[("0.35", 500)], no_bids=[("0.40", 500)]
    )
    cand = run(legs)
    assert cand.state == "INVALID"
    assert "NO_POSITIVE_NET_BOUND" in cand.reasons
    assert cand.executable_profit is None
    assert cand.net_bound.quote_gross_gap < 0


def test_ask_depth_insufficient_rejected():
    _, legs = pair([("0.45", Decimal("0.5"))], [("0.50", 500)])
    cand = run(legs, size="10")
    assert cand.state == "INVALID"
    assert any("INSUFFICIENT" in r or "BELOW_MINIMUM" in r for r in cand.reasons)
    assert cand.executable_profit is None


def test_fees_erase_positive_gap():
    _, legs = pair([("0.48", 500)], [("0.50", 500)], fee="300")
    cand = run(legs)
    bound = cand.net_bound
    assert bound.quote_gross_gap > 0  # quote-gross looks positive ...
    assert bound.fee_inclusive_bound < 0  # ... fee-inclusive bound is negative
    assert cand.state == "INVALID" and "QUOTE_GAP_ERASED_BY_COSTS" in cand.reasons
    assert cand.executable_profit is None


def test_quote_gross_and_fee_inclusive_reported_separately():
    _, legs = pair([("0.45", 500)], [("0.50", 500)], fee="100")
    cand = run(legs)
    bound = cand.net_bound
    assert bound.quote_gross_gap == Decimal("5.00")
    assert bound.total_fees == Decimal("0.95")
    assert bound.fee_inclusive_bound == Decimal("4.05")
    assert bound.net_bound == Decimal("4.05")
    assert cand.state == "EXECUTION_READY"
    assert cand.executable_profit == Decimal("4.05")
    assert bound.capital_required == Decimal("95.95")


# ---- AC: partial depth scales size or rejects ----
def test_partial_depth_scales_size():
    _, legs = pair([("0.45", 500)], [("0.50", 40)])
    cand = run(legs, size="100")
    assert cand.state == "PARTIAL"
    assert cand.size == Decimal("40.00")
    assert cand.requested_size == Decimal(100)
    assert "SIZE_SCALED_TO_COMMON_DEPTH" in cand.reasons
    assert all(q.matched_shares == Decimal("40.00") for q in cand.leg_quotes)


def test_multi_level_vwap_and_max_profitable_size():
    # second YES level is too expensive: profit only on the first 50 shares.
    _, legs = pair([("0.40", 50), ("0.60", 500)], [("0.50", 500)])
    cand = run(legs, size="200")
    assert cand.state == "INVALID"  # 200 shares: vwap .55 + .50 > 1
    assert cand.max_profitable_size == Decimal("50.00")
    assert cand.max_executable_size == Decimal("500")
    small = run(legs, size="50")
    assert small.state == "EXECUTION_READY" and small.executable_profit == Decimal("5.00")


# ---- AC: mismatched rules rejected ----
def test_rule_hash_mismatch_between_tokens_rejected():
    sp = spec()
    other = spec(rule_hash="different")
    legs = [leg("YES", sp, "YES", [("0.45", 100)]), leg("NO", other, "NO", [("0.50", 100)])]
    cand = run(legs, rule_pair=[rules(sp), rules(sp)])
    assert cand.state == "INVALID"
    assert "RULE_HASH_MISMATCH" in cand.reasons


def test_rule_drift_rejected():
    sp, legs = pair([("0.45", 100)], [("0.50", 100)])
    cand = run(legs, rule_pair=[rules(sp, rule_hash="drifted"), rules(sp, rule_hash="drifted")])
    assert cand.state == "INVALID"
    assert any(r.startswith("RULE_HASH_DRIFT") for r in cand.reasons)


def test_non_binary_negative_risk_and_unmatched_token_rejected():
    sp, legs = pair([("0.45", 100)], [("0.50", 100)])
    cand = run(legs, rule_pair=[rules(sp, neg_risk=True), rules(sp, neg_risk=True)])
    assert cand.state == "INVALID"
    assert "NEGATIVE_RISK_MARKET_NOT_BINARY_COMPLEMENT" in cand.reasons
    both_yes = [leg("A", sp, "YES", [("0.45", 100)]), leg("B", sp, "YES", [("0.50", 100)])]
    assert run(both_yes).state == "INVALID"
    unknown = run(legs, rule_pair=[rules(sp, neg_risk=None), rules(sp, neg_risk=None)])
    assert "NEG_RISK_STATUS_UNKNOWN" in unknown.reasons and unknown.state == "INVALID"


def test_unmatched_token_book_rejected():
    sp = spec()
    wrong_book = leg("YES", sp, "YES", [("0.45", 100)])
    wrong_book = wrong_book.model_copy(
        update={"book": wrong_book.book.model_copy(update={"token_id": "22"})}
    )
    cand = run([wrong_book, leg("NO", sp, "NO", [("0.5", 100)])])
    assert cand.state == "INVALID"
    assert any(r.startswith("TOKEN_MISMATCH") for r in cand.reasons)


def test_missing_rule_hash_rejected():
    sp, legs = pair([("0.45", 100)], [("0.50", 100)])
    cand = run(legs, rule_pair=[rules(sp, rule_hash=None), rules(sp, rule_hash=None)])
    assert "RULE_HASH_MISSING" in cand.reasons and cand.state == "INVALID"


# ---- stale / skew / fees unknown / void ----
def test_stale_book_rejected():
    _, legs = pair([("0.45", 100)], [("0.50", 100)], age="120")
    cand = run(legs)
    assert cand.state == "STALE" and cand.executable_profit is None


def test_asynchronous_books_rejected():
    sp = spec()
    legs = [
        leg("YES", sp, "YES", [("0.45", 100)], age="1"),
        leg("NO", sp, "NO", [("0.50", 100)], age="30"),
    ]
    cand = run(legs)
    assert cand.state == "STALE" and "ASYNCHRONOUS_BOOKS" in cand.reasons


def test_unknown_fee_abstains():
    _, legs = pair([("0.45", 100)], [("0.50", 100)], fee=None)
    cand = run(legs)
    assert cand.state == "INVALID"
    assert "UNKNOWN_FEES" in cand.badges and cand.executable_profit is None


def test_unknown_gas_or_settlement_cannot_be_execution_ready():
    _, legs = pair([("0.45", 100)], [("0.50", 100)])
    cand = run(legs, costs=CostAssumptions())
    assert cand.state == "DEPTH_VERIFIED"
    assert cand.executable_profit is None
    assert cand.net_bound.unknown_costs == ["gas_usdc", "settlement_usdc"]
    assert "UNKNOWN_FEES" in cand.badges
    assert any(row["amount"] == "UNKNOWN" for row in cand.cost_waterfall)


def test_wide_spread_no_edge_not_arbitrage():
    _, legs = pair(
        [("0.70", 100)], [("0.70", 100)], yes_bids=[("0.30", 100)], no_bids=[("0.30", 100)]
    )
    cand = run(legs)
    assert cand.state == "INVALID" and cand.executable_profit is None


def test_zero_edge_rounding_never_false_positive():
    _, legs = pair([("0.50", 100)], [("0.50", 100)])
    cand = run(legs)
    assert cand.state == "INVALID" and cand.net_bound.net_bound == 0


def test_void_scenario_in_matrix():
    sp, legs = pair([("0.45", 100)], [("0.50", 100)])
    cand = run(legs)
    names = {s.name for s in cand.payoff_matrix.scenarios}
    assert names == {"c0=YES", "c0=NO", "c0=VOID"}
    assert all(s.total == 1 for s in cand.payoff_matrix.scenarios)


# ---- AC: one-leg risk bounded and shown ----
def test_one_leg_risk_bounded_and_shown():
    _, legs = pair(
        [("0.45", 100)],
        [("0.50", 100)],
        yes_bids=[("0.40", 100)],
        no_bids=[("0.30", 100)],
        fee="100",
    )
    cand = run(legs)
    risk = cand.partial_fill_risk
    assert risk.non_atomic is True
    spent = {q.label: q.net_cash for q in cand.leg_quotes}
    assert risk.max_loss_bound == max(spent.values())
    full = [c for c in risk.cases if c.fill_fraction == 1]
    assert {c.filled_leg for c in full} == {"YES", "NO"}
    for case in risk.cases:
        assert case.bound >= case.modeled_unwind_loss >= 0
    half = [c for c in risk.cases if c.fill_fraction == Decimal("0.5")]
    assert half and all(c.cash_spent < spent[c.filled_leg] for c in half)


def test_one_leg_unwind_unavailable_without_bids():
    _, legs = pair([("0.45", 100)], [("0.50", 100)])
    risk = run(legs).partial_fill_risk
    assert all(c.unwind_status == "unavailable" for c in risk.cases)
    assert all(c.modeled_unwind_loss is None and c.bound == c.cash_spent for c in risk.cases)


# ---- AC: no 'guaranteed' claims when non-atomic ----
def test_no_guaranteed_language_anywhere():
    _, legs = pair([("0.45", 500)], [("0.50", 500)])
    payload = json.dumps(run(legs).model_dump(mode="json"))
    indicative = indicative_candidate(
        kind="paired_outcome",
        venue_ids=["polymarket"],
        condition_ids=[CID],
        gross_gap=Decimal("0.05"),
        as_of=NOW,
        detail={},
    )
    payload += json.dumps(indicative.model_dump(mode="json"))
    assert not re.search(r"(?<!not )guarantee", payload, re.I)
    assert "no profit is assured" in payload.lower()
    assert '"profit_assured":false' in payload.replace(" ", "")


def test_package_contains_no_order_or_signing_path():
    root = Path(__file__).resolve().parents[1] / "app/services/analytics/arbitrage"
    forbidden = (
        "pre_trade_gate",
        "live_trading_policy",
        "py_clob_client",
        "create_order",
        "post_order",
        "private_key",
        "submit_order",
        "sign_order",
    )
    for path in root.glob("*.py"):
        text = path.read_text()
        for needle in forbidden:
            assert needle not in text, f"{path.name} references {needle}"


# ---- AC: time-stamped reproducible quote ----
def test_reproducible_and_time_stamped():
    _, legs = pair([("0.45", 500)], [("0.50", 500)])
    first, second = run(legs), run(legs)
    assert first.model_dump_json() == second.model_dump_json()
    assert first.input_digest == second.input_digest
    assert first.candidate_id == first.input_digest[:16]
    assert first.as_of == NOW and first.stale_at is not None
    assert all(q.book_hash and q.book_as_of == NOW for q in first.leg_quotes)
    _, other = pair([("0.45", 500)], [("0.51", 500)])
    assert run(other).input_digest != first.input_digest


def test_paper_only_flags_always_set():
    _, legs = pair([("0.45", 500)], [("0.50", 500)])
    cand = run(legs)
    assert cand.paper_only is True and cand.live_execution_enabled is False
    assert cand.profit_assured is False and "NON_ATOMIC_LEGS" in cand.badges


# ---- cross venue ----
def _cross(**right_kw):
    sa, sb = spec(cid="0xA", yes="1", no="2"), spec(cid="0xB", yes="3", no="4", rule_hash="h2")
    common = {
        "event_key": "fixture-us-election-2028",
        "oracle": "AP",
        "cutoff": NOW,
        "yes_means": "candidate wins",
        "currency": "USDC",
        "settlement": "on-chain",
    }
    left = rules(sa, venue="polymarket", **common)
    right = rules(sb, venue="kalshi", **{**common, **right_kw})
    legs = [leg("A", sa, "YES", [("0.40", 100)]), leg("B", sb, "NO", [("0.50", 100)])]
    return legs, [left, right]


def test_cross_venue_verified_equivalent_needs_all_costs():
    legs, pair_rules = _cross()
    assert run(legs, rule_pair=pair_rules, costs=ZERO).state == "DEPTH_VERIFIED"
    ready = run(legs, rule_pair=pair_rules, costs=FULL)
    assert ready.state == "EXECUTION_READY" and ready.kind == "cross_venue"
    assert ready.payoff_matrix.verified_equivalent and ready.payoff_matrix.min_total == 2 - 1
    assert "CROSS_VENUE_SETTLEMENT_TIMING_AND_DISPUTE_RISK" in ready.settlement_risk


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("oracle", "Reuters"),
        ("cutoff", NOW.replace(day=2)),
        ("yes_means", "candidate loses"),
        ("currency", "USD"),
        ("settlement", "off-chain"),
        ("event_key", "other"),
    ],
)
def test_cross_venue_mismatch_rejected(field, value):
    legs, pair_rules = _cross(**{field: value})
    cand = run(legs, rule_pair=pair_rules, costs=FULL)
    assert cand.state == "INVALID"
    assert f"{field.upper()}_MISMATCH" in cand.reasons
    assert cand.executable_profit is None and "REVIEW_ONLY" not in cand.badges


def test_cross_venue_unknown_fields_stay_review_only():
    legs, pair_rules = _cross(oracle=None)
    cand = run(legs, rule_pair=pair_rules, costs=FULL)
    assert cand.state == "INVALID" and "REVIEW_ONLY" in cand.badges
    assert "ORACLE_UNVERIFIED" in cand.reasons


def test_title_overlap_alone_is_not_parity():
    a = rules(spec(cid="0xA"), venue="polymarket", rules_text="Will X win?")
    b = rules(spec(cid="0xB", yes="3", no="4"), venue="kalshi", rules_text="Will X win?")
    eq = verify_cross_venue(a, b)
    assert eq.status == "REVIEW_ONLY"


def test_play_money_leg_non_executable():
    legs, pair_rules = _cross()
    pair_rules[1] = pair_rules[1].model_copy(update={"is_real_money": False})
    cand = run(legs, rule_pair=pair_rules, costs=FULL)
    assert cand.state == "INVALID" and "CROSS_VENUE_NON_EXECUTABLE" in cand.reasons
    assert validate_binary_complement(pair_rules[1]) == ["PLAY_MONEY_NOT_EXECUTABLE"]


def test_unverified_cross_matrix_worst_case_is_zero():
    matrix = build_payoff_matrix([("A", "YES", 0), ("B", "NO", 1)], equivalent=False)
    assert matrix.min_total == 0 and matrix.max_total == 2
    assert not matrix.verified_equivalent
    assert len(matrix.scenarios) == 9
    verified = build_payoff_matrix([("A", "YES", 0), ("B", "NO", 1)], equivalent=True)
    assert verified.min_total == 1 and verified.max_total == 1


# ---- property-style invariants over random books ----
def test_random_books_never_overstate_edge():
    rng = random.Random(9)  # noqa: S311 - deterministic test data
    for _ in range(300):
        ya = sorted(
            (Decimal(rng.randint(5, 95)) / 100, Decimal(rng.randint(1, 300))) for _ in range(3)
        )
        na = sorted(
            (Decimal(rng.randint(5, 95)) / 100, Decimal(rng.randint(1, 300))) for _ in range(3)
        )
        fee = str(rng.choice([0, 50, 100, 300]))
        _, legs = pair(
            [(str(p), str(s)) for p, s in ya],
            [(str(p), str(s)) for p, s in na],
            fee=fee,
        )
        cand = run(legs, size=str(rng.randint(1, 400)))
        if cand.state in {"EXECUTION_READY", "PARTIAL"}:
            b = cand.net_bound
            assert b.net_bound > 0
            assert b.net_bound == b.size - sum(q.net_cash for q in cand.leg_quotes)
            assert sum(q.vwap for q in cand.leg_quotes) < 1
            assert cand.size <= min(q.displayed_capacity for q in cand.leg_quotes)
        else:
            assert cand.executable_profit is None
            if cand.net_bound is not None:
                assert cand.net_bound.net_bound <= 0 or cand.net_bound.unknown_costs
