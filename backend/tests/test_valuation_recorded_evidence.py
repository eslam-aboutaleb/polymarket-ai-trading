"""Actual archive/calibration joins; outbound public HTTP is a deterministic fixture."""

from datetime import timedelta
from decimal import Decimal

import book_research_fixtures as fixtures
import pytest

from app.models.analytics.assessment import VERIFIED_SCORING_VERSION, Assessment
from app.services.intelligence.binary_valuation import BinaryValuationInput, value_binary_contract
from app.services.intelligence.resolution_rules import parse_resolution_rule, record_rule_snapshot
from app.services.intelligence.valuation_evidence import (
    calibrated_probability,
    value_recorded_contract,
)

CID, NOW = fixtures.CID, fixtures.NOW


@pytest.fixture
def valuation_db(monkeypatch):
    original = fixtures.clob_market

    def market():
        raw = original()
        raw.update(
            question="Will BTC be at least $100000?",
            description=(
                "Resolves Yes if Chainlink (BTC/USD) is at least $100000 "
                "on 2026-12-31 23:59:00 UTC."
            ),
            resolutionSource="Chainlink",
            end_date_iso="2026-12-31T23:59:00Z",
            fees_enabled=False,
        )
        return raw

    monkeypatch.setattr(fixtures, "clob_market", market)
    for db in fixtures.research_db.__wrapped__(monkeypatch):
        rule = parse_resolution_rule(CID, market(), NOW - timedelta(seconds=2))
        assert rule.review_status == "verified", rule.abstain_reasons
        record_rule_snapshot(db, rule)
        for i in range(20):
            db.add(
                Assessment(
                    condition_id="0x" + f"{i:064x}",
                    ai_score=60,
                    reasoning="synthetic calibration fixture",
                    recommendation="hold",
                    risk_level="unknown",
                    confidence=60,
                    ai_probability=0.6,
                    model_name="fixture-model",
                    task="fixture",
                    created_at=NOW - timedelta(days=2),
                    resolved_at=NOW - timedelta(days=1),
                    resolved_outcome="YES" if i < 12 else "NO",
                    brier_score=0.16 if i < 12 else 0.36,
                    scoring_version=VERIFIED_SCORING_VERSION,
                )
            )
        forecast = Assessment(
            condition_id=CID,
            ai_score=60,
            reasoning="synthetic forecast fixture",
            recommendation="hold",
            risk_level="unknown",
            confidence=60,
            ai_probability=0.6,
            model_name="fixture-model",
            task="fixture",
            created_at=NOW - timedelta(seconds=1),
        )
        db.add(forecast)
        db.commit()
        inp = BinaryValuationInput(
            condition_id=CID,
            token_id="11",
            outcome="YES",
            shares="20",
            as_of=NOW,
            rules_hash=rule.source_hash,
            rule_review_status="verified",
            forecast_id=forecast.id,
            calibrated_p_yes=".9",
            p_yes_lower=".9",
            p_yes_upper=".9",
            executable_vwap=".01",
            available_depth_shares="10000",
            taker_fee_per_share="0",
            fee_schedule_version="caller-claim",
        )
        yield db, forecast, inp


def test_recorded_depth_and_calibration_replace_caller_claims(valuation_db):
    db, _, inp = valuation_db
    value = value_recorded_contract(db, inp, 1)
    assert value.status == "valued", value.abstain_reasons
    assert value.ev_gross_per_share == Decimal(".05")
    assert value.ev_net_total == Decimal("1")
    assert value.bounded_kelly_fraction == 0
    assert value.provenance["calibration"]["sample_count"] == 20
    assert value.provenance["book_hash"]
    assert value.execution_eligible is False


def test_missing_future_and_duplicate_labels_cannot_create_calibration(valuation_db):
    db, forecast, _ = valuation_db
    row = db.query(Assessment).filter(Assessment.condition_id != CID).first()
    row.resolved_at = NOW + timedelta(seconds=1)
    db.flush()
    with pytest.raises(ValueError, match="INSUFFICIENT_CALIBRATION_HISTORY"):
        calibrated_probability(db, forecast, NOW)


def test_wrong_token_missing_depth_and_unowned_sell_fail_closed(valuation_db):
    db, _, inp = valuation_db
    wrong = value_recorded_contract(db, inp.model_copy(update={"token_id": "22"}), 1)
    assert "ABSTAIN_TOKEN_RULE_MISMATCH" in wrong.abstain_reasons
    large = value_recorded_contract(db, inp.model_copy(update={"shares": Decimal("100000")}), 1)
    assert large.status == "abstain"
    sell = value_recorded_contract(
        db,
        inp.model_copy(
            update={
                "side": "SELL",
                "shares": Decimal("5"),
                "account_id": 3,
                "owned_shares": Decimal("100"),
            }
        ),
        1,
    )
    assert "ABSTAIN_ACCOUNT_OWNERSHIP" in sell.abstain_reasons
    sell = value_recorded_contract(
        db,
        inp.model_copy(
            update={
                "side": "SELL",
                "shares": Decimal("5"),
                "account_id": 1,
                "owned_shares": Decimal("100"),
            }
        ),
        1,
    )
    assert "ABSTAIN_INSUFFICIENT_OWNED_INVENTORY" in sell.abstain_reasons


def test_intervals_lots_baseline_and_kelly_claims_are_conservative(valuation_db):
    _, _, inp = valuation_db
    for change in [
        {"p_yes_lower": None},
        {"market_implied_p": Decimal("2")},
        {"share_lot": Decimal("3")},
        {"tick_size": Decimal(".01"), "limit_price": Decimal(".555")},
    ]:
        assert value_binary_contract(inp.model_copy(update=change)).status == "abstain"
    value = value_binary_contract(inp.model_copy(update={"kelly_fraction_cap": Decimal(".5")}))
    assert value.bounded_kelly_fraction == 0


def test_fee_curve_uses_each_depth_level_and_never_legacy_base_fee_alone():
    from app.services.market_data.valuation_fees import archived_taker_fee

    metadata = {
        "clob": {"taker_base_fee": 1000},
        "gamma": {
            "version": "v1",
            "feesEnabled": True,
            "feeType": "crypto_fees_v2",
            "feeSchedule": {"exponent": 1, "rate": 0.07, "takerOnly": True},
        },
    }
    fee, version = archived_taker_fee(
        metadata, [{"price": ".5", "shares": "10"}, {"price": ".6", "shares": "10"}]
    )
    assert fee == Decimal(".34300")
    assert "ceiling5" in version
    metadata["gamma"].pop("feeSchedule")
    with pytest.raises(ValueError, match="UNKNOWN_FEE_CURVE"):
        archived_taker_fee(metadata, [{"price": ".5", "shares": "10"}])
