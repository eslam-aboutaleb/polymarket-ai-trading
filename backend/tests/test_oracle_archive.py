"""Only server-owned immutable reference receipts can establish analytical lineage."""

from datetime import timedelta
from decimal import Decimal

from test_valuation_recorded_evidence import valuation_db as _valuation_db

from app.services.intelligence.resolution_rules import get_rule_snapshot_as_of
from app.services.market_data.oracle_basis import (
    FeedObservation,
    OracleBasisRequest,
    OracleSpecification,
)
from app.services.market_data.oracle_basis_archive import evaluate_archived_oracle_basis
from app.services.market_data.venue_observations import append_event

valuation_db = _valuation_db


def archived_pair(db, inp):
    rule = get_rule_snapshot_as_of(db, inp.condition_id, inp.as_of)
    points = []
    for provider, source, kind, price in [
        ("Coinbase", "coinbase-public-rest", "external_reference", "100050"),
        ("Chainlink", "chainlink-data-streams", "oracle_reference", "99950"),
    ]:
        point_time = inp.as_of - timedelta(microseconds=123456)
        raw = {
            "event_type": kind,
            "provider": provider,
            "instrument": "BTC/USD",
            "asset_id": "BTC/USD",
            "value": price,
            "source_at": point_time.isoformat(),
            "market": inp.condition_id,
        }
        row = append_event(
            db, raw, received_at=inp.as_of, connection_id="fixture-reference", source=source
        )
        points.append(
            FeedObservation(
                source=provider,
                instrument="BTC/USD",
                source_at=point_time,
                received_at=inp.as_of,
                value=Decimal(price),
                capture_id=row.id,
            )
        )
    db.commit()
    return OracleBasisRequest(
        specification=OracleSpecification(
            condition_id=inp.condition_id,
            rule_hash=rule.source_hash,
            rule_review_status="verified",
            authoritative_provider="Chainlink",
            eligible_instrument="BTC/USD",
            cutoff_utc=rule.cutoff_utc,
        ),
        as_of=inp.as_of,
        strike=Decimal("100000"),
        spot_observations=[points[0]],
        settlement_observations=[points[1]],
    )


def test_archive_source_hash_value_and_microsecond_clock_are_bound(valuation_db):
    db, _, inp = valuation_db
    request = archived_pair(db, inp)
    study = evaluate_archived_oracle_basis(db, request)
    assert study.oracle_verified and study.status == "analyzed"
    assert study.sign_reversal_count == 1
    assert study.false_win_interval_95 is not None
    assert study.raw_point_audit[0]["spot"]["source_at"].endswith("876544Z")
    forged = request.model_copy(deep=True)
    forged.settlement_observations[0].value = Decimal("100500")
    study = evaluate_archived_oracle_basis(db, forged)
    assert study.status == "abstain" and not study.oracle_verified
    assert (
        evaluate_archived_oracle_basis(db, request).model_dump()
        == evaluate_archived_oracle_basis(db, request).model_dump()
    )


def test_displayed_polymarket_reference_is_never_chainlink_proof(valuation_db):
    db, _, inp = valuation_db
    request = archived_pair(db, inp)
    request.settlement_observations[0].capture_id = None
    study = evaluate_archived_oracle_basis(db, request)
    assert study.status == "abstain" and not study.oracle_verified
