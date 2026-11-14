"""Unit, counterexample, API, and MCP tests for oracle-basis-risk (#17)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.main import app as fastapi_app
from app.services.market_data.oracle_basis import (
    FeedObservation,
    OracleBasisRequest,
    OracleSpecification,
    evaluate_oracle_basis,
)

CID = "0x" + "c" * 64
T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def _spec(**overrides) -> OracleSpecification:
    data = {
        "condition_id": CID,
        "rule_hash": "d" * 64,
        "rule_review_status": "verified",
        "authoritative_provider": "Chainlink",
        "eligible_instrument": "BTC/USD",
        "aggregation": "last",
        "rounding_decimals": 2,
        "cutoff_utc": T0 + timedelta(hours=1),
    }
    data.update(overrides)
    return OracleSpecification(**data)


def test_counterexample_spot_rises_above_strike_while_settlement_loses():
    # Spot (Binance) is 100,050 (> 100,000 strike) while Chainlink settlement is 99,950 (< 100,000).
    req = OracleBasisRequest(
        specification=_spec(),
        strike=Decimal("100000.00"),
        comparator=">=",
        spot_observations=[
            FeedObservation(
                instrument="BTC/USD",
                source="Binance",
                source_at=T0,
                received_at=T0 + timedelta(seconds=3),
                value=Decimal("100050.00"),
            )
        ],
        settlement_observations=[
            FeedObservation(
                instrument="BTC/USD",
                source="Chainlink",
                source_at=T0,
                received_at=T0 + timedelta(seconds=2),
                value=Decimal("99950.00"),
            )
        ],
    )
    study = evaluate_oracle_basis(req, lineage_verified=True)
    assert study.status == "analyzed"
    assert study.oracle_verified is True
    assert study.execution_eligible is False
    assert study.sign_reversal_count == 1
    assert study.false_win_rate == Decimal("1.0000")
    assert study.basis_mean_bp == Decimal("10.01")


def test_abstains_on_missing_settlement_unverified_rule_and_provider_mismatch():
    missing_sett = evaluate_oracle_basis(
        OracleBasisRequest(
            specification=_spec(),
            strike=Decimal("100000"),
            spot_observations=[
                FeedObservation(
                    instrument="BTC/USD",
                    source="Binance",
                    source_at=T0,
                    received_at=T0,
                    value=Decimal("100000"),
                )
            ],
            settlement_observations=[],
        )
    )
    assert missing_sett.status == "abstain"
    assert missing_sett.oracle_verified is False
    assert "ABSTAIN_MISSING_SETTLEMENT_FEED" in missing_sett.abstain_reasons

    unverified = evaluate_oracle_basis(
        OracleBasisRequest(
            specification=_spec(rule_review_status="ambiguous"),
            strike=Decimal("100000"),
            spot_observations=[
                FeedObservation(
                    instrument="BTC/USD",
                    source="Binance",
                    source_at=T0,
                    received_at=T0,
                    value=Decimal("100000"),
                )
            ],
            settlement_observations=[
                FeedObservation(
                    instrument="BTC/USD",
                    source="Chainlink",
                    source_at=T0,
                    received_at=T0,
                    value=Decimal("100000"),
                )
            ],
        )
    )
    assert unverified.status == "abstain"
    assert "ABSTAIN_UNVERIFIED_RULE" in unverified.abstain_reasons

    wrong_provider = evaluate_oracle_basis(
        OracleBasisRequest(
            specification=_spec(authoritative_provider="Chainlink"),
            strike=Decimal("100000"),
            spot_observations=[
                FeedObservation(
                    instrument="BTC/USD",
                    source="Binance",
                    source_at=T0,
                    received_at=T0,
                    value=Decimal("100000"),
                )
            ],
            settlement_observations=[
                FeedObservation(
                    instrument="BTC/USD",
                    source="RandomFeed",
                    source_at=T0,
                    received_at=T0,
                    value=Decimal("100000"),
                )
            ],
        )
    )
    assert wrong_provider.status == "abstain"
    assert "ABSTAIN_ORACLE_PROVIDER_MISMATCH" in wrong_provider.abstain_reasons


def test_twap_aggregation_rounding_and_checksum_reproducibility():
    req = OracleBasisRequest(
        specification=_spec(aggregation="twap", rounding_decimals=2, rounding_mode="half_up"),
        strike=Decimal("100.00"),
        comparator=">=",
        spot_observations=[
            FeedObservation(
                instrument="BTC/USD",
                source="Binance",
                source_at=T0,
                received_at=T0,
                value=Decimal("100.004"),
            )
        ],
        settlement_observations=[
            FeedObservation(
                instrument="BTC/USD",
                source="Chainlink",
                source_at=T0 - timedelta(seconds=60),
                received_at=T0,
                value=Decimal("99.90"),
            ),
            FeedObservation(
                instrument="BTC/USD",
                source="Chainlink",
                source_at=T0 - timedelta(seconds=10),
                received_at=T0,
                value=Decimal("100.50"),
            ),
        ],
    )
    study1 = evaluate_oracle_basis(req, lineage_verified=True)
    study2 = evaluate_oracle_basis(req, lineage_verified=True)
    assert study1.status == "analyzed"
    assert study1.observations[0].reference_value == Decimal("100.00")
    assert study1.observations[0].feed_value == Decimal("100.00")
    assert study1.basis_mean_bp == Decimal("0.00")
    assert study1.feed_checksum_sha256 == study2.feed_checksum_sha256


def test_oracle_basis_api_and_mcp_tool(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.models import Base
    from app.utils.database import get_db

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    fastapi_app.dependency_overrides[get_db] = lambda: db
    import types

    fastapi_app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    client = TestClient(fastapi_app)
    get_settings.cache_clear()
    try:
        assert client.get("/api/oracle-basis/schema").status_code == 503
        monkeypatch.setenv("ORACLE_BASIS_RISK_ENABLED", "true")
        get_settings.cache_clear()
        assert client.get("/api/oracle-basis/schema").status_code == 200
        resp = client.post(
            "/api/oracle-basis/analyze",
            json={
                "specification": _spec().model_dump(mode="json"),
                "strike": "100000",
                "comparator": ">=",
                "spot_observations": [
                    {
                        "source": "Binance",
                        "instrument": "BTC/USD",
                        "source_at": T0.isoformat(),
                        "received_at": T0.isoformat(),
                        "value": "100050",
                    }
                ],
                "settlement_observations": [
                    {
                        "source": "Chainlink",
                        "instrument": "BTC/USD",
                        "source_at": T0.isoformat(),
                        "received_at": T0.isoformat(),
                        "value": "99950",
                    }
                ],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["sign_reversal_count"] == 1
        assert resp.json()["execution_eligible"] is False
        assert resp.json()["status"] == "abstain"
        assert resp.json()["oracle_verified"] is False
    finally:
        fastapi_app.dependency_overrides.clear()
        get_settings.cache_clear()
        db.close()
        engine.dispose()

    if "ddgs" not in sys.modules:
        monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=object))
    mcp_dir = str(Path(__file__).resolve().parents[2] / "services" / "mcps" / "research")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)
    import server as mcp_server

    assert "oracle_basis_risk" in mcp_server.TOOLS
    descriptor = mcp_server.TOOLS["describe_oracle_basis_risk"]({})
    assert descriptor["read_only"] is True
    assert descriptor["input_schema"] == OracleBasisRequest.model_json_schema()
