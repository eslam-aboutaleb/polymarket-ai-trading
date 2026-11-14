"""Review counterexamples: actual availability, feed identity and weighted windows."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.market_data.oracle_basis import (
    FeedObservation,
    OracleBasisRequest,
    OracleSpecification,
    evaluate_oracle_basis,
)

T = datetime(2025, 11, 13, 12, 0, 0, 123456, tzinfo=UTC)
CID = "0x" + "a" * 64


def point(source, time=T, received=None, value="100", instrument="BTC/USD"):
    return FeedObservation(
        source=source,
        instrument=instrument,
        source_at=time,
        received_at=received or time,
        value=Decimal(value),
    )


def request(**changes):
    values = {
        "specification": OracleSpecification(
            condition_id=CID,
            rule_hash="a" * 64,
            rule_review_status="verified",
            authoritative_provider="Chainlink",
            eligible_instrument="BTC/USD",
            cutoff_utc=T + timedelta(hours=1),
        ),
        "strike": Decimal("100"),
        "spot_observations": [point("Coinbase")],
        "settlement_observations": [point("Chainlink")],
    }
    values.update(changes)
    return OracleBasisRequest(**values)


@pytest.mark.parametrize(
    "source,instrument",
    [("untrusted-chainlink-lookalike", "BTC/USD"), ("Chainlink", "ETH/USD"), ("Chainlink", None)],
)
def test_lookalike_or_wrong_instrument_never_verifies(source, instrument):
    result = evaluate_oracle_basis(
        request(settlement_observations=[point(source, instrument=instrument)]),
        lineage_verified=True,
    )
    assert result.status == "abstain" and not result.oracle_verified


def test_future_publication_stale_and_partial_coverage_never_verifies():
    future = request(
        settlement_observations=[point("Chainlink", received=T + timedelta(seconds=1))]
    )
    assert evaluate_oracle_basis(future, lineage_verified=True).status == "abstain"
    stale = request(
        spot_observations=[point("Coinbase", received=T + timedelta(seconds=301))],
        settlement_observations=[point("Chainlink", received=T + timedelta(seconds=301))],
    )
    result = evaluate_oracle_basis(stale, lineage_verified=True)
    assert result.stale_settlement_count == 1 and result.status == "abstain"
    partial = request(
        spot_observations=[point("Coinbase"), point("Coinbase", T + timedelta(seconds=120))]
    )
    result = evaluate_oracle_basis(partial, lineage_verified=True)
    assert result.paired_count == 1 and result.status == "abstain" and not result.oracle_verified


def test_timezone_conversion_preserves_2025_microsecond_precision():
    local = T.astimezone(timezone(timedelta(hours=-5)))
    result = evaluate_oracle_basis(
        request(settlement_observations=[point("Chainlink", local)]), lineage_verified=True
    )
    assert result.oracle_verified and result.observations[0].source_at == T
    assert result.observations[0].source_at.microsecond == 123456


def test_duration_weighted_twap_differs_from_mean_and_rejects_missing_cadence():
    req = request(
        settlement_observations=[
            point("Chainlink", T - timedelta(seconds=60), T, "99"),
            point("Chainlink", T - timedelta(seconds=10), T, "105"),
        ]
    )
    req.specification.aggregation = "twap"
    result = evaluate_oracle_basis(req, lineage_verified=True)
    assert result.oracle_verified and result.observations[0].reference_value == Decimal("100")
    assert result.raw_point_audit[0]["settlement_points"][0]["value"] == "99"
    req.specification.sampling_interval_seconds = 30
    assert evaluate_oracle_basis(req, lineage_verified=True).status == "abstain"


def test_untrusted_payload_and_declared_fallback_do_not_self_approve():
    result = evaluate_oracle_basis(request())
    assert result.paired_count == 1 and not result.oracle_verified and result.status == "abstain"
    req = request(settlement_observations=[point("Kraken")])
    req.specification.fallback_provider = "Kraken"
    assert evaluate_oracle_basis(req, lineage_verified=True).status == "abstain"


def test_equal_threshold_rounding_is_explicit_and_hash_bound():
    req = request(
        spot_observations=[point("Coinbase", value="100.004")],
        settlement_observations=[point("Chainlink", value="100.004")],
        strike=Decimal("100.01"),
    )
    req.specification.rounding_mode = "ceiling"
    up = evaluate_oracle_basis(req, lineage_verified=True)
    assert up.observations[0].reference_value == Decimal("100.01")
    req.specification.rounding_mode = "floor"
    down = evaluate_oracle_basis(req, lineage_verified=True)
    assert down.observations[0].reference_value == Decimal("100.00")
    assert up.feed_checksum_sha256 != down.feed_checksum_sha256
