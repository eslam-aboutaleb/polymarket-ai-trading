"""Golden-fixture, point-in-time, API, and MCP tests for resolution-rule-analysis (#16)."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.main import app as fastapi_app
from app.models import Assessment, Base, ResolutionRuleSnapshot, TradeHistory
from app.services.intelligence.forecast_resolution import verify_rule_before_settlement
from app.services.intelligence.resolution_rules import (
    PayoffObservation,
    detect_rule_change,
    get_rule_snapshot_as_of,
    invalidate_dependent_forecasts,
    parse_resolution_rule,
    record_rule_snapshot,
    resolve_payoff,
)
from app.utils.database import get_db

CID = "0x" + "a" * 64
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _base_market(**overrides) -> dict:
    market = {
        "condition_id": CID,
        "question": "Will BTC be at least $100,000?",
        "description": (
            "This market resolves to Yes if Chainlink (BTC/USD) is at least $100,000.00 "
            "on 2026-12-31 23:59:00 UTC."
        ),
        "end_date_iso": "2026-12-31T23:59:00Z",
        "resolutionSource": "Chainlink",
        "tokens": [
            {"outcome": "Yes", "token_id": "101"},
            {"outcome": "No", "token_id": "202"},
        ],
    }
    market.update(overrides)
    return market


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_strict_gt_vs_non_strict_gte_boundary_equality():
    gte_spec = parse_resolution_rule(CID, _base_market(), NOW)
    assert gte_spec.review_status == "verified"
    assert gte_spec.comparator == ">="
    assert gte_spec.threshold == Decimal("100000.00")
    assert gte_spec.threshold_precision == 2
    assert gte_spec.units == "USD"
    assert gte_spec.execution_eligible is False

    gt_market = _base_market(
        question="Will BTC be strictly greater than $100,000?",
        description=(
            "Resolves Yes if Chainlink (BTC/USD) is strictly greater than $100,000.00 "
            "on 2026-12-31 23:59:00 UTC."
        ),
    )
    gt_spec = parse_resolution_rule(CID, gt_market, NOW)
    assert gt_spec.review_status == "verified"
    assert gt_spec.comparator == ">"

    obs = PayoffObservation(
        observed_at=datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
        source_at=gte_spec.cutoff_utc,
        oracle="Chainlink",
        oracle_symbol="BTC/USD",
        value=Decimal("100000.00"),
    )
    assert resolve_payoff(gte_spec, obs).outcome == "YES"
    assert resolve_payoff(gt_spec, obs).outcome == "NO"


def test_evidence_span_exact_traceability():
    market = _base_market()
    spec = parse_resolution_rule(CID, market, NOW)
    assert spec.subject == "BTC"
    assert spec.metric == "oracle_series:BTC/USD"
    assert spec.evidence_spans
    for span in spec.evidence_spans:
        target = market["description"] if span.field.startswith("rules.") else market["question"]
        assert target[span.start : span.end] == span.quote


def test_lt_vs_lte_and_source_update_without_token_flip():
    lte_spec = parse_resolution_rule(
        CID,
        _base_market(
            question="Will BTC be at most $100,000?",
            description=(
                "Resolves Yes if Chainlink (BTC/USD) is at most $100,000.00 "
                "on 2026-12-31 23:59:00 UTC."
            ),
        ),
        NOW,
    )
    lt_spec = parse_resolution_rule(
        CID,
        _base_market(
            question="Will BTC be strictly less than $100,000?",
            description=(
                "Resolves Yes if Chainlink (BTC/USD) is strictly less than $100,000.00 "
                "on 2026-12-31 23:59:00 UTC."
            ),
        ),
        NOW,
    )
    obs = PayoffObservation(
        observed_at=datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
        source_at=lte_spec.cutoff_utc,
        oracle="Chainlink",
        oracle_symbol="BTC/USD",
        value=Decimal("100000.00"),
    )
    assert resolve_payoff(lte_spec, obs).outcome == "YES"
    assert resolve_payoff(lt_spec, obs).outcome == "NO"

    updated = parse_resolution_rule(
        CID,
        _base_market(
            description=(
                "This market resolves to Yes if Chainlink (BTC/USD) is at least $100,000.00 "
                "on 2026-12-31 23:59:00 UTC. Source update: primary feed only."
            )
        ),
        NOW + timedelta(minutes=5),
    )
    report = detect_rule_change(parse_resolution_rule(CID, _base_market(), NOW), updated)
    assert report.changed is True
    assert report.invalidates_forecasts is True
    assert "RULE_TEXT_CHANGED" in report.reasons


def test_daylight_saving_cutoff_ambiguity_and_mismatch():
    # EST used in July (when EDT applies in America/New_York) must flag mismatch and abstain.
    dst_mismatch = _base_market(
        description=(
            "Resolves Yes if Chainlink (BTC/USD) is at least $100,000 on 2026-07-15 16:00:00 EST."
        ),
        end_date_iso="2026-07-15T20:00:00Z",
    )
    spec_mismatch = parse_resolution_rule(CID, dst_mismatch, NOW)
    assert spec_mismatch.review_status == "ambiguous"
    assert "DST_STANDARD_TIME_MISMATCH" in spec_mismatch.ambiguity_flags
    assert "ABSTAIN_AMBIGUOUS_CUTOFF" in spec_mismatch.abstain_reasons

    # Generic "ET" without EST/EDT offset is flagged as DST-ambiguous.
    et_market = _base_market(
        description=(
            "Resolves Yes if Chainlink (BTC/USD) is at least $100,000 on 2026-12-31 16:00:00 ET."
        ),
    )
    spec_et = parse_resolution_rule(CID, et_market, NOW)
    assert spec_et.review_status == "ambiguous"
    assert "AMBIGUOUS_DAYLIGHT_SAVING_TIMEZONE" in spec_et.ambiguity_flags

    # Unverified/ambiguous rules categorically block payoff resolution.
    obs = PayoffObservation(
        observed_at=datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
        oracle="Chainlink",
        oracle_symbol="BTC/USD",
        value=Decimal("120000"),
    )
    payoff = resolve_payoff(spec_et, obs)
    assert payoff.outcome == "ABSTAIN"
    assert payoff.reason == "UNVERIFIED_OR_AMBIGUOUS_RULE"


def test_missing_oracle_and_metadata_mismatch_abstain():
    no_oracle = _base_market(
        description="Resolves Yes if BTC is at least $100,000 on 2026-12-31 23:59:00 UTC.",
        resolutionSource=None,
    )
    spec = parse_resolution_rule(CID, no_oracle, NOW)
    assert spec.review_status == "ambiguous"
    assert "ABSTAIN_MISSING_ORACLE" in spec.abstain_reasons

    mismatch = _base_market(resolutionSource="Binance")
    spec_mismatch = parse_resolution_rule(CID, mismatch, NOW)
    assert spec_mismatch.review_status == "ambiguous"
    assert "ORACLE_METADATA_RULES_MISMATCH" in spec_mismatch.ambiguity_flags
    assert "ABSTAIN_METADATA_RULES_MISMATCH" in spec_mismatch.abstain_reasons


def test_multi_outcome_and_dispute_void_markets():
    multi = _base_market(
        tokens=[
            {"outcome": "A", "token_id": "1"},
            {"outcome": "B", "token_id": "2"},
            {"outcome": "C", "token_id": "3"},
        ]
    )
    spec_multi = parse_resolution_rule(CID, multi, NOW)
    assert spec_multi.review_status == "invalid"
    assert "ABSTAIN_NON_BINARY_OR_MISSING_TOKENS" in spec_multi.abstain_reasons

    disputed = _base_market(umaResolutionStatus="disputed")
    spec_disp = parse_resolution_rule(CID, disputed, NOW)
    assert spec_disp.review_status == "ambiguous"
    assert "ABSTAIN_DISPUTED_MARKET" in spec_disp.abstain_reasons

    voided = _base_market(umaResolutionStatus="voided")
    spec_void = parse_resolution_rule(CID, voided, NOW)
    assert spec_void.review_status == "invalid"
    assert "ABSTAIN_VOID_MARKET" in spec_void.abstain_reasons

    # Verified spec receiving a venue void observation resolves to VOID 0.5/0.5.
    market = _base_market()
    market["description"] += " If void, payout is 50/50."
    verified = parse_resolution_rule(CID, market, NOW)
    void_obs = PayoffObservation(
        observed_at=datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
        oracle="Chainlink",
        source_at=datetime(2026, 12, 31, 23, 59, tzinfo=UTC),
        oracle_symbol="BTC/USD",
        uma_status="voided",
    )
    res = resolve_payoff(verified, void_obs)
    assert res.outcome == "VOID"
    assert res.payout_yes == Decimal("0.5")
    assert res.payout_no == Decimal("0.5")


def test_token_mapping_flip_and_late_clarification_invalidate_forecasts(db_session):
    spec1 = parse_resolution_rule(CID, _base_market(), NOW)
    record_rule_snapshot(db_session, spec1)
    trade = TradeHistory(
        market_id=CID,
        wallet_address="0x" + "1" * 40,
        order_type="buy",
        amount=10.0,
        price=0.5,
        timestamp=NOW,
        created_at=NOW,
    )
    db_session.add(trade)
    db_session.flush()
    db_session.add(
        Assessment(
            trade_history_id=trade.id,
            ai_score=65.0,
            reasoning="test",
            recommendation="copy",
            risk_level="low",
            confidence=65.0,
            ai_probability=0.65,
            market_price_at_assessment=0.50,
            created_at=NOW,
        )
    )
    db_session.commit()

    # Flipped YES/NO token IDs and late clarification.
    flipped_market = _base_market(
        tokens=[
            {"outcome": "Yes", "token_id": "202"},
            {"outcome": "No", "token_id": "101"},
        ]
    )
    later = NOW + timedelta(hours=2)
    spec2 = parse_resolution_rule(
        CID,
        flipped_market,
        later,
        clarifications=["Clarification: intraday wicks do not count."],
    )
    change = detect_rule_change(spec1, spec2)
    assert change.changed is True
    assert change.invalidates_forecasts is True
    assert "TOKEN_MAPPING_FLIPPED_OR_CHANGED" in change.reasons
    assert "CLARIFICATION_ADDED" in change.reasons

    record_rule_snapshot(db_session, spec2)
    invalidated = invalidate_dependent_forecasts(db_session, CID, spec2.source_hash)
    db_session.commit()
    assert invalidated == 1

    # Point-in-time as_of query at NOW + 1h must NOT see the later snapshot.
    snap_before = get_rule_snapshot_as_of(db_session, CID, NOW + timedelta(hours=1))
    assert snap_before is not None
    assert snap_before.source_hash == spec1.source_hash

    snap_after = get_rule_snapshot_as_of(db_session, CID, later)
    assert snap_after is not None
    assert snap_after.source_hash == spec2.source_hash
    assert db_session.query(ResolutionRuleSnapshot).count() == 2


def test_verify_rule_before_settlement_blocks_ambiguous():
    ok, reason, _ = verify_rule_before_settlement(CID, _base_market(), NOW)
    assert ok is True
    assert reason is None

    ambig_market = _base_market(
        description="Resolves Yes if BTC hits $100,000 on 2026-12-31 23:59:00 UTC."
    )
    ok2, reason2, _ = verify_rule_before_settlement(CID, ambig_market, NOW)
    assert ok2 is False
    assert reason2 is not None


def test_resolution_rules_api_feature_flag_and_analyze(db_session, monkeypatch):
    fastapi_app.dependency_overrides[get_db] = lambda: db_session
    fastapi_app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    client = TestClient(fastapi_app)

    # Disabled by default -> 503.
    get_settings.cache_clear()
    resp_disabled = client.get("/api/resolution-rules/schema")
    assert resp_disabled.status_code == 503

    monkeypatch.setenv("RESOLUTION_RULE_ANALYSIS_ENABLED", "true")
    get_settings.cache_clear()
    try:
        resp_schema = client.get("/api/resolution-rules/schema")
        assert resp_schema.status_code == 200
        assert "specification_schema" in resp_schema.json()

        resp_naive = client.post(
            "/api/resolution-rules/analyze",
            json={
                "condition_id": CID,
                "raw_market": _base_market(),
                "as_of": "2026-11-01T12:00:00",
            },
        )
        assert resp_naive.status_code == 422

        injected = client.post(
            "/api/resolution-rules/analyze",
            json={
                "condition_id": CID,
                "raw_market": _base_market(),
                "as_of": "2026-11-01T12:00:00Z",
                "persist_snapshot": True,
            },
        )
        assert injected.status_code == 422

        async def official_market(self, condition):
            assert condition == CID
            return _base_market()

        monkeypatch.setattr(
            "app.services.market_data.polymarket_service.PolymarketService.get_market_outcome_tokens",
            official_market,
        )
        resp_ok = client.post("/api/resolution-rules/refresh", params={"condition_id": CID})
        assert resp_ok.status_code == 200
        body = resp_ok.json()
        assert body["tradable_edge_allowed"] is True
        assert body["execution_eligible"] is False
        assert body["specification"]["review_status"] == "verified"

        resp_explain = client.get(
            "/api/resolution-rules/explain",
            params={"condition_id": CID, "as_of": body["specification"]["observed_at"]},
        )
        assert resp_explain.status_code == 200
        assert (
            resp_explain.json()["specification"]["source_hash"]
            == body["specification"]["source_hash"]
        )
    finally:
        fastapi_app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_research_mcp_resolution_rule_tool(monkeypatch):
    import types

    if "ddgs" not in sys.modules:
        monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=object))
    mcp_dir = str(Path(__file__).resolve().parents[2] / "services" / "mcps" / "research")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)
    import server as mcp_server

    assert "resolution_rule_analysis" in mcp_server.TOOLS
    schema = mcp_server.TOOLS["describe_resolution_rule_analysis"]({})
    assert schema["read_only"] is True
    bad = asyncio.run(mcp_server.resolution_rule_research({"condition_id": "invalid"}))
    assert bad["status"] == "abstain"
    assert bad["reason"] == "INVALID_CONDITION_ID"
    assert bad["execution_eligible"] is False
