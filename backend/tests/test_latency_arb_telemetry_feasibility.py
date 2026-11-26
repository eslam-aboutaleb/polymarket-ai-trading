"""Latency-arb telemetry and feasibility (issue #12): real objects, no network."""

from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth.auth import get_current_user_from_token
from app.main import app
from app.services.trading import latency_arb_feasibility as fz
from app.services.trading import latency_arb_telemetry as tm


def trace(trace_id="t", gaps_ms=(2, 3, 1, 1, 1, 4, 5, 20), outcome="filled", upto=None, **kw):
    stamps, now = {}, 100.0
    for stage, gap in zip(tm.STAGES, (0, *gaps_ms), strict=False):
        now += gap / 1000.0
        stamps[stage] = now
        if stage == upto:
            break
    return tm.LatencyTrace(trace_id, stamps, outcome=outcome, **kw)


def test_components_sum_to_total_and_are_correlated_to_the_stamps():
    t = trace()
    assert t.is_valid()
    assert t.total_ms() == pytest.approx(37.0)
    assert t.components_sum_to_total()
    assert t.stage_ms()["acknowledged"] == pytest.approx(5.0)


def test_backwards_stamps_are_invalid_and_excluded_from_percentiles():
    bad = tm.LatencyTrace("bad", {"event_received": 5.0, "signal_ready": 4.0})
    assert not bad.is_valid()
    unknown_stage = tm.LatencyTrace("x", {"event_received": 1.0, "bogus": 2.0})
    assert not unknown_stage.is_valid()
    summary = tm.summarize([trace(), bad])
    assert summary["valid_trace_count"] == 1 and summary["invalid_trace_count"] == 1
    assert summary["stage_ms"]["signal_ready"]["n"] == 1


def test_percentiles_use_nearest_rank_and_cover_p50_p95_p99():
    assert tm.percentile([1, 2, 3, 4, 100], 50) == 3
    assert tm.percentile([float(i) for i in range(1, 101)], 95) == 95
    assert tm.percentile([float(i) for i in range(1, 101)], 99) == 99
    assert tm.percentile([], 50) is None
    traces = [trace(f"t{i}", gaps_ms=(i, 1, 1, 1, 1, 1, 1, 1)) for i in range(1, 101)]
    s = tm.summarize(traces)
    assert s["stage_ms"]["signal_ready"]["p95"] == pytest.approx(95.0)
    assert s["coherent_components"] is True


def test_dropped_signals_and_unknown_clock_offset_are_counted_not_hidden():
    dropped = trace("d", outcome="rejected", upto="risk_approved", reject_reason="STALE_BOOK")
    ok = trace("ok", wall_clock_uncertainty_ms=12.0)
    s = tm.summarize([dropped, ok])
    assert s["dropped_before_submit"] == 1
    assert s["unknown_clock_offset"] == 1
    assert s["outcomes"] == {"rejected": 1, "filled": 1}
    assert s["submit_to_ack_ms"]["n"] == 1


def test_store_is_bounded():
    store = tm.TelemetryStore(capacity=3)
    for i in range(5):
        store.record(trace(f"t{i}"))
    assert [t.trace_id for t in store.snapshot()] == ["t2", "t3", "t4"]


# ── feasibility ──────────────────────────────────────────────────────────────


def rep(p="0.70", orig="0.55", arrival="0.60", fee="0.01", filled=True, trace_id="r"):
    return fz.EdgeDecayReport(
        trace_id, D(p), D(orig), D(arrival) if arrival else None, D(fee) if fee else None, filled
    )


def test_edge_uses_the_arrival_quote_not_the_stale_trigger_quote():
    r = rep()
    assert r.original_edge == D("0.15")  # looks great at the trigger
    assert r.net_edge == D("0.70") - D("0.60") - D("0.01")  # what was obtainable
    assert r.decay == D("0.05")


def test_missed_fills_are_retained_as_misses_with_no_invented_edge():
    reports = [rep(), rep(filled=False, arrival="0.90", trace_id="m")]
    assert reports[1].missed_fill and reports[1].net_edge is None
    out = fz.feasibility(reports, canary_approved=True)
    assert out["missed_fills"] == 1 and out["miss_rate"] == 0.5 and out["n"] == 2
    assert out["mean_net_edge_at_arrival"] == "0.09"


def test_non_positive_net_edge_blocks_live_promotion_even_when_approved():
    out = fz.feasibility([rep(arrival="0.70")], canary_approved=True)
    assert out["live_promotion_allowed"] is False
    assert "NET_EDGE_NOT_POSITIVE" in out["reasons"]


def test_canary_needs_explicit_approval_even_with_positive_edge():
    out = fz.feasibility([rep()])
    assert out["live_promotion_allowed"] is False
    assert out["reasons"] == ["CANARY_NOT_EXPLICITLY_APPROVED"]
    assert fz.feasibility([rep()], canary_approved=True)["live_promotion_allowed"] is True


def test_unknown_quote_or_fee_blocks_and_empty_input_is_not_a_pass():
    unknown = fz.feasibility([rep(arrival=None), rep()], canary_approved=True)
    assert unknown["live_promotion_allowed"] is False
    assert "FILLS_WITH_UNKNOWN_QUOTE_OR_FEE" in unknown["reasons"]
    empty = fz.feasibility([], canary_approved=True)
    assert empty["live_promotion_allowed"] is False and empty["reasons"][0] == "NO_PRICED_FILLS"
    assert "profitable" in empty["note"]


# ── endpoints ────────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    tm.STORE.clear()
    fz.REPORTS.clear()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user_from_token, None)
        tm.STORE.clear()
        fz.REPORTS.clear()


def test_telemetry_endpoint_reports_recorded_traces(client):
    tm.STORE.record(trace("a"))
    body = client.get("/api/v1/latency-arb/telemetry").json()
    assert body["trace_count"] == 1
    assert body["total_ms"]["p50"] == pytest.approx(37.0)


def test_feasibility_endpoint_never_allows_live_without_approval(client):
    fz.REPORTS.record(rep())
    body = client.get("/api/v1/latency-arb/feasibility").json()
    assert body["fills"] == 1
    assert body["live_promotion_allowed"] is False
    assert "CANARY_NOT_EXPLICITLY_APPROVED" in body["reasons"]


def test_endpoints_require_authentication():
    app.dependency_overrides.pop(get_current_user_from_token, None)
    plain = TestClient(app)
    assert plain.get("/api/v1/latency-arb/telemetry").status_code in (401, 403)
    assert plain.get("/api/v1/latency-arb/feasibility").status_code in (401, 403)
