"""Read-side bridge from immutable native receipts to the binary-book model."""

import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

import httpx
from pydantic import AwareDatetime, Field

from app.models.market_data.venue_observation import VenueObservation
from app.services.market_data.binary_book import (
    VERSION,
    BookEvent,
    Contract,
    Level,
    MarketSpec,
    WalkRequest,
    complement_summary,
    reconstruct,
    walk_depth,
)
from app.services.market_data.replay_capture import digest, native_active
from app.services.market_data.venue_observations import append_event, serialize
from app.utils.time import utc_now


class ResearchQuery(Contract):
    condition_id: str = Field(pattern=r"^0x[a-fA-F0-9]{64}$")
    as_of: AwareDatetime | None = None
    outcome: Literal["YES", "NO"] = "YES"
    sizes: list[Annotated[Decimal, Field(gt=0, le=1_000_000_000, decimal_places=8)]] = Field(
        default_factory=lambda: [Decimal("10")], min_length=1, max_length=8
    )
    side: Literal["BUY", "SELL"] = "BUY"
    units: Literal["SHARES", "USDC"] = "SHARES"
    fee_bps: Decimal | None = Field(default=None, ge=0, le=10000, decimal_places=8)
    fee_version: str | None = Field(default=None, min_length=1, max_length=80)
    lot: Decimal = Field(default=Decimal("0.01"), gt=0, le=1, decimal_places=8)
    max_age_seconds: int = Field(default=60, ge=1, le=300)


def checksum(raw):
    """Use the pinned SDK's pure server-compatible hash; never construct a client."""
    if not raw.get("hash"):
        return "unavailable"
    try:
        from py_clob_client_v2.utilities import (
            generate_orderbook_summary_hash,
            parse_raw_orderbook_summary,
        )

        model = parse_raw_orderbook_summary(raw)
        return "verified" if generate_orderbook_summary_hash(model) == raw["hash"] else "mismatch"
    except (KeyError, TypeError, ValueError):
        return "incomplete"


def market_spec(metadata, query):
    raw = metadata["raw"]
    clob, gamma = raw["clob"], raw["gamma"]
    from app.services.intelligence.market_context import build_market_context

    captured_at = datetime.fromisoformat(metadata["received_at"])
    context = build_market_context(raw["market"], clob, captured_at).model_dump(mode="json")
    labels, tokens = gamma["outcomes"], gamma["clobTokenIds"]
    labels = json.loads(labels) if isinstance(labels, str) else labels
    tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
    if (
        str(gamma["conditionId"]).lower() != context["condition_id"]
        or len(labels) != 2
        or len(tokens) != 2
        or {str(label).upper(): str(token) for label, token in zip(labels, tokens, strict=True)}
        != {v["outcome"]: v["token_id"] for v in context["outcomes"]}
    ):
        raise ValueError("PAIRED_METADATA_CONFLICT")
    mapping = {v["outcome"]: v["token_id"] for v in context["outcomes"]}
    return MarketSpec(
        condition_id=context["condition_id"],
        yes_token=mapping["YES"],
        no_token=mapping["NO"],
        rule_hash=context["rules"]["content_hash"],
        metadata_id=metadata["snapshot_id"],
        received_at=datetime.fromisoformat(metadata["received_at"]),
        active=native_active(clob, gamma),
        tick=clob["minimum_tick_size"],
        minimum_shares=clob["minimum_order_size"],
        lot=query.lot,
        supported=clob.get("neg_risk") is False,
    )


def book_event(value, spec):
    raw = value["raw"]
    kind = raw.get("event_type") or raw.get("type")
    base = {
        "snapshot_id": value["snapshot_id"],
        "token_id": value["token_id"],
        "condition_id": str(raw.get("market") or ""),
        "rule_hash": raw.get("_rule_hash", spec.rule_hash),
        "received_at": value["received_at"],
        "source_at": value["event_at"],
        "connection_id": value["connection_id"],
        "sequence": None,
    }
    try:
        # Native market-channel documentation does not promise consecutive sequence
        # numbers. Numeric-looking fields are never promoted to that guarantee.
        if kind == "source_status":
            base["condition_id"] = spec.condition_id
            return (
                BookEvent(**base, kind="disconnect")
                if raw.get("status") != "SOURCE_CONNECTED"
                else None
            )
        if kind == "book":
            if (
                raw.get("tick_size") is not None
                and Decimal(str(raw["tick_size"])) != spec.tick
                or raw.get("min_order_size") is not None
                and Decimal(str(raw["min_order_size"])) != spec.minimum_shares
            ):
                return BookEvent(**base, kind="tick_change")
            return BookEvent(
                **base,
                kind="snapshot",
                checksum=checksum(raw),
                bids=[Level(price=v["price"], shares=v["size"]) for v in raw["bids"]],
                asks=[Level(price=v["price"], shares=v["size"]) for v in raw["asks"]],
            )
        if kind == "price_change":
            changes = [
                (v["side"], Level(price=v["price"], shares=v["size"]))
                for v in raw["price_changes"]
                if str(v["asset_id"]) == value["token_id"]
            ]
            return BookEvent(**base, kind="delta", changes=changes)
        if kind == "tick_size_change":
            return BookEvent(**base, kind="tick_change")
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return BookEvent(**base, kind="disconnect")
    return None


def analyze_values(query: ResearchQuery, values: list[dict], as_of):
    visible = [v for v in values if datetime.fromisoformat(v["received_at"]) <= as_of]
    metadata = [
        v
        for v in visible
        if v["raw"].get("event_type") == "market_metadata"
        and str(v["condition_id"]).lower() == query.condition_id.lower()
    ]
    base = {
        "version": VERSION,
        "classification": "implemented_detector",
        "research_only": True,
        "condition_id": query.condition_id.lower(),
        "as_of": as_of.isoformat(),
        "actual_fills": None,
        "queue_policy": "unavailable_no_probability",
        "money_path_changed": False,
    }
    if not metadata:
        return {
            **base,
            "status": "abstain",
            "reason": "NO_ASOF_MARKET_METADATA",
            "books": {},
            "estimates": [],
        }
    latest = max(metadata, key=lambda v: (v["received_at"], v["snapshot_id"]))
    try:
        spec = market_spec(latest, query)
        if spec.condition_id != query.condition_id.lower():
            raise ValueError("metadata condition mismatch")
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return {
            **base,
            "status": "abstain",
            "reason": "MAPPING_OR_SIZE_RULES_UNAVAILABLE",
            "books": {},
            "estimates": [],
        }
    events = [
        event
        for v in visible
        if v["token_id"] in (spec.yes_token, spec.no_token)
        and (event := book_event(v, spec)) is not None
    ]
    books = {
        "YES": reconstruct(spec, spec.yes_token, events, as_of, query.max_age_seconds),
        "NO": reconstruct(spec, spec.no_token, events, as_of, query.max_age_seconds),
    }
    estimates = [
        walk_depth(
            spec,
            books[query.outcome],
            WalkRequest(
                side=query.side,
                units=query.units,
                amount=size,
                fee_bps=query.fee_bps,
                fee_version=query.fee_version,
            ),
        ).model_dump(mode="json")
        for size in query.sizes
    ]
    return {
        **base,
        "status": "modeled" if any(e["status"] == "modeled" for e in estimates) else "abstain",
        "reason": None
        if any(e["status"] == "modeled" for e in estimates)
        else "COST_ESTIMATE_UNAVAILABLE",
        "metadata_id": latest["snapshot_id"],
        "rule_hash": spec.rule_hash,
        "spec": spec.model_dump(mode="json"),
        "books": {key: value.model_dump(mode="json") for key, value in books.items()},
        "estimates": estimates,
        "complement": complement_summary(books["YES"], books["NO"]),
    }


def load_analysis(db, query):
    as_of = query.as_of or utc_now()
    metadata = (
        db.query(VenueObservation)
        .filter(
            VenueObservation.condition_id == query.condition_id.lower(),
            VenueObservation.kind == "market_metadata",
            VenueObservation.received_at <= as_of,
        )
        .order_by(VenueObservation.received_at.desc(), VenueObservation.id.desc())
        .first()
    )
    if metadata is None:
        return analyze_values(query, [], as_of), []
    meta = serialize(metadata)
    try:
        spec = market_spec(meta, query)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return analyze_values(query, [meta], as_of), [meta]
    rows = []
    for token in (spec.yes_token, spec.no_token):
        rows.extend(
            db.query(VenueObservation)
            .filter(
                VenueObservation.token_id == token,
                VenueObservation.received_at <= as_of,
                VenueObservation.kind.in_(
                    ["book", "price_change", "source_status", "tick_size_change"]
                ),
            )
            .order_by(VenueObservation.received_at.desc(), VenueObservation.id.desc())
            .limit(100)
            .all()
        )
    values = [meta, *[serialize(row) for row in rows]]
    return analyze_values(query, values, as_of), values


async def capture_market_books(db, condition_id, venue):
    clob, gamma, context = await venue.market(condition_id)
    raw = {
        "event_type": "market_metadata",
        "market": condition_id,
        "asset_id": context.outcomes[0].token_id,
        "clob": clob,
        "gamma": gamma,
        "context": context.model_dump(mode="json"),
    }
    meta = append_event(db, raw, received_at=context.observed_at, connection_id="native-rest")
    saved, failures = [], []
    for outcome in context.outcomes:
        try:
            raw_book, book = await venue.book(outcome.token_id, condition_id)
            row = append_event(
                db,
                {**raw_book, "event_type": "book", "_rule_hash": context.rules.content_hash},
                received_at=book.observed_at,
                connection_id="native-rest",
            )
            saved.append(row.id)
        except (ValueError, ArithmeticError, httpx.HTTPError):
            append_event(
                db,
                {
                    "event_type": "source_status",
                    "asset_id": outcome.token_id,
                    "market": condition_id,
                    "status": "SOURCE_CAPTURE_UNAVAILABLE",
                },
                received_at=utc_now(),
                connection_id="native-rest",
            )
            failures.append(outcome.token_id)
    return {
        "metadata_id": meta.id,
        "snapshot_ids": saved,
        "failed_tokens": failures,
        "status": "captured" if not failures else "partial",
    }


def save_research(db, query, account):
    from uuid import uuid4

    from app.models.analytics.book_research_run import BookResearchRun
    from app.models.market_data.venue_observation import (
        ObservationDataset,
        ObservationDatasetMember,
    )
    from app.services.market_data.venue_observations import ensure_transaction

    ensure_transaction(db)
    query = query.model_copy(update={"as_of": query.as_of or utc_now()})
    result, values = load_analysis(db, query)
    manifest = {
        "version": "venue-observation-v1",
        "as_of": query.as_of.isoformat(),
        "observations": [
            {
                "snapshot_id": v["snapshot_id"],
                "content_hash": v["content_hash"],
                "receipt_hash": digest(v),
            }
            for v in values
        ],
    }
    identity = digest(manifest)
    if db.get(ObservationDataset, identity) is None:
        from sqlalchemy.exc import IntegrityError

        try:
            with db.begin_nested():
                db.add(
                    ObservationDataset(
                        id=identity,
                        manifest_json=json.dumps(manifest, sort_keys=True),
                        created_at=utc_now(),
                    )
                )
                db.flush()
                for value in values:
                    db.add(
                        ObservationDatasetMember(
                            dataset_id=identity, observation_id=value["snapshot_id"]
                        )
                    )
                db.flush()
        except IntegrityError:
            if db.get(ObservationDataset, identity) is None:
                raise
    payload = query.model_dump(mode="json")
    row = BookResearchRun(
        id=str(uuid4()),
        user_id=account.owner_user_id,
        account_id=account.id,
        dataset_id=identity,
        input_json=json.dumps(payload, sort_keys=True),
        input_hash=digest(payload),
        result_json=json.dumps(result, sort_keys=True),
        result_hash=digest(result),
    )
    db.add(row)
    db.flush()
    return {"id": row.id, "result_hash": row.result_hash, "result": result}


def replay_research(db, row):
    from app.services.market_data.venue_observations import replay_dataset

    payload = json.loads(row.input_json)
    result = json.loads(row.result_json)
    if digest(payload) != row.input_hash or digest(result) != row.result_hash:
        raise ValueError("PRIVATE_RUN_INTEGRITY_FAILURE")
    captured = replay_dataset(db, row.dataset_id)
    query = ResearchQuery.model_validate(payload)
    recomputed = analyze_values(query, captured["observations"], query.as_of)
    if digest(recomputed) != row.result_hash:
        raise ValueError("ANALYSIS_REPLAY_MISMATCH")
    return {"id": row.id, "result_hash": row.result_hash, "result": recomputed}
