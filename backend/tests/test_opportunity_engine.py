import ast
import inspect
from datetime import timedelta
from decimal import Decimal

from test_binary_book import NOW, snapshot, spec

from app.services.market_data.binary_book import Level, reconstruct
from app.services.opportunities import opportunity_engine as module
from app.services.opportunities.opportunity_engine import (
    CandidateRequest,
    ForecastInput,
    evaluate_opportunity,
    rank_opportunities,
    size_sensitivity,
)

D = Decimal


def yes_book(**changes):
    return reconstruct(spec(), "11", [snapshot(**changes)], NOW)


def no_book():
    return reconstruct(spec(), "22", [snapshot(token_id="22")], NOW)


def forecast(**changes):
    values = {
        "forecast_id": "f1",
        "condition_id": "condition",
        "yes_probability": "0.70",
        "interval_low": "0.65",
        "interval_high": "0.75",
        "forecast_at": NOW - timedelta(minutes=5),
        "calibrated": True,
        "rule_hash": "rules",
        **changes,
    }
    return ForecastInput(**values)


def request(**changes):
    return CandidateRequest(
        **{"outcome": "YES", "shares": "5", "fee_bps": "100", "fee_version": "t", **changes}
    )


def evaluate(f=None, r=None, book=None, now=NOW):
    return evaluate_opportunity(spec(), book or yes_book(), f or forecast(), r or request(), now)


def test_yes_math_is_executable_price_not_midpoint_and_verified_only_with_full_evidence():
    o = evaluate()
    assert o.status == "VERIFIED" and o.reasons == []
    assert o.executable_vwap == D("0.5") and o.fees == D("0.025")
    assert o.net_ev == D("0.975")  # 5 * (0.70 - 0.50 - 0.005)
    assert o.conservative_net_ev == D("0.725")
    assert o.capital_required == D("2.525")
    assert abs(o.net_ev_per_dollar - D("0.975") / D("2.525")) < D("1e-12")
    assert o.guaranteed_profit is False and o.mode == "research_only_no_order"
    assert o.snapshot_id == yes_book().book_hash and o.opportunity_id


def test_no_side_uses_the_complement_probability_and_the_no_token_book():
    o = evaluate(
        forecast(yes_probability="0.30", interval_low="0.25", interval_high="0.35"),
        request(outcome="NO"),
        no_book(),
    )
    assert o.status == "VERIFIED" and o.predicted_probability == D("0.70")
    assert o.uncertainty_interval == (D("0.65"), D("0.75")) and o.token_id == "22"
    assert o.net_ev == D("0.975")
    # Same YES forecast must not make the YES token look like a buy.
    wrong = evaluate(forecast(yes_probability="0.30", interval_low="0.25", interval_high="0.35"))
    assert wrong.status == "INELIGIBLE" and wrong.reasons == ["NO_NET_EDGE_AFTER_EXECUTABLE_COST"]
    # A NO request against the YES book is a token mix-up and is refused.
    mix = evaluate(r=request(outcome="NO"))
    assert mix.reasons == ["TOKEN_OR_CONDITION_MISMATCH"]


def test_larger_size_changes_vwap_and_can_reject_the_candidate():
    f = forecast(yes_probability="0.55", interval_low="0.50", interval_high="0.60")
    rows = size_sensitivity(spec(), yes_book(), f, request(), [D(5), D(10), D(20), D(30)], NOW)
    assert [r.status for r in rows][:2] == ["INDICATIVE", "INDICATIVE"]
    assert rows[2].status == "INELIGIBLE" and rows[3].status == "INELIGIBLE"
    vwaps = [r.executable_vwap for r in rows if r.executable_vwap]
    assert vwaps == sorted(vwaps) and rows[0].executable_vwap == D("0.5")
    assert rows[0].opportunity_id != rows[1].opportunity_id


def test_insufficient_depth_is_never_priced_as_a_full_fill():
    o = evaluate(r=request(shares="40"))
    assert o.status == "INELIGIBLE" and o.reasons == ["INSUFFICIENT_DISPLAYED_DEPTH"]
    assert o.net_ev is None and o.executable_vwap is None


def test_stale_sources_invalidate():
    old_forecast = evaluate(forecast(forecast_at=NOW - timedelta(hours=2)))
    assert old_forecast.status == "STALE" and old_forecast.reasons == ["FORECAST_STALE"]
    late = evaluate(
        forecast(forecast_at=NOW + timedelta(seconds=30)), now=NOW + timedelta(seconds=61)
    )
    assert late.status == "STALE"
    stale_book = evaluate(now=NOW + timedelta(seconds=61))
    assert stale_book.status == "STALE"
    future = evaluate(forecast(forecast_at=NOW + timedelta(seconds=1)))
    assert future.reasons == ["FORECAST_FROM_FUTURE"]
    gapped = reconstruct(spec(), "11", [snapshot(bids=[Level(price="0.7", shares="1")])], NOW)
    assert evaluate(book=gapped).status == "INELIGIBLE"


def test_abstained_or_missing_forecasts_are_never_opportunities():
    for f in (
        forecast(abstained=True),
        forecast(yes_probability=None, interval_low=None, interval_high=None),
    ):
        o = evaluate(f)
        assert o.status == "INELIGIBLE" and o.reasons == ["FORECAST_ABSTAINED"]
    ranked, filtered = rank_opportunities([evaluate(forecast(abstained=True))])
    assert ranked == [] and filtered[0][1] == "FORECAST_ABSTAINED"


def test_fees_and_missing_fee_policy_remove_thin_edges():
    thin = evaluate(forecast(yes_probability="0.505", interval_low="0.50", interval_high="0.51"))
    assert thin.status == "INELIGIBLE" and thin.reasons == ["NO_NET_EDGE_AFTER_EXECUTABLE_COST"]
    no_fee = evaluate(r=request(fee_bps=None))
    assert no_fee.status == "INELIGIBLE" and no_fee.reasons == ["FEE_POLICY_UNKNOWN"]


def test_missing_calibration_interval_or_rules_stay_indicative():
    o = evaluate(forecast(calibrated=False, interval_low=None, interval_high=None, rule_hash=None))
    assert o.status == "INDICATIVE"
    assert set(o.reasons) == {
        "NO_UNCERTAINTY_INTERVAL",
        "FORECAST_NOT_CALIBRATED",
        "RULES_NOT_VERIFIED",
    }
    wide = evaluate(forecast(interval_low="0.45", interval_high="0.80"))
    assert wide.status == "INDICATIVE" and wide.reasons == ["CONSERVATIVE_EDGE_NOT_POSITIVE"]
    assert wide.conservative_net_ev < 0 < wide.net_ev
    assert evaluate(forecast(rule_hash="other")).reasons == ["RULE_HASH_MISMATCH"]


def test_snapshot_replay_is_exact_and_id_tracks_every_input():
    assert evaluate().model_dump_json() == evaluate().model_dump_json()
    base = evaluate().opportunity_id
    assert evaluate(r=request(shares="6")).opportunity_id != base
    assert evaluate(book=yes_book(bids=[Level(price="0.4", shares="11")])).opportunity_id != base
    assert evaluate(forecast(forecast_id="f2")).opportunity_id != base


def test_ranking_exposes_components_and_orders_by_net_ev_per_dollar():
    strong = evaluate(
        forecast(forecast_id="a", yes_probability="0.80", interval_low="0.75", interval_high="0.85")
    )
    weak = evaluate(
        forecast(forecast_id="b", yes_probability="0.60", interval_low="0.55", interval_high="0.65")
    )
    indicative = evaluate(
        forecast(forecast_id="c", yes_probability="0.90", interval_low=None, interval_high=None)
    )
    dead = evaluate(forecast(forecast_id="d", abstained=True))
    ranked, filtered = rank_opportunities([weak, dead, indicative, strong])
    # Verified outranks indicative even when the indicative edge looks larger.
    assert [o.forecast_id for o in ranked] == ["a", "b", "c"]
    assert [o.forecast_id for o, _ in filtered] == ["d"]
    assert "fee_per_share" in ranked[0].components
    only, cut = rank_opportunities([weak, strong], min_edge=D("0.3"))
    assert [o.forecast_id for o in only] == ["a"] and cut[0][1] == "BELOW_MIN_EDGE"


def test_engine_cannot_submit_sign_or_touch_io():
    names = set()
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(a.name for a in node.names)
    for bad in (
        "httpx",
        "sqlalchemy",
        "py_clob_client",
        "post_order",
        "create_order",
        "place_live_order",
        "pre_trade_gate",
        "live_trading_policy",
        "Session",
    ):
        assert not [n for n in names if bad in n]
