"""Counterexamples found in review: numeric integrity, event time and no hindsight."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.analytics.resolution_rule_snapshot import ResolutionRuleSnapshot
from app.services.intelligence.resolution_rules import (
    PayoffObservation,
    detect_rule_change,
    explain_resolution_rule,
    parse_resolution_rule,
    record_rule_snapshot,
    resolve_payoff,
)

CID = "0x" + "a" * 64
NOW = datetime(2026, 11, 13, tzinfo=UTC)
CUTOFF = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)


def market(threshold="100000", comparator="at least"):
    return {
        "condition_id": CID,
        "question": f"Will BTC be {comparator} ${threshold}?",
        "description": (
            f"Resolves Yes if Chainlink (BTC/USD) is {comparator} ${threshold} "
            "on 2026-12-31 23:59:00 UTC."
        ),
        "resolutionSource": "Chainlink",
        "end_date_iso": CUTOFF.isoformat(),
        "neg_risk": False,
        "tokens": [{"outcome": "Yes", "token_id": "101"}, {"outcome": "No", "token_id": "202"}],
    }


@pytest.mark.parametrize("number", ["100000", "100000.01", "100,000.01", "999999999.99"])
def test_entire_threshold_is_parsed_and_every_span_round_trips(number):
    spec = parse_resolution_rule(CID, market(number), NOW)
    assert spec.review_status == "verified"
    assert spec.threshold == Decimal(number.replace(",", ""))
    for span in spec.evidence_spans:
        text = spec.question if span.field.startswith("question.") else spec.raw_rules
        assert text[span.start : span.end] == span.quote


def test_comparator_overlap_is_not_confused_with_multiple_independent_bounds():
    spec = parse_resolution_rule(CID, market(comparator="greater than or equal to"), NOW)
    assert spec.review_status == "verified"
    assert spec.comparator == ">="
    raw = market()
    raw["description"] += " It must also be below $120000."
    assert parse_resolution_rule(CID, raw, NOW).review_status != "verified"


def test_dispute_or_void_revision_changes_the_fingerprint():
    raw = market()
    original = parse_resolution_rule(CID, raw, NOW)
    raw["disputed"] = True
    changed = parse_resolution_rule(CID, raw, NOW + timedelta(hours=1))
    assert changed.review_status != "verified"
    assert detect_rule_change(original, changed).invalidates_forecasts


@pytest.mark.parametrize(
    "oracle,symbol", [("not-chainlink", "BTC/USD"), ("Chainlink", None), ("Chainlink", "ETH/USD")]
)
def test_wrong_or_missing_source_instrument_cannot_resolve(oracle, symbol):
    spec = parse_resolution_rule(CID, market(), NOW)
    result = resolve_payoff(
        spec,
        PayoffObservation(
            observed_at=CUTOFF + timedelta(hours=1),
            source_at=CUTOFF,
            oracle=oracle,
            oracle_symbol=symbol,
            value=Decimal("100001"),
        ),
    )
    assert result.outcome == "ABSTAIN"


def test_delayed_publication_uses_cutoff_event_time_not_later_price():
    spec = parse_resolution_rule(CID, market(), NOW)
    observation = PayoffObservation(
        observed_at=CUTOFF + timedelta(hours=1),
        source_at=CUTOFF,
        oracle="Chainlink",
        oracle_symbol="BTC/USD",
        value=Decimal("99999"),
    )
    assert resolve_payoff(spec, observation).outcome == "NO"
    late_price = observation.model_copy(update={"source_at": CUTOFF + timedelta(seconds=1)})
    assert resolve_payoff(spec, late_price).outcome == "ABSTAIN"


def test_historical_miss_never_fetches_or_backdates_current_payload():
    class ForbiddenProvider:
        async def get_market_outcome_tokens(self, _):
            pytest.fail("A historical miss must not fetch current mutable rules")

    engine = create_engine("sqlite://")
    ResolutionRuleSnapshot.__table__.create(engine)
    try:
        with Session(engine) as db:
            with pytest.raises(ValueError, match="HISTORICAL_RULE_SNAPSHOT_UNAVAILABLE"):
                asyncio.run(
                    explain_resolution_rule(CID, as_of=NOW, db=db, service=ForbiddenProvider())
                )
            with pytest.raises(ValueError, match="HISTORICAL_RULE_SNAPSHOT_UNAVAILABLE"):
                asyncio.run(explain_resolution_rule(CID, as_of=NOW, db=db, raw_market=market()))
            record_rule_snapshot(db, parse_resolution_rule(CID, market(), NOW))
            result = asyncio.run(
                explain_resolution_rule(CID, as_of=NOW, db=db, service=ForbiddenProvider())
            )
            assert result.specification.observed_at == NOW
    finally:
        engine.dispose()


def test_untrusted_analysis_does_not_advertise_a_tradable_edge():
    result = asyncio.run(explain_resolution_rule(CID, raw_market=market()))
    assert result.specification.review_status == "verified"
    assert result.tradable_edge_allowed is False


@pytest.mark.parametrize(
    "suffix",
    [
        " The value is rounded to two decimals.",
        " Use the TWAP over the final minute.",
        " An average of observations determines the value.",
    ],
)
def test_unsupported_rounding_and_aggregation_never_claim_a_verified_point_rule(suffix):
    raw = market()
    raw["description"] += suffix
    assert parse_resolution_rule(CID, raw, NOW).review_status != "verified"


@pytest.mark.parametrize("number", ["1e5", "100k", "100 000"])
def test_numeric_scale_or_spacing_is_not_silently_truncated(number):
    assert parse_resolution_rule(CID, market(number), NOW).review_status != "verified"


def test_void_payout_cannot_bypass_oracle_identity():
    raw = market()
    raw["description"] += " If voided, both outcomes receive 50/50."
    spec = parse_resolution_rule(CID, raw, NOW)
    assert spec.review_status == "verified"
    obs = PayoffObservation(
        observed_at=CUTOFF,
        source_at=CUTOFF,
        oracle="counterfeit",
        oracle_symbol="BTC/USD",
        uma_status="void",
    )
    assert resolve_payoff(spec, obs).outcome == "ABSTAIN"
    assert resolve_payoff(spec, obs.model_copy(update={"oracle": "Chainlink"})).outcome == "VOID"


def test_rule_reversion_is_recorded_at_its_actual_receipt_time():
    from app.services.intelligence.resolution_rules import get_rule_snapshot_as_of

    engine = create_engine("sqlite://")
    ResolutionRuleSnapshot.__table__.create(engine)
    with Session(engine) as db:
        first = parse_resolution_rule(CID, market(), NOW)
        changed = parse_resolution_rule(CID, market("110000"), NOW + timedelta(hours=1))
        reverted = parse_resolution_rule(CID, market(), NOW + timedelta(hours=2))
        for spec in [first, changed, reverted, reverted]:
            record_rule_snapshot(db, spec)
        db.commit()
        assert (
            get_rule_snapshot_as_of(db, CID, NOW + timedelta(minutes=90)).source_hash
            == changed.source_hash
        )
        assert (
            get_rule_snapshot_as_of(db, CID, NOW + timedelta(hours=3)).source_hash
            == first.source_hash
        )
        assert db.query(ResolutionRuleSnapshot).count() == 3
    engine.dispose()


def test_ordinary_finalization_does_not_change_the_contract_rule_hash():
    raw = market()
    before = parse_resolution_rule(CID, raw, NOW)
    raw["umaResolutionStatus"] = "resolved"
    assert parse_resolution_rule(CID, raw, CUTOFF).source_hash == before.source_hash


def test_resolution_path_rejects_changed_or_unrecorded_decision_rules(monkeypatch):
    from types import SimpleNamespace

    from app.services.intelligence.forecast_resolution import (
        ExclusionReason,
        _rule_version_exclusion,
    )

    engine = create_engine("sqlite://")
    ResolutionRuleSnapshot.__table__.create(engine)

    async def official(self, condition):
        assert condition == CID
        return market()

    monkeypatch.setattr(
        "app.services.market_data.polymarket_service.PolymarketService.get_market_outcome_tokens",
        official,
    )
    row = SimpleNamespace(condition_id=CID, created_at=NOW + timedelta(minutes=1))
    with Session(engine) as db:
        assert asyncio.run(_rule_version_exclusion(db, row, {})) == ExclusionReason.RULE_UNVERIFIED
        record_rule_snapshot(db, parse_resolution_rule(CID, market(), NOW))
        assert asyncio.run(_rule_version_exclusion(db, row, {})) is None
        record_rule_snapshot(
            db, parse_resolution_rule(CID, market("110000"), NOW + timedelta(seconds=1))
        )
        assert asyncio.run(_rule_version_exclusion(db, row, {})) == ExclusionReason.RULE_CHANGED
    engine.dispose()
