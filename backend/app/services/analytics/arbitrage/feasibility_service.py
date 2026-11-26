"""Point-in-time feasibility from frozen native book receipts (read-only).

The quote is not fetched live here. It is read from immutable captured venue
observations at an explicit ``as_of`` (via the #18 binary-book research path),
so an assessment can be replayed byte for byte later.
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AwareDatetime, Field

from app.services.analytics.arbitrage.arbitrage_solver import CostAssumptions
from app.services.analytics.arbitrage.depth_aware_pricer import PricedLeg
from app.services.analytics.arbitrage.execution_feasibility import (
    ArbCandidate,
    assess_candidate,
    canonical_digest,
)
from app.services.analytics.arbitrage.payout_validator import ContractRules
from app.services.market_data.binary_book import BinaryBook, Contract, MarketSpec
from app.services.market_data.book_research import (
    ResearchQuery,
    load_analysis,
    replay_research,
    save_research,
)
from app.utils.time import utc_now


class FeasibilityRequest(Contract):
    size: Decimal = Field(gt=0, le=1_000_000_000, decimal_places=8)
    fee_bps: Decimal | None = Field(default=None, ge=0, le=10000, decimal_places=8)
    fee_version: str | None = Field(default=None, min_length=1, max_length=80)
    costs: CostAssumptions = Field(default_factory=CostAssumptions)
    as_of: AwareDatetime | None = None
    capture_ids: list[Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]] | None = Field(
        default=None, max_length=201
    )
    max_age_seconds: int = Field(default=60, ge=1, le=300)
    persist: bool = False


def research_query(condition_id: str, request: FeasibilityRequest, as_of: datetime):
    return ResearchQuery(
        condition_id=condition_id,
        as_of=as_of,
        capture_ids=request.capture_ids,
        sizes=[request.size],
        fee_bps=request.fee_bps,
        fee_version=request.fee_version,
        max_age_seconds=request.max_age_seconds,
    )


def candidate_from_analysis(
    analysis: dict[str, Any], request: FeasibilityRequest, as_of: datetime
) -> ArbCandidate:
    """Pure: a frozen analysis document plus request gives one ArbCandidate."""
    if analysis.get("reason") in {"NO_ASOF_MARKET_METADATA", "MAPPING_OR_SIZE_RULES_UNAVAILABLE"}:
        raise LookupError(analysis["reason"])
    spec = MarketSpec.model_validate(analysis["spec"])
    books = {k: BinaryBook.model_validate(v) for k, v in analysis["books"].items()}
    legs = [
        PricedLeg(
            label=outcome,
            spec=spec,
            book=books[outcome],
            outcome=outcome,
            fee_bps=request.fee_bps,
            fee_version=request.fee_version,
        )
        for outcome in ("YES", "NO")
    ]
    rules = ContractRules(
        venue="polymarket",
        market_id=spec.condition_id,
        yes_token=spec.yes_token,
        no_token=spec.no_token,
        rule_hash=spec.rule_hash,
        neg_risk=False if spec.supported else None,
    )
    return assess_candidate(
        legs=legs,
        rules=[rules, rules],
        requested_size=request.size,
        costs=request.costs,
        as_of=as_of,
        max_age_seconds=Decimal(request.max_age_seconds),
    )


def assess_frozen(db, condition_id: str, request: FeasibilityRequest) -> dict[str, Any]:
    as_of = request.as_of or utc_now()
    query = research_query(condition_id, request, as_of)
    analysis, _ = load_analysis(db, query)
    candidate = candidate_from_analysis(analysis, request, as_of)
    return {
        "candidate": candidate.model_dump(mode="json"),
        "lineage": {
            "as_of": as_of.isoformat(),
            "capture_ids": analysis["capture_ids"],
            "metadata_id": analysis.get("metadata_id"),
            "rule_hash": analysis.get("rule_hash"),
            "source": "native_venue_observations",
            "credentials_included": False,
        },
    }


def save_run(db, condition_id: str, request: FeasibilityRequest, account) -> dict[str, Any]:
    from uuid import uuid4

    from app.models.analytics.arbitrage_feasibility_run import ArbitrageFeasibilityRun

    as_of = request.as_of or utc_now()
    frozen = request.model_copy(update={"as_of": as_of, "persist": False})
    research = save_research(db, research_query(condition_id, frozen, as_of), account)
    candidate = candidate_from_analysis(research["result"], frozen, as_of)
    payload = {"condition_id": condition_id, "request": frozen.model_dump(mode="json")}
    result = candidate.model_dump(mode="json")
    row = ArbitrageFeasibilityRun(
        id=str(uuid4()),
        user_id=account.owner_user_id,
        account_id=account.id,
        research_run_id=research["id"],
        candidate_id=candidate.candidate_id,
        input_json=json.dumps(payload, sort_keys=True),
        input_hash=canonical_digest(payload),
        result_json=json.dumps(result, sort_keys=True),
        result_hash=canonical_digest(result),
    )
    db.add(row)
    db.flush()
    return {
        "id": row.id,
        "research_run_id": row.research_run_id,
        "result_hash": row.result_hash,
        "candidate": result,
    }


def replay_run(db, row) -> dict[str, Any]:
    """Recompute from the frozen receipts; any drift or tampering raises ValueError."""
    from app.models.analytics.book_research_run import BookResearchRun

    payload = json.loads(row.input_json)
    stored = json.loads(row.result_json)
    if canonical_digest(payload) != row.input_hash or canonical_digest(stored) != row.result_hash:
        raise ValueError("ARB_RUN_INTEGRITY_FAILURE")
    research_row = db.get(BookResearchRun, row.research_run_id)
    if (
        research_row is None
        or research_row.account_id != row.account_id
        or research_row.user_id != row.user_id
    ):
        raise ValueError("ARB_RUN_RESEARCH_MISSING")
    replayed = replay_research(db, research_row)
    request = FeasibilityRequest.model_validate(payload["request"])
    candidate = candidate_from_analysis(replayed["result"], request, request.as_of)
    recomputed = candidate.model_dump(mode="json")
    if canonical_digest(recomputed) != row.result_hash:
        raise ValueError("ARB_REPLAY_MISMATCH")
    return {"id": row.id, "result_hash": row.result_hash, "candidate": recomputed}


__all__ = [
    "FeasibilityRequest",
    "assess_frozen",
    "candidate_from_analysis",
    "replay_run",
    "save_run",
]
