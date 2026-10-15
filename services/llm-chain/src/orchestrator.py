"""Deterministic orchestration layer for multi-step analysis plans.

The orchestrator turns an ``intent`` + payload into a plan of subtasks,
executes each subtask on the backend that owns it — capability-aware
routing, ``llm-chain`` by default and ``cli-agent`` only when the
request's provider resolves to GitHub Models — and synthesises one
answer from the step outputs.

Failure policy is **fail-soft**: a step that errors, including a step
that times out, is recorded as a typed :class:`StepError` and the plan
still produces a synthesis with ``partial=true``. Nothing is retried
silently (master invariants I8/I12), and every step is bounded by a
timeout so a hung backend can never hang the RPC.

This module deliberately keeps its imports light: ``langchain`` and the
generated protobuf modules are imported lazily inside the functions that
need them so the planner, router and executor stay importable (and
testable) without the AI stack installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from polymarket_settings import TASK_TIERS, ModelTier

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────

#: Proto ``LLMProvider`` value for GitHub Models (``LLM_PROVIDER_GITHUB``).
#: Only ``cli-agent`` serves that provider; ``llm-chain`` maps it to "no
#: override" and falls back to its own service default.
PROVIDER_GITHUB = 6

#: Task key registered in ``TASK_TIERS`` for the synthesis call.
ORCHESTRATION_TASK = "orchestration"

#: Hard cap on executed steps per plan (roadmap risk: latency and cost).
MAX_STEPS = 6

#: Markets a single sweep scans, and how many get the deep-dive fan-out.
MAX_SWEEP_MARKETS = 20
SWEEP_TOP_N = 3

#: Default per-step timeout in seconds. ``llm-chain``'s settings carry no
#: ``grpc_timeout`` field (that lives in the backend), so the orchestrator
#: resolves it from the environment and otherwise uses this constant.
DEFAULT_STEP_TIMEOUT_SECONDS = 120.0

#: Step outputs are truncated before they reach the synthesis prompt.
MAX_STEP_CHARS = 4000
MAX_SYNTHESIS_CHARS = 24000


class AIBackend(StrEnum):
    """Backends that can execute an orchestration step."""

    LLM_CHAIN = "llm-chain"
    CLI_AGENT = "cli-agent"


#: Every RPC on ``AnalysisService`` (15 pre-existing + the 2 orchestration
#: ones). ``llm-chain`` implements all of them.
SERVICE_RPCS: frozenset[str] = frozenset(
    {
        "AnalyzeMarket",
        "QuickAnalysis",
        "ScanMarkets",
        "AssessRisk",
        "GenerateTradePlan",
        "AnalyzeMarketStream",
        "AnalyzeTrader",
        "EvaluateCopyTrade",
        "EvaluateInversePosition",
        "AnalyzeSentiment",
        "DiscoverBestTrade",
        "IndexMarketsRAG",
        "GenerateNews",
        "GenerateNewsBatch",
        "HealthCheck",
        "OrchestrateAnalysis",
        "OrchestrateAnalysisStream",
    }
)

#: RPCs ``cli-agent`` actually implements. It is missing
#: ``AnalyzeSentiment``, ``DiscoverBestTrade``, ``IndexMarketsRAG``,
#: ``GenerateNews``, ``GenerateNewsBatch`` and both orchestration RPCs —
#: the generated base class answers UNIMPLEMENTED for those, which is why
#: routing must consult this set before dispatching.
CLI_AGENT_RPCS: frozenset[str] = frozenset(
    SERVICE_RPCS
    - {
        "AnalyzeSentiment",
        "DiscoverBestTrade",
        "IndexMarketsRAG",
        "GenerateNews",
        "GenerateNewsBatch",
        "OrchestrateAnalysis",
        "OrchestrateAnalysisStream",
    }
)

#: Which RPCs each backend can serve. Kept next to ``ROUTING`` so the two
#: cannot drift apart unnoticed.
BACKEND_CAPABILITIES: dict[AIBackend, frozenset[str]] = {
    AIBackend.LLM_CHAIN: SERVICE_RPCS,
    AIBackend.CLI_AGENT: CLI_AGENT_RPCS,
}

#: Default backend for every schedulable step. ``llm-chain`` owns every
#: chain plus the research MCP client, so it is the default everywhere.
DEFAULT_BACKEND = AIBackend.LLM_CHAIN

#: RPCs a planner may schedule. Streaming, news, RAG and orchestration RPCs
#: are never scheduled as steps (they are either not chain-backed or are the
#: orchestration entrypoint itself).
STEP_RPCS: frozenset[str] = frozenset(
    {
        "AnalyzeMarket",
        "QuickAnalysis",
        "ScanMarkets",
        "AssessRisk",
        "GenerateTradePlan",
        "AnalyzeTrader",
        "EvaluateCopyTrade",
        "EvaluateInversePosition",
        "AnalyzeSentiment",
        "DiscoverBestTrade",
    }
)

#: Per-RPC routing table. ``llm-chain`` is the default for every step; the
#: request's provider is what can move a step to ``cli-agent`` (see
#: :func:`route`).
ROUTING: dict[str, AIBackend] = dict.fromkeys(sorted(STEP_RPCS), DEFAULT_BACKEND)

#: ``TradingAnalysisChain`` method that implements each schedulable RPC.
CHAIN_METHODS: dict[str, str] = {
    "AnalyzeMarket": "analyze_market",
    "QuickAnalysis": "quick_analysis",
    "ScanMarkets": "scan_markets",
    "AssessRisk": "assess_risk",
    "GenerateTradePlan": "generate_trade_plan",
    "AnalyzeTrader": "analyze_trader",
    "EvaluateCopyTrade": "evaluate_copy_trade",
    "EvaluateInversePosition": "evaluate_inverse_position",
    "AnalyzeSentiment": "analyze_sentiment",
    "DiscoverBestTrade": "discover_best_trade",
}

#: Proto request message for each schedulable RPC (the cli-agent path).
REQUEST_TYPES: dict[str, str] = {
    "AnalyzeMarket": "MarketAnalysisRequest",
    "QuickAnalysis": "QuickAnalysisRequest",
    "ScanMarkets": "MarketScanRequest",
    "AssessRisk": "RiskAssessmentRequest",
    "GenerateTradePlan": "TradePlanRequest",
    "AnalyzeTrader": "TraderAnalysisRequest",
    "EvaluateCopyTrade": "CopyTradeEvaluationRequest",
    "EvaluateInversePosition": "InversePositionEvaluationRequest",
    "AnalyzeSentiment": "SentimentAnalysisRequest",
    "DiscoverBestTrade": "DiscoverBestTradeRequest",
}


# ── Errors ───────────────────────────────────────────────────


class OrchestrationError(RuntimeError):
    """Base class for failures that abort a whole plan."""


class UnknownIntentError(OrchestrationError):
    """The intent has no registered planner."""


class PlanError(OrchestrationError):
    """The payload is missing a field the intent's planner requires."""


class UnroutableStepError(OrchestrationError):
    """No backend implements the step's RPC."""


class StepError(RuntimeError):
    """One step failed.

    Always carries the step name so the caller can tell *which* subtask
    degraded; the plan still completes with ``partial=true``.
    """

    def __init__(self, step: str, message: str, backend: str = ""):
        super().__init__(f"step '{step}' failed: {message}")
        self.step = step
        self.message = message
        self.backend = backend


# ── Plan model ───────────────────────────────────────────────


@dataclass
class Step:
    """One schedulable subtask.

    ``request`` holds proto-shaped field values; a string of the form
    ``"$<step>.<path>"`` is resolved against that step's output at
    execution time (see :func:`_resolve_request`). ``parallel_group``
    marks steps that may run concurrently — only steps that share a group
    and do not depend on each other are ever gathered.
    """

    name: str
    rpc: str
    request: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    parallel_group: int = 0


@dataclass
class Plan:
    """An ordered list of steps for one intent."""

    intent: str
    steps: list[Step] = field(default_factory=list)


@dataclass
class StepResult:
    """Outcome of one executed step."""

    step: str
    ok: bool
    backend: str
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "ok": self.ok,
            "backend": self.backend,
            "output": self.output,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass
class OrchestrationResult:
    """The merged outcome of a whole plan."""

    success: bool
    synthesis: str
    steps: list[StepResult] = field(default_factory=list)
    partial: bool = False
    metadata: dict[str, str] = field(default_factory=dict)


#: Callable that runs one step and returns its output dict.
StepExecutor = Callable[[Step, AIBackend, dict[str, Any]], Awaitable[dict[str, Any]]]

#: Callable that merges step results into the synthesis text.
Synthesizer = Callable[[list[StepResult]], Awaitable[str]]


# ── Metrics ──────────────────────────────────────────────────

_metrics: dict[str, int] = {
    "plans_total": 0,
    "plans_partial": 0,
    "steps_total": 0,
    "steps_ok": 0,
    "steps_failed": 0,
    "steps_timed_out": 0,
    "steps_routed_cli_agent": 0,
    "synthesis_fallbacks": 0,
    "step_duration_ms_total": 0,
}


def get_orchestration_metrics() -> dict[str, int]:
    """Snapshot of orchestrator counters (same style as the other services)."""
    return dict(_metrics)


def _bump(key: str, amount: int = 1) -> None:
    _metrics[key] = _metrics.get(key, 0) + amount


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


# ── Routing ──────────────────────────────────────────────────


def _provider_of(llm_config: Any) -> int:
    """Read the provider enum off a proto ``LLMConfig`` (0 when absent)."""
    if llm_config is None:
        return 0
    try:
        return int(llm_config.provider)
    except (AttributeError, TypeError, ValueError):
        return 0


def route(step: Step, llm_config: Any = None) -> AIBackend:
    """Resolve the backend for ``step`` — capability-aware, ``llm-chain`` first.

    ``llm-chain`` owns every chain and the research MCP client, so it is the
    default. A request whose provider resolves to GitHub Models is routed to
    ``cli-agent`` (the only backend serving that provider) — unless the
    step's RPC is one ``cli-agent`` does not implement, in which case the
    step stays on ``llm-chain``.

    Known degradation: a GitHub-provider step that falls back this way runs
    on ``llm-chain`` with *its* service-default provider, because
    ``llm-chain`` cannot authenticate to GitHub Models. v1 has no way
    around that (see the plan's handoff notes).
    """
    target = ROUTING.get(step.rpc, DEFAULT_BACKEND)
    if _provider_of(llm_config) == PROVIDER_GITHUB:
        target = AIBackend.CLI_AGENT
    if step.rpc not in BACKEND_CAPABILITIES[target]:
        logger.info(
            "Step %s (%s) is not served by %s; executing on %s",
            step.name,
            step.rpc,
            target,
            DEFAULT_BACKEND,
        )
        target = DEFAULT_BACKEND
    if step.rpc not in BACKEND_CAPABILITIES[target]:
        raise UnroutableStepError(f"no backend implements {step.rpc}")
    return target


# ── Planners ─────────────────────────────────────────────────


def _required(payload: dict[str, Any], field_name: str, intent: str) -> Any:
    value = payload.get(field_name)
    if value is None or value == "":
        raise PlanError(f"intent {intent!r} requires payload field {field_name!r}")
    return value


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    return int(_as_float(value, float(default)))


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _market_entry(market: Any, intent: str) -> dict[str, Any]:
    """Normalise one caller-supplied market into AnalyzeMarket fields."""
    if not isinstance(market, dict):
        raise PlanError(f"intent {intent!r} requires market objects, got {type(market).__name__}")
    title = _as_text(market.get("title") or market.get("question") or market.get("market_title"))
    if not title:
        raise PlanError(f"intent {intent!r}: every market needs a 'title' or 'question'")
    yes_price = _as_float(
        market.get("yes_price", market.get("yes", 0.5)),
        0.5,
    )
    return {
        "title": title,
        "description": _as_text(market.get("description"), title),
        "yes_price": yes_price,
        "no_price": _as_float(market.get("no_price", market.get("no", 1.0 - yes_price))),
        "volume_24h": _as_float(market.get("volume_24h", market.get("volume24hr", 0.0))),
        "end_date": _as_text(market.get("end_date", market.get("endDate", ""))),
    }


def plan_market_deep_dive(payload: dict[str, Any]) -> list[Step]:
    """AnalyzeMarket(include_research=True) → AssessRisk → GenerateTradePlan."""
    intent = "market_deep_dive"
    market_title = _required(payload, "market_title", intent)
    market_title = str(market_title)
    market_description = _as_text(payload.get("market_description"), market_title)
    yes_price = _as_float(payload.get("yes_price"), 0.5)
    position_size = _as_float(payload.get("position_size"), 100.0)
    return [
        Step(
            name="analyze_market",
            rpc="AnalyzeMarket",
            request={
                "market_title": market_title,
                "market_description": market_description,
                "yes_price": yes_price,
                "no_price": _as_float(payload.get("no_price"), 1.0 - yes_price),
                "volume_24h": _as_float(payload.get("volume_24h"), 0.0),
                "end_date": _as_text(payload.get("end_date")),
                "include_research": True,
            },
        ),
        Step(
            name="assess_risk",
            rpc="AssessRisk",
            depends_on=("analyze_market",),
            request={
                "market_title": market_title,
                "position_size": position_size,
                "entry_price": _as_float(payload.get("entry_price"), yes_price),
                "days_to_expiry": _as_int(payload.get("days_to_expiry"), 30),
                "correlation_info": _as_text(
                    payload.get("correlation_info"), "No correlation data"
                ),
            },
        ),
        Step(
            name="trade_plan",
            rpc="GenerateTradePlan",
            depends_on=("assess_risk",),
            request={
                "action": _as_text(payload.get("action"), "buy_yes"),
                "market_title": market_title,
                "target_size": _as_float(payload.get("target_size"), position_size),
                "current_price": _as_float(payload.get("current_price"), yes_price),
                "order_book": payload.get("order_book")
                if isinstance(payload.get("order_book"), dict)
                else {},
            },
        ),
    ]


def plan_opportunity_sweep(payload: dict[str, Any]) -> list[Step]:
    """ScanMarkets → per-top-3 AnalyzeMarket (parallel) → AssessRisk.

    The shortlist is chosen deterministically by 24h volume: planning is
    code, not an LLM, in v1.
    """
    intent = "opportunity_sweep"
    raw_markets = payload.get("markets")
    if not isinstance(raw_markets, list) or not raw_markets:
        raise PlanError(f"intent {intent!r} requires a non-empty payload field 'markets'")
    markets = [_market_entry(market, intent) for market in raw_markets[:MAX_SWEEP_MARKETS]]
    shortlist = sorted(markets, key=lambda m: m["volume_24h"], reverse=True)[:SWEEP_TOP_N]

    steps = [Step(name="scan_markets", rpc="ScanMarkets", request={"markets": markets})]
    for index, market in enumerate(shortlist, start=1):
        steps.append(
            Step(
                name=f"deep_dive_{index}",
                rpc="AnalyzeMarket",
                depends_on=("scan_markets",),
                parallel_group=1,
                request={
                    "market_title": market["title"],
                    "market_description": market["description"],
                    "yes_price": market["yes_price"],
                    "no_price": market["no_price"],
                    "volume_24h": market["volume_24h"],
                    "end_date": market["end_date"],
                    "include_research": True,
                },
            )
        )
    steps.append(
        Step(
            name="assess_risk",
            rpc="AssessRisk",
            depends_on=tuple(f"deep_dive_{index}" for index in range(1, len(shortlist) + 1)),
            request={
                "market_title": shortlist[0]["title"],
                "position_size": _as_float(payload.get("position_size"), 100.0),
                "entry_price": shortlist[0]["yes_price"],
                "days_to_expiry": _as_int(payload.get("days_to_expiry"), 30),
                "correlation_info": _as_text(
                    payload.get("correlation_info"), "No correlation data"
                ),
            },
        )
    )
    return steps


def plan_trader_diligence(payload: dict[str, Any]) -> list[Step]:
    """AnalyzeTrader → EvaluateCopyTrade → AssessRisk."""
    intent = "trader_diligence"
    wallet_address = str(_required(payload, "wallet_address", intent))
    market_title = str(_required(payload, "market_title", intent))
    return [
        Step(
            name="analyze_trader",
            rpc="AnalyzeTrader",
            request={
                "wallet_address": wallet_address,
                "display_name": _as_text(payload.get("display_name")),
                "total_pnl": _as_float(payload.get("total_pnl"), 0.0),
                "win_rate": _as_float(payload.get("win_rate"), 0.0),
                "trade_count": _as_int(payload.get("trade_count"), 0),
                "markets_traded": _as_int(payload.get("markets_traded"), 0),
                "recent_trades_json": _as_text(payload.get("recent_trades_json"), "[]"),
            },
        ),
        Step(
            name="evaluate_copy_trade",
            rpc="EvaluateCopyTrade",
            depends_on=("analyze_trader",),
            request={
                "trader_wallet": wallet_address,
                # Cross-step reference: the analyst profile produced above.
                "trader_stats": "$analyze_trader.analysis",
                "market_id": _as_text(payload.get("market_id")),
                "market_title": market_title,
                "trade_side": _as_text(payload.get("trade_side"), "BUY"),
                "trade_size": _as_float(payload.get("trade_size"), 0.0),
                "current_price": _as_float(payload.get("current_price"), 0.5),
                "user_risk_profile": _as_text(
                    payload.get("user_risk_profile"), "max_position_daily_loss"
                ),
            },
        ),
        Step(
            name="assess_risk",
            rpc="AssessRisk",
            depends_on=("evaluate_copy_trade",),
            request={
                "market_title": market_title,
                "position_size": _as_float(payload.get("position_size"), 100.0),
                "entry_price": _as_float(payload.get("current_price"), 0.5),
                "days_to_expiry": _as_int(payload.get("days_to_expiry"), 30),
                "correlation_info": _as_text(
                    payload.get("correlation_info"), "No correlation data"
                ),
            },
        ),
    ]


#: Intent → planner. New intents are added here and nowhere else.
PLANNERS: dict[str, Callable[[dict[str, Any]], list[Step]]] = {
    "market_deep_dive": plan_market_deep_dive,
    "opportunity_sweep": plan_opportunity_sweep,
    "trader_diligence": plan_trader_diligence,
}


def _subset_with_dependencies(planned: list[Step], requested: list[str]) -> list[Step]:
    """Keep the requested steps plus their transitive dependencies, in plan order."""
    by_name = {step.name: step for step in planned}
    keep: set[str] = set()
    queue = [name for name in requested if name in by_name]
    while queue:
        name = queue.pop()
        if name in keep:
            continue
        keep.add(name)
        queue.extend(dep for dep in by_name[name].depends_on if dep in by_name)
    return [step for step in planned if step.name in keep]


def build_plan(
    intent: str,
    payload: dict[str, Any] | None = None,
    steps: list[str] | None = None,
) -> Plan:
    """Build the :class:`Plan` for ``intent`` from ``payload``.

    ``steps`` optionally restricts the plan to the named steps (their
    dependencies are pulled in automatically so the plan stays runnable).
    """
    planner = PLANNERS.get(intent)
    if planner is None:
        known = ", ".join(sorted(PLANNERS))
        raise UnknownIntentError(f"unknown orchestration intent {intent!r}; known: {known}")
    planned = planner(payload or {})
    if steps:
        planned = _subset_with_dependencies(planned, steps)
    if not planned:
        raise PlanError(f"intent {intent!r} produced no executable steps")
    return Plan(intent=intent, steps=planned)


# ── Request helpers ──────────────────────────────────────────


def _resolve_value(value: Any, outputs: dict[str, Any]) -> Any:
    """Resolve a ``"$<step>.<path>"`` reference against prior step outputs."""
    if not isinstance(value, str) or not value.startswith("$"):
        return value
    step_name, _, path = value[1:].partition(".")
    cursor: Any = outputs.get(step_name)
    if cursor is None:
        return None
    for part in path.split(".") if path else []:
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        elif isinstance(cursor, list) and part.isdigit() and int(part) < len(cursor):
            cursor = cursor[int(part)]
        else:
            return None
    return cursor


def _resolve_request(step: Step, outputs: dict[str, Any]) -> dict[str, Any]:
    """Copy ``step.request`` with refs resolved; unresolvable fields drop out."""
    resolved: dict[str, Any] = {}
    for key, value in step.request.items():
        resolved_value = _resolve_value(value, outputs)
        if resolved_value is None:
            logger.debug("Step %s: field %r unresolved, sending without it", step.name, key)
            continue
        resolved[key] = resolved_value
    return resolved


def _as_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _chain_kwargs(rpc: str, request: dict[str, Any]) -> dict[str, Any]:
    """Adapt a proto-shaped request to the chain method's signature."""
    kwargs = dict(request)
    if rpc == "GenerateTradePlan":
        # The chain takes the parsed order book; the proto field is a JSON string.
        raw = kwargs.pop("order_book_json", None)
        if raw is None:
            raw = kwargs.pop("order_book", None)
        else:
            kwargs.pop("order_book", None)
        kwargs["order_book"] = _as_dict(raw)
    return kwargs


def _proto_kwargs(rpc: str, request: dict[str, Any]) -> dict[str, Any]:
    """Adapt a chain-shaped request to the proto request message."""
    kwargs = dict(request)
    if rpc == "GenerateTradePlan":
        order_book = kwargs.pop("order_book", None)
        kwargs.pop("order_book_json", None)
        kwargs["order_book_json"] = json.dumps(order_book or {})
    return kwargs


# ── Execution ────────────────────────────────────────────────


@dataclass
class _RunContext:
    """Everything one step execution needs."""

    llm_config: Any
    run_step: StepExecutor
    timeout: float


def _step_timeout() -> float:
    """Per-step timeout in seconds (env override, else the constant)."""
    raw = os.environ.get("ORCHESTRATION_STEP_TIMEOUT", "")
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_STEP_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_STEP_TIMEOUT_SECONDS


def _failed(step: Step, backend: Any, exc: BaseException, started: float) -> StepResult:
    """Build a failed :class:`StepResult` and count it."""
    _bump("steps_failed")
    message = exc.message if isinstance(exc, StepError) else str(exc)
    logger.warning(
        "Orchestration step %s failed on %s: %s",
        step.name,
        str(backend) if backend else "unrouted",
        message,
    )
    return StepResult(
        step=step.name,
        ok=False,
        backend=str(backend) if backend else "",
        error=message if isinstance(exc, StepError) else f"{type(exc).__name__}: {message}",
        duration_ms=_elapsed_ms(started),
    )


async def _run_one(step: Step, ctx: _RunContext, outputs: dict[str, Any]) -> StepResult:
    """Execute one step, converting every failure into a failed result."""
    started = time.monotonic()
    try:
        backend = route(step, ctx.llm_config)
    except OrchestrationError as exc:
        return _failed(step, "", exc, started)
    if backend is AIBackend.CLI_AGENT:
        _bump("steps_routed_cli_agent")
    request = _resolve_request(step, outputs)
    try:
        output = await asyncio.wait_for(ctx.run_step(step, backend, request), timeout=ctx.timeout)
    except TimeoutError:
        _bump("steps_timed_out")
        return _failed(
            step,
            backend,
            StepError(step.name, f"timed out after {ctx.timeout:g}s", str(backend)),
            started,
        )
    except StepError as exc:
        return _failed(step, backend, exc, started)
    except Exception as exc:
        # One bad step must never abort the plan: fail soft and keep going.
        return _failed(step, backend, exc, started)
    _bump("step_duration_ms_total", _elapsed_ms(started))
    return StepResult(
        step=step.name,
        ok=True,
        backend=str(backend),
        output=output if isinstance(output, dict) else {"analysis": str(output)},
        duration_ms=_elapsed_ms(started),
    )


async def execute_plan(
    plan: Plan,
    *,
    llm_config: Any = None,
    chain: Any = None,
    executor: StepExecutor | None = None,
    step_timeout: float | None = None,
    max_steps: int = MAX_STEPS,
    user_id: str = "",
) -> AsyncIterator[StepResult]:
    """Execute ``plan`` step by step, yielding each :class:`StepResult`.

    Steps whose dependencies have all succeeded and that share a non-zero
    ``parallel_group`` run concurrently through ``asyncio.gather``; every
    other step runs alone, in plan order. A step runs when *at least one* of
    its dependencies succeeded — references to failed dependencies are
    dropped from its request (see :func:`_resolve_request`) — and is only
    skipped when every dependency failed, because then it has no input left
    to work from. Each step is bounded by ``step_timeout`` so nothing hangs.
    """
    _bump("plans_total")
    steps = list(plan.steps)
    if len(steps) > max_steps:
        logger.warning("Plan %r capped from %d to %d steps", plan.intent, len(steps), max_steps)
        steps = steps[:max_steps]
    ctx = _RunContext(
        llm_config=llm_config,
        run_step=executor or _make_default_executor(chain, user_id),
        timeout=step_timeout or _step_timeout(),
    )
    outputs: dict[str, Any] = {}
    remaining: deque[Step] = deque(steps)
    while remaining:
        step = remaining.popleft()
        missing = [dep for dep in step.depends_on if dep not in outputs]
        if step.depends_on and len(missing) == len(step.depends_on):
            _bump("steps_total")
            yield _failed(
                step,
                "",
                StepError(step.name, f"skipped: every dependency failed ({missing[0]!r} first)"),
                time.monotonic(),
            )
            continue
        batch = [step]
        if step.parallel_group:
            # Provably independent siblings: no dependency between them and
            # all their inputs are already available.
            while remaining and remaining[0].parallel_group == step.parallel_group:
                candidate = remaining[0]
                if any(dep not in outputs for dep in candidate.depends_on):
                    break
                remaining.popleft()
                batch.append(candidate)
        if len(batch) > 1:
            results = await asyncio.gather(*(_run_one(item, ctx, outputs) for item in batch))
        else:
            results = [await _run_one(step, ctx, outputs)]
        for result in results:
            _bump("steps_total")
            if result.ok:
                _bump("steps_ok")
                outputs[result.step] = result.output
            yield result


# ── Step executors ───────────────────────────────────────────

_cli_agent_stub: Any = None


def _cli_agent_address() -> str:
    """cli-agent address, read lazily so environment changes take effect.

    The compose service name is the default: both services share the default
    compose network, so no extra plumbing is needed there.
    """
    host = os.environ.get("CLI_AGENT_GRPC_HOST", "cli-agent")
    port = os.environ.get("CLI_AGENT_GRPC_PORT", "50052")
    return f"{host}:{port}"


def _get_cli_agent_stub() -> Any:
    """Lazily create the shared cli-agent stub.

    ``llm-chain``'s settings cannot carry the address (the field lives in the
    backend config), so it comes from the environment with the compose
    service name as the default — both services share the default compose
    network, so no extra plumbing is needed.
    """
    global _cli_agent_stub
    if _cli_agent_stub is None:
        import analysis_pb2_grpc
        import grpc

        channel = grpc.aio.insecure_channel(
            _cli_agent_address(),
            options=[
                ("grpc.max_receive_message_length", 50 * 1024 * 1024),
                ("grpc.keepalive_time_ms", 10000),
            ],
        )
        _cli_agent_stub = analysis_pb2_grpc.AnalysisServiceStub(channel)
    return _cli_agent_stub


def reset_cli_agent_stub() -> None:
    """Drop the cached cli-agent stub (tests, or a redeployed agent)."""
    global _cli_agent_stub
    _cli_agent_stub = None


def _response_to_dict(response: Any) -> dict[str, Any]:
    """Normalise an ``AnalysisResponse`` into the chain's result shape."""
    result: dict[str, Any] = {"analysis": _as_text(getattr(response, "analysis", ""))}
    research = _as_text(getattr(response, "research_context", ""))
    if research:
        result["research_context"] = research
    timestamp = _as_text(getattr(response, "timestamp", ""))
    if timestamp:
        result["timestamp"] = timestamp
    metadata = dict(getattr(response, "metadata", {}) or {})
    if metadata:
        result["metadata"] = metadata
    return result


async def _run_on_cli_agent(
    step: Step, request: dict[str, Any], user_id: str = ""
) -> dict[str, Any]:
    """Dispatch the step's RPC to the cli-agent service over gRPC."""
    try:
        import analysis_pb2
        import grpc
    except ImportError as exc:  # pragma: no cover - stubs ship with the service
        raise StepError(
            step.name, f"generated stubs unavailable: {exc}", str(AIBackend.CLI_AGENT)
        ) from exc

    request_type = REQUEST_TYPES.get(step.rpc)
    if request_type is None or not hasattr(analysis_pb2, request_type):
        raise StepError(
            step.name, f"no proto request type for {step.rpc}", str(AIBackend.CLI_AGENT)
        )
    kwargs = _proto_kwargs(step.rpc, request)
    if user_id and "user_id" in getattr(analysis_pb2, request_type).DESCRIPTOR.fields_by_name:
        kwargs.setdefault("user_id", user_id)
    stub = _get_cli_agent_stub()
    method = getattr(stub, step.rpc)
    try:
        response = await method(
            getattr(analysis_pb2, request_type)(**kwargs),
            timeout=_step_timeout(),
        )
    except grpc.RpcError as exc:
        raise StepError(
            step.name,
            f"cli-agent RPC error {exc.code()}: {exc.details()}",
            str(AIBackend.CLI_AGENT),
        ) from exc
    if not getattr(response, "success", True):
        raise StepError(
            step.name,
            f"cli-agent returned failure: {_as_text(getattr(response, 'error', ''))}",
            str(AIBackend.CLI_AGENT),
        )
    return _response_to_dict(response)


async def _run_on_llm_chain(step: Step, request: dict[str, Any], chain: Any) -> dict[str, Any]:
    """Execute the step in-process on this service's chain."""
    method_name = CHAIN_METHODS.get(step.rpc)
    if method_name is None or chain is None:
        raise StepError(
            step.name,
            f"no in-process implementation for {step.rpc}",
            str(AIBackend.LLM_CHAIN),
        )
    result = await getattr(chain, method_name)(**_chain_kwargs(step.rpc, request))
    if not isinstance(result, dict):
        return {"analysis": str(result)}
    return result


def _make_default_executor(chain: Any, user_id: str = "") -> StepExecutor:
    """Build the default step executor for the given chain (and caller)."""

    async def _execute(step: Step, backend: AIBackend, request: dict[str, Any]) -> dict[str, Any]:
        if backend is AIBackend.CLI_AGENT:
            return await _run_on_cli_agent(step, request, user_id)
        return await _run_on_llm_chain(step, request, chain)

    return _execute


# ── Synthesis ────────────────────────────────────────────────


def _output_excerpt(output: dict[str, Any], limit: int = MAX_STEP_CHARS) -> str:
    """One-line, length-bounded excerpt of a step output."""
    text = output.get("analysis") or json.dumps(output, default=str)
    text = " ".join(str(text).split())
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _fallback_synthesis(results: list[StepResult]) -> str:
    """Deterministic synthesis used when the LLM call fails.

    Mirrors ``daily_report_service._fallback_report``: the content is
    factual (the step outputs), only the merged prose is missing.
    """
    if not results:
        return "No steps ran; no synthesis available."
    lines = ["Synthesis unavailable (LLM call failed) — raw step outputs:"]
    for result in results:
        if result.ok:
            lines.append(
                f"- {result.step} [{result.backend}, {result.duration_ms}ms]: "
                f"{_output_excerpt(result.output)}"
            )
        else:
            lines.append(
                f"- {result.step} [{result.backend or 'unrouted'}]: FAILED — {result.error}"
            )
    return "\n".join(lines)


def _synthesis_llm(chain: Any) -> Any:
    """Resolve the tier-correct LLM for the synthesis call.

    The chain's task-LLM resolver honours both the per-request provider
    override and the ``orchestration`` tier registered in ``TASK_TIERS``.
    """
    getter = getattr(chain, "_get_task_llm", None)
    if getter is not None:
        return getter(ORCHESTRATION_TASK)
    from src.llm_factory import create_llm_for_task  # lazy: imports langchain

    return create_llm_for_task(ORCHESTRATION_TASK)


async def _synthesize_with_llm(results: list[StepResult], chain: Any) -> str:
    """Render ``orchestration.synthesis`` and ask the LLM for the verdict."""
    from langchain_core.messages import HumanMessage  # lazy: not always needed

    from src.analysis_chain import PromptManager  # lazy: imports langchain

    prompts = PromptManager()
    template = prompts.get("orchestration", "synthesis")
    steps_json = json.dumps([result.to_dict() for result in results], default=str)
    if len(steps_json) > MAX_SYNTHESIS_CHARS:
        steps_json = steps_json[:MAX_SYNTHESIS_CHARS] + "… (truncated)"
    prompt = template.format(steps_json=steps_json)
    response = await _synthesis_llm(chain).ainvoke([HumanMessage(content=prompt)])
    text = _as_text(getattr(response, "content", ""))
    if not text:
        raise RuntimeError("synthesis returned empty content")
    return text


async def _produce_synthesis(
    results: list[StepResult],
    *,
    chain: Any,
    synthesizer: Synthesizer | None,
) -> tuple[str, bool]:
    """Return ``(synthesis, used_fallback)`` — never raises."""
    if synthesizer is not None:
        try:
            text = _as_text(await synthesizer(results))
        except Exception as exc:
            logger.warning("Orchestration synthesizer raised: %s", exc)
            text = ""
        return text or _fallback_synthesis(results), not text
    try:
        text = await _synthesize_with_llm(results, chain)
    except Exception as exc:
        logger.warning("Orchestration synthesis LLM call failed: %s", exc)
        text = ""
    return text or _fallback_synthesis(results), not text


async def _finish(
    plan: Plan,
    results: list[StepResult],
    *,
    chain: Any,
    synthesizer: Synthesizer | None = None,
) -> OrchestrationResult:
    """Merge step results into the final :class:`OrchestrationResult`."""
    partial = any(not result.ok for result in results)
    if partial:
        _bump("plans_partial")
    synthesis, used_fallback = await _produce_synthesis(
        results, chain=chain, synthesizer=synthesizer
    )
    if used_fallback:
        _bump("synthesis_fallbacks")
    metadata = {
        "intent": plan.intent,
        "steps_total": str(len(results)),
        "steps_failed": str(sum(1 for result in results if not result.ok)),
        "partial": str(partial).lower(),
        # Tier label lets the recorded verdict stay attributable (plan 04).
        "llm_tier": TASK_TIERS.get(ORCHESTRATION_TASK, ModelTier.STRONG).value,
        "synthesis_fallback": str(used_fallback).lower(),
        "backends": ",".join(sorted({result.backend for result in results if result.backend})),
    }
    return OrchestrationResult(
        success=bool(results) and bool(synthesis.strip()),
        synthesis=synthesis,
        steps=results,
        partial=partial,
        metadata=metadata,
    )


# ── Public entrypoints ───────────────────────────────────────


async def run_plan(
    plan: Plan,
    *,
    llm_config: Any = None,
    chain: Any = None,
    executor: StepExecutor | None = None,
    synthesizer: Synthesizer | None = None,
    step_timeout: float | None = None,
    max_steps: int = MAX_STEPS,
    user_id: str = "",
) -> OrchestrationResult:
    """Execute ``plan`` and return the merged outcome."""
    results: list[StepResult] = []
    async for result in execute_plan(
        plan,
        llm_config=llm_config,
        chain=chain,
        executor=executor,
        step_timeout=step_timeout,
        max_steps=max_steps,
        user_id=user_id,
    ):
        results.append(result)
    return await _finish(plan, results, chain=chain, synthesizer=synthesizer)


async def stream_plan(
    plan: Plan,
    *,
    llm_config: Any = None,
    chain: Any = None,
    executor: StepExecutor | None = None,
    synthesizer: Synthesizer | None = None,
    step_timeout: float | None = None,
    max_steps: int = MAX_STEPS,
    user_id: str = "",
) -> AsyncIterator[dict[str, Any]]:
    """Execute ``plan`` and yield one event per step, then the synthesis.

    Yields ``{"type": "step", ...}`` per executed step and a final
    ``{"type": "recommendation", ...}`` carrying the synthesis, so the
    streaming RPC and the SSE route share one event vocabulary.
    """
    results: list[StepResult] = []
    async for result in execute_plan(
        plan,
        llm_config=llm_config,
        chain=chain,
        executor=executor,
        step_timeout=step_timeout,
        max_steps=max_steps,
        user_id=user_id,
    ):
        results.append(result)
        yield {"type": "step", **result.to_dict()}
    outcome = await _finish(plan, results, chain=chain, synthesizer=synthesizer)
    yield {
        "type": "recommendation",
        "success": outcome.success,
        "synthesis": outcome.synthesis,
        "partial": outcome.partial,
        "metadata": outcome.metadata,
    }
