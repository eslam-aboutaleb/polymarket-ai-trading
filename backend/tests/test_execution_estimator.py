import ast
import inspect
from datetime import timedelta
from decimal import Decimal

import pytest
from test_binary_book import NOW, delta, snapshot, spec

from app.services.market_data.binary_book import Level, reconstruct
from app.services.trading.execution import execution_estimator as module
from app.services.trading.execution.execution_estimator import (
    CohortOrder,
    EstimateRequest,
    ShortfallInput,
    compare_estimate_to_actual,
    compare_matched_cohorts,
    estimate_execution,
    implementation_shortfall,
    revalidate_estimate,
)

D = Decimal
LATER = NOW + timedelta(seconds=2)


def book(*events, as_of=NOW):
    return reconstruct(spec(), "11", list(events) or [snapshot()], as_of)


def req(**changes):
    values = {
        "order_intent_id": "intent-1",
        "side": "BUY",
        "shares": "5",
        "limit_price": "0.60",
        "fee_bps": "100",
        "fee_version": "flat-test",
        **changes,
    }
    return EstimateRequest(**values)


def est(request, b=None, now=NOW):
    return estimate_execution(spec(), b or book(), request, now)


def test_buy_inside_touch_prices_exactly_and_is_read_only():
    e = est(req())
    assert e.status == "modeled" and e.fill_status == "FULL"
    assert e.executable_vwap == D("0.5") and e.fees == D("0.025")
    assert e.net_cash == D("2.525") and e.depth_coverage == 1
    assert (e.best_bid, e.best_ask, e.arrival_mid) == (D("0.4"), D("0.5"), D("0.45"))
    assert e.expected_price_impact == 0
    assert abs(e.expected_cost_vs_mid_bps - D("0.05") / D("0.45") * 10000) < D("1e-12")
    assert e.book_snapshot_id == book().book_hash and e.quote_expiry == book().expires_at
    assert e.live_order_action is False and e.fill_probability is None
    assert e.nonfill_cost is None and e.estimate_id and len(e.estimate_id) == 64


def test_larger_size_walks_deeper_and_cost_is_monotonic():
    sizes = ["1", "5", "10", "11", "20", "30"]
    estimates = [est(req(shares=s)) for s in sizes]
    assert all(e.fill_status == "FULL" for e in estimates)
    vwaps = [e.executable_vwap for e in estimates]
    assert vwaps == sorted(vwaps) and vwaps[0] == D("0.5") and vwaps[-1] > vwaps[0]
    assert est(req(shares="20")).executable_vwap == D("0.55")
    impacts = [e.expected_price_impact for e in estimates]
    assert impacts == sorted(impacts) and impacts[0] == 0 and impacts[-1] > impacts[1]
    assert est(req(shares="20")).expected_price_impact == D("0.05")


def test_insufficient_depth_is_never_priced_as_a_full_fill():
    partial = est(req(shares="40"))
    assert partial.fill_status == "PARTIAL" and partial.fillable_quantity == 30
    assert partial.residual_quantity == 10 and partial.depth_coverage == D("0.75")
    assert partial.reason == "PARTIAL_DEPTH_WITHIN_MAX_ACCEPTABLE_PRICE"
    fok = est(req(shares="40", mode="FOK"))
    assert fok.status == "modeled" and fok.fill_status == "NONE"
    assert fok.executable_vwap is None and fok.net_cash is None and fok.fees is None
    assert fok.reason == "FOK_WOULD_NOT_FILL_INSUFFICIENT_DEPTH_WITHIN_LIMIT"
    assert est(req(shares="20", mode="FOK")).fill_status == "FULL"
    bounded = est(req(shares="20", limit_price="0.55"))
    assert bounded.fillable_quantity == 10 and bounded.executable_vwap == D("0.5")
    none = est(req(shares="5", limit_price="0.40"))
    assert none.fill_status == "NONE" and none.executable_vwap is None


def test_sell_walks_bids_and_slippage_bound_tightens_limit():
    sell = est(req(side="SELL", shares="5", limit_price="0.30"))
    assert sell.executable_vwap == D("0.4") and sell.net_cash == D("1.98")
    assert abs(sell.expected_cost_vs_mid_bps - D("0.05") / D("0.45") * 10000) < D("1e-12")
    bounded = est(req(shares="20", limit_price="0.99", max_slippage_bps="500"))
    # 5% above the 0.50 touch rounds down to tick 0.52: only the 0.50 level qualifies.
    assert bounded.max_acceptable_price == D("0.52") and bounded.fillable_quantity == 10


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"limit_price": "0.555"}, "REJECT_OFF_TICK_PRICE"),
        ({"shares": "5.005"}, "REJECT_OFF_LOT_SIZE"),
        ({"shares": "0.5"}, "REJECT_BELOW_MINIMUM_ORDER_SIZE"),
        ({"limit_price": None}, "REJECT_MAX_ACCEPTABLE_PRICE_REQUIRED"),
    ],
)
def test_invalid_tick_size_or_unbounded_orders_reject_without_pricing(changes, reason):
    e = est(req(**changes))
    assert e.status == "rejected" and e.reason == reason
    assert e.executable_vwap is None and e.net_cash is None


def test_missing_fee_stale_crossed_and_wrong_identity_abstain():
    assert est(req(fee_bps=None)).reason == "FEE_POLICY_UNKNOWN"
    assert est(req(), now=NOW + timedelta(seconds=61)).reason == "QUOTE_EXPIRED_OR_FROM_FUTURE"
    assert est(req(), now=NOW - timedelta(seconds=1)).reason == "QUOTE_EXPIRED_OR_FROM_FUTURE"
    crossed = book(snapshot(bids=[Level(price="0.7", shares="1")]))
    assert est(req(), crossed).reason == "BOOK_NOT_USABLE"
    empty = book(snapshot(asks=[]))
    assert est(req(), empty).status == "abstain"
    other = spec().model_copy(update={"rule_hash": "other"})
    assert estimate_execution(other, book(), req(), NOW).reason == "BOOK_IDENTITY_MISMATCH"
    with pytest.raises(ValueError):
        estimate_execution(spec(), book(), req(), NOW.replace(tzinfo=None))


def test_maker_rest_is_unmodeled_never_a_fill_and_cannot_cross():
    rest = est(req(mode="MAKER_REST", limit_price="0.45", maker_fee_bps="0"))
    assert rest.fill_status == "UNMODELED" and rest.fill_probability is None
    assert rest.executable_vwap == D("0.45") and rest.fees == 0
    assert rest.maker_queue["status"] == "unavailable"
    assert rest.reason == "MAKER_FILL_UNMODELED_NO_QUEUE_EVIDENCE"
    assert est(req(mode="MAKER_REST", limit_price="0.50", maker_fee_bps="0")).reason == (
        "REJECT_WOULD_CROSS_SPREAD_AS_MAKER"
    )
    assert est(req(mode="MAKER_REST", limit_price="0.45")).reason == "MAKER_FEE_POLICY_UNKNOWN"
    assert est(req(mode="MAKER_REST", limit_price=None, maker_fee_bps="0")).status == "rejected"


def test_quote_changes_invalidate_a_prior_estimate():
    prior = est(req())
    same = revalidate_estimate(prior, book(), NOW + timedelta(seconds=1))
    assert same.valid and same.reasons == []
    moved = book(snapshot(), delta(), as_of=LATER)  # the 0.50 ask level disappears
    result = revalidate_estimate(prior, moved, NOW + timedelta(seconds=3))
    assert not result.valid
    assert {"BEST_QUOTE_CHANGED", "BOOK_DEPTH_CHANGED"} <= set(result.reasons)
    deeper = book(snapshot(bids=[Level(price="0.4", shares="12")]))
    assert revalidate_estimate(prior, deeper, NOW).reasons == ["BOOK_DEPTH_CHANGED"]
    expired = revalidate_estimate(prior, book(), NOW + timedelta(seconds=61))
    assert "QUOTE_EXPIRED" in expired.reasons and not expired.valid
    assert "CURRENT_BOOK_NOT_USABLE" in revalidate_estimate(prior, None, NOW).reasons
    assert not revalidate_estimate(est(req(limit_price=None)), book(), NOW).valid


def shortfall(**changes):
    values = {
        "side": "BUY",
        "ordered_shares": "10",
        "filled_shares": "10",
        "fill_vwap": "0.52",
        "fees_paid": "0.05",
        "arrival_mid": "0.50",
        "benchmark_at": NOW,
        **changes,
    }
    return implementation_shortfall(ShortfallInput(**values))


def test_implementation_shortfall_decomposition_and_missing_components():
    full = shortfall()
    assert full.execution_cost == D("0.20") and full.fee_cost == D("0.05")
    assert full.missed_trade_cost == 0 and full.total_cost == D("0.25")
    assert full.price_shortfall_bps == D("400") and full.total_shortfall_bps == D("500")
    assert full.fill_rate == 1 and full.components_missing == []
    sell = shortfall(side="SELL", fill_vwap="0.48")
    assert sell.execution_cost == D("0.20") and sell.price_shortfall_bps == D("400")
    partial = shortfall(filled_shares="4", fees_paid=None)
    assert partial.fill_rate == D("0.4") and partial.fee_cost is None
    assert set(partial.components_missing) == {"fee_cost", "missed_trade_cost"}
    later = shortfall(filled_shares="4", later_mid="0.60", later_at=NOW + timedelta(minutes=5))
    assert later.missed_trade_cost == D("0.60") and "missed_trade_cost" in later.components_observed
    none = shortfall(filled_shares="0", fill_vwap=None, fees_paid=None)
    assert none.price_shortfall_bps is None and none.fill_rate == 0
    with pytest.raises(ValueError):
        shortfall(filled_shares="11")
    with pytest.raises(ValueError):
        shortfall(later_mid="0.6")
    with pytest.raises(ValueError):
        shortfall(fill_vwap=None)


def test_estimated_vs_actual_shortfall_share_one_benchmark_timestamp():
    e = est(req(shares="20"))
    ok = implementation_shortfall(
        ShortfallInput(
            side="BUY",
            ordered_shares="20",
            filled_shares="20",
            fill_vwap="0.56",
            arrival_mid=e.arrival_mid,
            benchmark_at=e.benchmark_at,
        )
    )
    gap = compare_estimate_to_actual(e, ok)
    assert gap.status == "compared"
    assert gap.error_bps == ok.price_shortfall_bps - e.expected_cost_vs_mid_bps
    assert gap.error_bps > 0  # filled worse than the displayed walk
    skewed = ok.model_copy(update={"benchmark_at": e.benchmark_at + timedelta(seconds=1)})
    assert compare_estimate_to_actual(e, skewed).reason == "BENCHMARK_TIMESTAMP_OR_PRICE_MISMATCH"
    other_mid = ok.model_copy(update={"arrival_mid": D("0.46")})
    assert compare_estimate_to_actual(e, other_mid).status == "abstain"
    unfilled = shortfall(
        filled_shares="0", fill_vwap=None, arrival_mid=e.arrival_mid, benchmark_at=e.benchmark_at
    )
    assert compare_estimate_to_actual(e, unfilled).reason == "ACTUAL_HAS_NO_FILL"
    assert compare_estimate_to_actual(est(req(limit_price=None)), ok).status == "abstain"


def orders(costs, fills):
    return [
        CohortOrder(decision_key=f"d{i}", total_shortfall_bps=c, fill_rate=f)
        for i, (c, f) in enumerate(zip(costs, fills, strict=True))
    ]


def test_matched_cohorts_report_cost_and_fill_rate_only_on_shared_decisions():
    n = 40
    baseline = orders([D(100 + i) for i in range(n)], [D("0.9")] * n)
    policy = orders([D(80 + i) for i in range(n)], [D("0.7")] * n)
    policy = policy[:-2] + [
        CohortOrder(decision_key="extra", total_shortfall_bps=D(1), fill_rate=D(1))
    ]
    result = compare_matched_cohorts(baseline, policy)
    assert result.status == "compared" and result.matched_pairs == n - 2
    assert result.unmatched_baseline == 2 and result.unmatched_policy == 1
    assert result.mean_cost_delta_bps == D(-20) and result.p50_cost_delta_bps == D(-20)
    assert result.p95_cost_delta_bps == D(-20)
    assert result.mean_fill_rate_delta == D("-0.2")
    assert result.profitability_claim is False


def test_cohort_comparison_refuses_small_empty_and_duplicate_samples():
    small = compare_matched_cohorts(orders([D(1)] * 5, [D(1)] * 5), orders([D(2)] * 5, [D(1)] * 5))
    assert small.status == "insufficient_sample" and small.matched_pairs == 5
    disjoint = compare_matched_cohorts(
        orders([D(1)], [D(1)]),
        [CohortOrder(decision_key="x", total_shortfall_bps=D(1), fill_rate=D(1))],
    )
    assert disjoint.reason == "NO_MATCHED_DECISIONS"
    dup = [CohortOrder(decision_key="a", total_shortfall_bps=D(1), fill_rate=D(1))] * 2
    assert compare_matched_cohorts(dup, dup).reason == "DUPLICATE_DECISION_KEY"
    spread = compare_matched_cohorts(
        orders([D(0)] * 100, [D(1)] * 100),
        orders([D(i) for i in range(100)], [D(1)] * 100),
    )
    assert spread.p50_cost_delta_bps == D("49.5") and spread.p95_cost_delta_bps == D("94.05")


def test_module_cannot_submit_sign_or_touch_io():
    tree = ast.parse(inspect.getsource(module))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    forbidden = ("httpx", "requests", "sqlalchemy", "py_clob_client", "pre_trade_gate", "Session")
    assert not [n for n in names if any(f in n for f in forbidden)]
    for word in ("post_order", "create_order", "place_live_order", "cancel_order", "sign_order"):
        assert not [n for n in names if word in n]
    assert not [n for n in names if n.endswith(".live") or n == "live"]
