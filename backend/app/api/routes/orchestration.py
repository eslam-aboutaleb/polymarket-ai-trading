"""Multi-step AI orchestration routes (plan 01).

A single request fans out into a plan of subtasks — market deep dive,
opportunity sweep, trader diligence — that the llm-chain service executes
step by step and merges into one synthesis.

Two things are deliberate here:

* the route always talks to **llm-chain**, the only backend implementing
  ``OrchestrateAnalysis`` (``cli-agent`` serves 10 of the 17 RPCs), so a
  GitHub-Models provider is honoured *per step* inside the orchestrator
  through the request-level ``llm_config`` instead of by re-pointing the
  whole request;
* the stream reuses the SSE envelope the other ``/api/analysis`` routes
  use — a JSON object per ``data:`` line, terminal ``{"done": true}`` or
  ``{"error": ...}`` — so the frontend ``consumeSSEStream`` reader needs no
  new protocol.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from polymarket_settings import TASK_TIERS, ModelTier
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require_plan_quota
from app.api.routes.auth import get_current_user_from_token
from app.config import AIBackend
from app.grpc_clients.analysis_client import AnalysisClient
from app.models.user_settings import UserSettings
from app.services.entitlement_service import CAP_ORCHESTRATION
from app.services.experiment_service import resolve_experiment
from app.services.llm_gateway import get_llm_gateway
from app.services.prompt_version import current_prompt_version
from app.utils.database import get_db

router = APIRouter(prefix="/api/analysis/orchestrate", tags=["orchestration"])
logger = logging.getLogger(__name__)


# ── Schemas ──────────────────────────────────────────────────


class OrchestrateRequest(BaseModel):
    """One orchestration request: intent + payload for its planner."""

    intent: str = Field(
        ...,
        description="Planner key registered on the service, e.g. market_deep_dive",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Fields the intent's planner reads (market data, trader stats, ...)",
    )
    steps: list[str] | None = Field(
        default=None,
        description="Optional step subset to run (dependencies are pulled in automatically)",
    )


class OrchestrateResponse(BaseModel):
    """Merged outcome of a plan; ``partial`` marks a degraded run."""

    success: bool
    synthesis: str
    steps: list[dict[str, Any]]
    partial: bool
    metadata: dict[str, str]
    backend: str


# ── Helpers ──────────────────────────────────────────────────


def _orchestration_client() -> AnalysisClient:
    """A client pinned to llm-chain.

    ``OrchestrateAnalysis`` exists on llm-chain only, so the request can
    never be re-pointed at cli-agent the way a single-RPC call is. The
    user's provider still matters per step and travels inside
    ``llm_config``.
    """
    return AnalysisClient(backend=AIBackend.LLM_CHAIN)


def _orchestration_llm_config(db: Session, user_id: int | None) -> dict[str, Any]:
    """Resolve the request-level LLM config plus attribution keys.

    Gateway precedence (request override → user preference → system
    default) decides the provider; the provider is what the orchestrator
    uses to send GitHub-Models steps to cli-agent. The attribution keys
    have no proto LLMConfig fields, so they ride along inside the same
    dict and come back on the response metadata — that is what keeps an
    orchestrated verdict attributable for calibration (plan 04).
    """
    settings_record = None
    if user_id is not None:
        settings_record = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()

    gateway = get_llm_gateway()
    _backend, llm_config = gateway.resolve_provider(user_settings=settings_record)
    config: dict[str, Any] = dict(llm_config.to_proto_dict()) if llm_config else {}

    config["prompt_version"] = current_prompt_version()

    experiment = resolve_experiment(db, "orchestration", user_id)
    if experiment and experiment.get("experiment_id"):
        config["experiment_id"] = str(experiment["experiment_id"])
        config["variant"] = str(experiment.get("variant", ""))
        # An experiment arm's config is provider/model overrides, exactly
        # what the gateway would have supplied.
        overrides = experiment.get("config")
        if isinstance(overrides, dict):
            config.update(overrides)

    # The tier comes from the shared table so it cannot drift from the
    # service's own resolution of the same key.
    config["llm_tier"] = (
        config.get("llm_tier") or TASK_TIERS.get("orchestration", ModelTier.STRONG).value
    )
    return config


def _sse_event(payload: dict[str, Any]) -> str:
    """Render one SSE frame in the shared ``data: {json}`` envelope."""
    return f"data: {json.dumps(payload)}\n\n"


async def _orchestration_sse_stream(
    intent: str,
    payload: dict[str, Any],
    steps: list[str] | None,
    user_id: str,
    llm_config: dict[str, Any],
):
    """Yield one plan's events in the shared SSE envelope.

    Event order: a ``status`` open, one ``step`` per executed subtask, a
    ``recommendation`` with the synthesis, then the terminal ``done`` (or
    ``error``) event — the same wire shape as the opportunity scan streams.
    """
    yield _sse_event({"status": "started", "intent": intent})

    client = _orchestration_client()
    done_emitted = False
    try:
        async for event in client.orchestrate_stream(
            intent=intent,
            payload=payload,
            steps=steps,
            user_id=user_id,
            llm_config=llm_config,
        ):
            chunk_type = event.get("chunk_type")
            if chunk_type == "step":
                yield _sse_event({"step": event})
            elif chunk_type == "recommendation":
                partial = bool(event.get("partial"))
                yield _sse_event({"recommendation": event.get("synthesis", ""), "partial": partial})
                yield _sse_event({"done": True, "partial": partial})
                done_emitted = True
                return
            elif chunk_type == "error":
                yield _sse_event({"error": event.get("error", "orchestration failed")})
                return
        if not done_emitted:
            # The service stream ended without its terminal event.
            yield _sse_event({"done": True})
    finally:
        await client.close()


# ── Routes ───────────────────────────────────────────────────


@router.post(
    "",
    response_model=OrchestrateResponse,
    dependencies=[Depends(require_plan_quota(CAP_ORCHESTRATION))],
)
async def orchestrate(
    request: OrchestrateRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Run a multi-step analysis plan and return one merged synthesis.

    Gated on the ``orchestration`` capability and charged against the
    plan's daily budget: this is the most expensive surface in the product
    (a plan of subtasks per request), so it is the first place a quota
    belongs. Manual trading and single-shot analysis are untouched.
    """
    user_id = current_user.get("user_id")
    llm_config = _orchestration_llm_config(db, user_id)
    client = _orchestration_client()
    try:
        result = await client.orchestrate(
            intent=request.intent,
            payload=request.payload,
            steps=request.steps,
            user_id=str(user_id or ""),
            llm_config=llm_config,
        )
    except RuntimeError as exc:
        logger.exception("Orchestration request failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Orchestration backend unavailable",
        ) from exc
    finally:
        await client.close()

    if not result.get("success") and not result.get("steps"):
        # The service rejected the plan (unknown intent, bad payload); its
        # metadata carries the reason, including the known intent list.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.get("metadata", {}).get("error", "plan could not be built"),
        )
    return {**result, "backend": "llm_chain"}


@router.post(
    "/stream",
    dependencies=[Depends(require_plan_quota(CAP_ORCHESTRATION))],
)
async def orchestrate_stream(
    request: OrchestrateRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Stream a plan: one SSE event per step, then the synthesis."""
    user_id = current_user.get("user_id")
    llm_config = _orchestration_llm_config(db, user_id)
    return StreamingResponse(
        _orchestration_sse_stream(
            intent=request.intent,
            payload=request.payload,
            steps=request.steps,
            user_id=str(user_id or ""),
            llm_config=llm_config,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
