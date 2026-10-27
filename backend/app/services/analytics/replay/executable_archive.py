"""Freeze actual captured receipts and bind them to private research hypotheses."""

import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import Account, BacktestRun
from app.models.analytics.executable_replay import ExecutableReplayRun, ExecutableSignalSource
from app.models.market_data.replay_capture import ReplayCapture
from app.models.market_data.venue_observation import (
    ObservationDataset,
    ObservationDatasetMember,
    VenueObservation,
)
from app.services.analytics.paper_evidence import payload
from app.services.analytics.replay.executable import replay
from app.services.analytics.replay.executable_contracts import (
    ReplayDefinition,
    ReplaySignal,
    ReplayTape,
    SettlementEvent,
)
from app.services.analytics.replay.follower import Signal
from app.services.market_data.book_research import ResearchQuery, book_event, market_spec
from app.services.market_data.replay_capture import aware, capture_manifest, digest
from app.services.market_data.venue_observations import serialize
from app.utils.time import utc_now


def _native_settlements(value, spec):
    """Paired native binary finalization; unsupported voids remain unresolved."""
    raw = value["raw"]
    clob, gamma = raw["clob"], raw["gamma"]
    if clob.get("closed") is not True:
        return []
    from decimal import Decimal

    def pair(name):
        data = gamma.get(name)
        return json.loads(data) if isinstance(data, str) else data

    tokens, prices, outcomes = pair("clobTokenIds"), pair("outcomePrices"), pair("outcomes")
    final = gamma.get("closed") is True and gamma.get("umaResolutionStatus") == "resolved"
    expected = {spec.yes_token: "YES", spec.no_token: "NO"}
    mapping = {}
    try:
        if not all(isinstance(v, list) and len(v) == 2 for v in (tokens, prices, outcomes)):
            raise ValueError("missing final map")
        for token, price, outcome in zip(tokens, prices, outcomes, strict=True):
            payout = Decimal(str(price))
            if not payout.is_finite() or expected.get(str(token)) != str(outcome).upper():
                raise ValueError("final mapping conflict")
            mapping[str(token)] = payout
        if sorted(mapping.values()) != [Decimal(0), Decimal(1)]:
            final = False
        confirmations = clob.get("tokens")
        if not isinstance(confirmations, list) or len(confirmations) != 2:
            final = False
        else:
            for token in confirmations:
                identity = str(token.get("token_id"))
                if (
                    identity not in mapping
                    or type(token.get("winner")) is not bool
                    or token["winner"] != bool(mapping[identity])
                ):
                    final = False
    except (ValueError, TypeError, ArithmeticError):
        final = False
    return [
        SettlementEvent(
            id=digest([value["snapshot_id"], token, "settlement"]),
            token_id=token,
            condition_id=spec.condition_id,
            rule_hash=spec.rule_hash,
            available_at=datetime.fromisoformat(value["received_at"]),
            source_at=datetime.fromisoformat(value["received_at"]),
            status="FINAL" if final else "DISPUTED",
            payout=mapping.get(token) if final else None,
            confirmed=final,
            source_ids=[value["snapshot_id"]],
        )
        for token in expected
    ]


def _signal_payload(row):
    if row is None or row.kind != "signal":
        raise ValueError("captured signal unavailable")
    raw = payload(row)
    signal = Signal.model_validate(raw["signal"])
    venue = raw["venue"]
    if (
        signal.event_id != row.id
        or signal.token_id != row.token_id
        or signal.first_seen_at != aware(row.observed_at)
        or signal.source_at != aware(row.source_at)
        or str(venue.get("asset")) != row.token_id
        or str(venue.get("conditionId", "")).lower() != row.condition_id
        or venue.get("side") != signal.side
        or str(venue.get("proxyWallet", "")).lower() != row.wallet
        or venue.get("type") != "TRADE"
    ):
        raise ValueError("captured signal identity conflict")
    return signal.model_dump()


def captured_definition(db, definition, capture_ids, wallet=None):
    if len(set(capture_ids)) != len(capture_ids) or not 1 <= len(capture_ids) <= 250:
        raise ValueError("invalid captured receipt selection")
    if definition.end_at > utc_now():
        raise ValueError("native source cutoff cannot be future dated")
    rows = (
        db.query(VenueObservation)
        .filter(
            VenueObservation.id.in_(capture_ids), VenueObservation.received_at <= definition.end_at
        )
        .order_by(VenueObservation.received_at, VenueObservation.id)
        .all()
    )
    if len(rows) != len(capture_ids):
        raise ValueError("captured receipt unavailable at frozen cutoff")
    values = [serialize(row) for row in rows]
    specs, events, settlements = [], [], []
    for value in values:
        if value["raw"].get("event_type") == "market_metadata":
            spec = market_spec(
                value, ResearchQuery(condition_id=value["condition_id"], as_of=definition.end_at)
            )
            specs.append(spec)
            settlements.extend(_native_settlements(value, spec))
    for value in values:
        if value["raw"].get("event_type") == "market_metadata":
            continue
        candidates = [
            s
            for s in specs
            if s.condition_id == value["condition_id"]
            and s.received_at <= datetime.fromisoformat(value["received_at"])
        ]
        if not candidates:
            continue
        spec = max(candidates, key=lambda s: (s.received_at, s.metadata_id))
        event = book_event(value, spec)
        if event is not None:
            events.append(event)
    signal_refs = []
    signals = list(definition.tape.signals)
    signal_quality = "user_declared_unverified"
    if wallet is not None:
        rows = (
            db.query(ReplayCapture)
            .filter(
                ReplayCapture.kind == "signal",
                ReplayCapture.wallet == wallet.lower(),
                ReplayCapture.observed_at >= definition.start_at,
                ReplayCapture.observed_at <= definition.end_at,
            )
            .order_by(ReplayCapture.observed_at, ReplayCapture.id)
            .limit(101)
            .all()
        )
        if len(rows) > 100:
            raise ValueError("too many captured follower signals")
        signal_refs = [row.id for row in rows]
        capture_manifest(db, signal_refs)
        signals = []
        for row in rows:
            source = _signal_payload(row)
            signals.append(
                ReplaySignal(
                    id=row.id,
                    condition_id=row.condition_id,
                    token_id=row.token_id,
                    side=source["side"],
                    source_at=aware(row.source_at) if row.source_at else aware(row.observed_at),
                    available_at=aware(row.observed_at),
                )
            )
        signal_quality = "native_first_receipt_captured"
    tape = ReplayTape(specs=specs, events=events, signals=signals, settlements=settlements)
    definition = ReplayDefinition.model_validate(
        {**definition.model_dump(), "tape": tape.model_dump()}
    )
    manifest = {
        "version": "executable-source-v1",
        "token_id": "multiple",
        "as_of": definition.end_at.isoformat(),
        "observations": [
            {
                "snapshot_id": row["snapshot_id"],
                "content_hash": row["content_hash"],
                "receipt_hash": digest(row),
            }
            for row in values
        ],
        "quality": "public_native_receipts_not_execution_proof",
        "selection": "explicit_ids",
        "rights_scope": "public native endpoints; operator verifies reuse permissions",
    }
    dataset_id = digest(manifest)
    if db.get(ObservationDataset, dataset_id) is None:
        db.add(
            ObservationDataset(
                id=dataset_id,
                manifest_json=json.dumps(manifest, sort_keys=True),
                created_at=utc_now(),
            )
        )
        db.flush()
        for identity in capture_ids:
            db.add(ObservationDatasetMember(dataset_id=dataset_id, observation_id=identity))
    evidence = {
        "book_evidence": "native_captured",
        "signal_evidence": signal_quality,
        "dataset_id": dataset_id,
        "capture_manifest": manifest,
        "signal_manifest": capture_manifest(db, signal_refs) if signal_refs else [],
    }
    return definition, evidence, signal_refs


def verify(row, db):
    definition = ReplayDefinition.model_validate_json(row.input_json)
    evidence = json.loads(row.evidence_json)
    if (
        digest(definition.model_dump(mode="json")) != row.input_hash
        or digest(evidence) != row.evidence_hash
    ):
        raise ValueError("frozen recipe integrity conflict")
    if row.dataset_id:
        from app.services.market_data.venue_observations import replay_dataset

        result = replay_dataset(db, row.dataset_id)
        if result["manifest"] != evidence["capture_manifest"]:
            raise ValueError("frozen source manifest conflict")
        refs = [item["id"] for item in evidence["signal_manifest"]]
        for identity in refs:
            _signal_payload(db.get(ReplayCapture, identity))
        if refs and capture_manifest(db, refs) != evidence["signal_manifest"]:
            raise ValueError("frozen signal manifest conflict")
    if row.result_json is not None and digest(json.loads(row.result_json)) != row.result_hash:
        raise ValueError("frozen result integrity conflict")
    return definition, evidence


def freeze(db: Session, account, definition, capture_ids=None, wallet=None):
    db.query(Account).filter_by(
        id=account.id, owner_user_id=account.owner_user_id
    ).with_for_update().one()
    if capture_ids is not None:
        definition, evidence, signal_refs = captured_definition(db, definition, capture_ids, wallet)
    else:
        if wallet:
            raise ValueError("native follower requires captured book selection")
        evidence, signal_refs = (
            {"book_evidence": "imported_unverified", "signal_evidence": "imported_unverified"},
            [],
        )
    input_hash, evidence_hash = digest(definition.model_dump(mode="json")), digest(evidence)
    old = (
        db.query(ExecutableReplayRun)
        .filter_by(account_id=account.id, input_hash=input_hash, evidence_hash=evidence_hash)
        .first()
    )
    if old is not None:
        verify(old, db)
        return old
    row = ExecutableReplayRun(
        id=str(uuid4()),
        user_id=account.owner_user_id,
        account_id=account.id,
        input_hash=input_hash,
        input_json=definition.model_dump_json(),
        evidence_json=json.dumps(evidence, sort_keys=True),
        evidence_hash=evidence_hash,
        dataset_id=evidence.get("dataset_id"),
    )
    db.add(row)
    db.flush()
    for identity in signal_refs:
        db.add(ExecutableSignalSource(run_id=row.id, capture_id=identity))
    db.flush()
    return row


def execute(db, account, row):
    db.query(Account).filter_by(
        id=account.id, owner_user_id=account.owner_user_id
    ).with_for_update().one()
    row = (
        db.query(ExecutableReplayRun)
        .filter_by(id=row.id, account_id=account.id, user_id=account.owner_user_id)
        .populate_existing()
        .with_for_update()
        .one()
    )
    definition, evidence = verify(row, db)
    if row.result_json is not None:
        return row
    result = replay(definition)
    result.update(
        source_evidence=evidence,
        execution_evidence="native_captured_counterfactual"
        if row.dataset_id
        else "imported_unverified_modeled",
    )
    result_json = json.dumps(result, sort_keys=True, allow_nan=False)
    selected = result["holdout"]
    summary = {
        "execution_evidence": result["execution_evidence"],
        "execution_replay_id": row.id,
        "input_hash": row.input_hash,
        "model_version": result["version"],
        "selected_trial_id": result["selected_trial_id"],
        "valuation": selected["valuation"] if selected else None,
        "statistics": selected["statistics"] if selected else None,
    }
    run = BacktestRun(
        user_id=account.owner_user_id,
        account_id=account.id,
        strategy_type="custom",
        strategy_name="Frozen executable replay",
        start_date=definition.start_at.date().isoformat(),
        end_date=definition.end_at.date().isoformat(),
        parameters={"executable_replay_id": row.id},
        status="completed",
        total_trades=selected["trade_count"] if selected else 0,
        total_pnl=float(selected["net_pnl"]) if selected and selected["net_pnl"] is not None else 0,
        sharpe_ratio=selected["statistics"]["sharpe_ratio"] if selected else None,
        max_drawdown=float(selected["max_drawdown"])
        if selected and selected["max_drawdown"] is not None
        else None,
        trade_log=selected["outcomes"] if selected else [],
        indicator_values={
            "execution_report": summary,
            "net_pnl_quality": "modeled"
            if selected and selected["net_pnl"] is not None
            else "unavailable",
        },
        completed_at=utc_now(),
    )
    db.add(run)
    db.flush()
    row.backtest_run_id = run.id
    row.result_json, row.result_hash = result_json, digest(result)
    row.executed_at = utc_now()
    db.flush()
    return row
