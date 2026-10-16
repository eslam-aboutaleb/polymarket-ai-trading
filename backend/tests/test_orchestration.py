"""Tests for the plan-01 AI agent orchestration layer.

The planner/router/executor live in ``services/llm-chain/src/orchestrator.py``
and are imported with the llm-chain source on ``sys.path`` (the same trick
``test_llm_factory_tiering.py`` uses), so they run inside the backend venv
without the langchain stack. Coroutines are driven with ``asyncio.run``
because the suite has no async test plugin.

The gRPC route is exercised through a bare app that mounts only the
orchestration router — ``main.py`` is owned by another change, and this
keeps the route's contract testable on its own.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import AIBackend
from app.grpc_clients import analysis_pb2
from app.utils.database import get_db

REPO_ROOT = Path(__file__).resolve().parents[2]
LLM_CHAIN_SRC = REPO_ROOT / "services" / "llm-chain"

WALLET = "0x" + "ab" * 20

MARKET_PAYLOAD = {
    "market_title": "Will BTC hit $150k by Dec 31?",
    "market_description": "Bitcoin price market",
    "yes_price": 0.62,
    "no_price": 0.38,
    "volume_24h": 250000.0,
    "end_date": "2026-12-31T00:00:00Z",
    "position_size": 250.0,
}

SWEEP_PAYLOAD = {
    "markets": [
        {"title": "Low volume", "yes_price": 0.4, "no_price": 0.6, "volume_24h": 10},
        {"title": "Top volume", "yes_price": 0.3, "no_price": 0.7, "volume_24h": 900},
        {"title": "Mid volume", "yes_price": 0.5, "no_price": 0.5, "volume_24h": 500},
        {"title": "Fourth", "yes_price": 0.2, "no_price": 0.8, "volume_24h": 400},
    ]
}

TRADER_PAYLOAD = {
    "wallet_address": WALLET,
    "display_name": "Whale",
    "total_pnl": 50000.0,
    "win_rate": 0.63,
    "trade_count": 120,
    "markets_traded": 40,
    "market_title": "Will BTC hit $150k by Dec 31?",
    "trade_side": "BUY",
    "trade_size": 25.0,
    "current_price": 0.62,
}


@pytest.fixture(scope="module")
def orchestrator():
    """Import ``src.orchestrator`` with the llm-chain source on ``sys.path``."""
    sys_path_added = str(LLM_CHAIN_SRC) not in __import__("sys").path
    if sys_path_added:
        __import__("sys").path.insert(0, str(LLM_CHAIN_SRC))
    try:
        from src import orchestrator

        yield orchestrator
    finally:
        if sys_path_added:
            __import__("sys").path.remove(str(LLM_CHAIN_SRC))
            for module_name in ("src.orchestrator", "src.config", "src"):
                __import__("sys").modules.pop(module_name, None)


def _llm_config(provider: int | None):
    """A proto LLMConfig for the given provider enum (None → no override)."""
    if provider is None:
        return None
    return analysis_pb2.LLMConfig(provider=provider)


def _names(plan) -> list[str]:
    return [step.name for step in plan.steps]


def _rpcs(plan) -> list[str]:
    return [step.rpc for step in plan.steps]


def _recording_executor(records: list[tuple[str, str, dict]], delay: float = 0.0):
    """Executor that records (step, backend, request) and returns canned output."""

    async def _execute(step, backend, request):
        records.append((step.name, str(backend), dict(request)))
        if delay:
            await asyncio.sleep(delay)
        return {"analysis": f"output of {step.name}"}

    return _execute


async def _echo_synthesizer(results):
    """Synthesizer double returning a fixed verdict."""
    return "SYNTHESIS"


# ── Planners ─────────────────────────────────────────────────


class TestPlanners:
    def test_market_deep_dive_step_order(self, orchestrator):
        plan = orchestrator.build_plan("market_deep_dive", MARKET_PAYLOAD)

        assert plan.intent == "market_deep_dive"
        assert _rpcs(plan) == ["AnalyzeMarket", "AssessRisk", "GenerateTradePlan"]
        # Each step consumes the previous one's output.
        assert [step.depends_on for step in plan.steps] == [
            (),
            ("analyze_market",),
            ("assess_risk",),
        ]
        # The market step is the research-enabled deep dive.
        assert plan.steps[0].request["include_research"] is True
        assert plan.steps[0].request["yes_price"] == pytest.approx(0.62)
        assert plan.steps[1].request["position_size"] == pytest.approx(250.0)
        assert plan.steps[2].request["action"] == "buy_yes"

    def test_opportunity_sweep_scans_then_fans_out_top_three(self, orchestrator):
        plan = orchestrator.build_plan("opportunity_sweep", SWEEP_PAYLOAD)

        assert _rpcs(plan) == [
            "ScanMarkets",
            "AnalyzeMarket",
            "AnalyzeMarket",
            "AnalyzeMarket",
            "AssessRisk",
        ]
        assert _names(plan)[0] == "scan_markets"
        # The scan sees every market; the fan-out only the top three.
        assert len(plan.steps[0].request["markets"]) == 4
        fan_out = plan.steps[1:4]
        assert [step.parallel_group for step in fan_out] == [1, 1, 1]
        # Deterministic shortlist: highest 24h volume first.
        assert [step.request["market_title"] for step in fan_out] == [
            "Top volume",
            "Mid volume",
            "Fourth",
        ]
        assert all(step.depends_on == ("scan_markets",) for step in fan_out)
        # The risk step waits for the whole fan-out batch.
        assert plan.steps[4].depends_on == ("deep_dive_1", "deep_dive_2", "deep_dive_3")

    def test_trader_diligence_uses_a_cross_step_reference(self, orchestrator):
        plan = orchestrator.build_plan("trader_diligence", TRADER_PAYLOAD)

        assert _rpcs(plan) == ["AnalyzeTrader", "EvaluateCopyTrade", "AssessRisk"]
        assert plan.steps[1].request["trader_stats"] == "$analyze_trader.analysis"
        assert plan.steps[1].request["trader_wallet"] == WALLET

    def test_every_intent_is_plannable(self, orchestrator):
        assert set(orchestrator.PLANNERS) == {
            "market_deep_dive",
            "opportunity_sweep",
            "trader_diligence",
        }
        payloads = {
            "market_deep_dive": MARKET_PAYLOAD,
            "opportunity_sweep": SWEEP_PAYLOAD,
            "trader_diligence": TRADER_PAYLOAD,
        }
        for intent, planner in orchestrator.PLANNERS.items():
            plan = orchestrator.build_plan(intent, payloads[intent])
            assert len(plan.steps) <= orchestrator.MAX_STEPS
            assert planner(payloads[intent]) == plan.steps

    def test_unknown_intent_is_rejected(self, orchestrator):
        with pytest.raises(orchestrator.UnknownIntentError):
            orchestrator.build_plan("not_a_real_intent", MARKET_PAYLOAD)

    def test_missing_required_payload_field_is_rejected(self, orchestrator):
        with pytest.raises(orchestrator.PlanError):
            orchestrator.build_plan("market_deep_dive", {"yes_price": 0.5})
        with pytest.raises(orchestrator.PlanError):
            orchestrator.build_plan("opportunity_sweep", {"markets": []})
        with pytest.raises(orchestrator.PlanError):
            orchestrator.build_plan("trader_diligence", {"wallet_address": WALLET})

    def test_requested_step_subset_pulls_in_dependencies(self, orchestrator):
        plan = orchestrator.build_plan("market_deep_dive", MARKET_PAYLOAD, steps=["trade_plan"])

        # Asking for the last step still runs its two dependencies first.
        assert _names(plan) == ["analyze_market", "assess_risk", "trade_plan"]

    def test_unknown_requested_step_is_ignored(self, orchestrator):
        plan = orchestrator.build_plan(
            "market_deep_dive", MARKET_PAYLOAD, steps=["assess_risk", "nope"]
        )
        assert _names(plan) == ["analyze_market", "assess_risk"]


# ── Routing ──────────────────────────────────────────────────


class TestRouting:
    def test_default_backend_is_llm_chain(self, orchestrator):
        plan = orchestrator.build_plan("market_deep_dive", MARKET_PAYLOAD)

        for step in plan.steps:
            assert orchestrator.route(step) is orchestrator.AIBackend.LLM_CHAIN
            assert orchestrator.route(step, _llm_config(None)) is orchestrator.AIBackend.LLM_CHAIN
            # Every non-GitHub provider stays on llm-chain.
            for provider in (0, 1, 2, 3, 4, 5):
                assert (
                    orchestrator.route(step, _llm_config(provider))
                    is orchestrator.AIBackend.LLM_CHAIN
                )

    def test_github_provider_routes_to_cli_agent(self, orchestrator):
        plan = orchestrator.build_plan("market_deep_dive", MARKET_PAYLOAD)
        github = _llm_config(6)  # LLM_PROVIDER_GITHUB

        for step in plan.steps:
            assert orchestrator.route(step, github) is orchestrator.AIBackend.CLI_AGENT

    def test_capability_guard_never_routes_a_missing_rpc_to_cli_agent(self, orchestrator):
        github = _llm_config(6)
        # cli-agent does not implement these RPCs, so a GitHub-provider
        # request must keep them on llm-chain instead of failing there.
        for rpc in ("AnalyzeSentiment", "DiscoverBestTrade", "GenerateNews", "IndexMarketsRAG"):
            step = orchestrator.Step(name="guarded", rpc=rpc)
            assert orchestrator.route(step, github) is orchestrator.AIBackend.LLM_CHAIN
            assert rpc not in orchestrator.BACKEND_CAPABILITIES[orchestrator.AIBackend.CLI_AGENT]

    def test_capability_sets_match_the_two_servicers(self, orchestrator):
        cli_agent = orchestrator.BACKEND_CAPABILITIES[orchestrator.AIBackend.CLI_AGENT]
        llm_chain = orchestrator.BACKEND_CAPABILITIES[orchestrator.AIBackend.LLM_CHAIN]

        assert cli_agent == {
            "AnalyzeMarket",
            "QuickAnalysis",
            "ScanMarkets",
            "AssessRisk",
            "GenerateTradePlan",
            "AnalyzeMarketStream",
            "AnalyzeTrader",
            "EvaluateCopyTrade",
            "EvaluateInversePosition",
            "HealthCheck",
        }
        # cli-agent's gaps are exactly the RPCs the guide calls out.
        assert orchestrator.SERVICE_RPCS - cli_agent == {
            "AnalyzeSentiment",
            "DiscoverBestTrade",
            "IndexMarketsRAG",
            "GenerateNews",
            "GenerateNewsBatch",
            "OrchestrateAnalysis",
            "OrchestrateAnalysisStream",
        }
        assert llm_chain == orchestrator.SERVICE_RPCS

    def test_every_planner_rpc_is_routable(self, orchestrator):
        payloads = {
            "market_deep_dive": MARKET_PAYLOAD,
            "opportunity_sweep": SWEEP_PAYLOAD,
            "trader_diligence": TRADER_PAYLOAD,
        }
        for intent, payload in payloads.items():
            for step in orchestrator.build_plan(intent, payload).steps:
                assert (
                    step.rpc in orchestrator.BACKEND_CAPABILITIES[orchestrator.AIBackend.LLM_CHAIN]
                )
                assert step.rpc in orchestrator.ROUTING
                assert step.rpc in orchestrator.CHAIN_METHODS


# ── Execution ────────────────────────────────────────────────


class TestExecution:
    def test_steps_run_in_plan_order(self, orchestrator):
        records: list[tuple[str, str, dict]] = []
        plan = orchestrator.build_plan("market_deep_dive", MARKET_PAYLOAD)

        outcome = asyncio.run(
            orchestrator.run_plan(
                plan,
                executor=_recording_executor(records),
                synthesizer=_echo_synthesizer,
            )
        )

        assert [name for name, _, _ in records] == [
            "analyze_market",
            "assess_risk",
            "trade_plan",
        ]
        assert outcome.success is True
        assert outcome.partial is False
        assert outcome.metadata["intent"] == "market_deep_dive"
        assert outcome.metadata["llm_tier"] == "strong"
        assert all(result.ok for result in outcome.steps)
        assert all(result.backend == "llm-chain" for result in outcome.steps)
        assert isinstance(outcome.steps[0].duration_ms, int)

    def test_cross_step_reference_is_resolved(self, orchestrator):
        records: list[tuple[str, str, dict]] = []
        plan = orchestrator.build_plan("trader_diligence", TRADER_PAYLOAD)

        asyncio.run(
            orchestrator.run_plan(
                plan,
                executor=_recording_executor(records),
                synthesizer=_echo_synthesizer,
            )
        )

        # The second step's request carries the first step's output.
        assert records[1][2]["trader_stats"] == "output of analyze_trader"
        assert records[0][1] == "llm-chain"

    def test_parallel_group_runs_concurrently(self, orchestrator):
        active = 0
        peak = 0

        async def _execute(step, backend, request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return {"analysis": step.name}

        plan = orchestrator.build_plan("opportunity_sweep", SWEEP_PAYLOAD)

        async def _collect():
            return [result async for result in orchestrator.execute_plan(plan, executor=_execute)]

        results = asyncio.run(_collect())

        assert peak == 3  # the three deep dives really overlapped
        assert [result.step for result in results] == [
            "scan_markets",
            "deep_dive_1",
            "deep_dive_2",
            "deep_dive_3",
            "assess_risk",
        ]

    def test_step_failure_is_fail_soft(self, orchestrator):
        async def _execute(step, backend, request):
            if step.name == "assess_risk":
                raise RuntimeError("risk engine exploded")
            return {"analysis": f"output of {step.name}"}

        plan = orchestrator.build_plan("market_deep_dive", MARKET_PAYLOAD)
        outcome = asyncio.run(
            orchestrator.run_plan(plan, executor=_execute, synthesizer=_echo_synthesizer)
        )

        assert outcome.partial is True
        # The synthesis is still produced with the steps that did run: a
        # failed step degrades the answer, it does not abort the plan. The
        # trade-plan step is skipped because its only dependency failed.
        assert outcome.synthesis == "SYNTHESIS"
        assert outcome.success is True
        failed = [result for result in outcome.steps if not result.ok]
        assert [result.step for result in failed] == ["assess_risk", "trade_plan"]
        assert "risk engine exploded" in failed[0].error
        assert "skipped" in failed[1].error
        assert outcome.metadata["steps_failed"] == "2"
        assert outcome.metadata["partial"] == "true"
        assert orchestrator.get_orchestration_metrics()["plans_partial"] >= 1

    def test_parallel_failure_does_not_skip_the_plan_tail(self, orchestrator):
        """One failed deep dive must not cost the sweep its risk step."""

        async def _execute(step, backend, request):
            if step.name == "deep_dive_2":
                raise RuntimeError("research timeout")
            return {"analysis": f"output of {step.name}"}

        plan = orchestrator.build_plan("opportunity_sweep", SWEEP_PAYLOAD)
        outcome = asyncio.run(
            orchestrator.run_plan(plan, executor=_execute, synthesizer=_echo_synthesizer)
        )

        assert [result.ok for result in outcome.steps] == [True, True, False, True, True]
        assert outcome.partial is True
        assert outcome.synthesis == "SYNTHESIS"

    def test_every_dependency_failed_skips_the_step(self, orchestrator):
        async def _execute(step, backend, request):
            if step.name != "analyze_market":
                raise RuntimeError("always down")
            return {"analysis": "ok"}

        plan = orchestrator.build_plan("market_deep_dive", MARKET_PAYLOAD)
        outcome = asyncio.run(
            orchestrator.run_plan(plan, executor=_execute, synthesizer=_echo_synthesizer)
        )

        assert [result.ok for result in outcome.steps] == [True, False, False]
        assert "skipped" in outcome.steps[2].error

    def test_hanging_step_times_out_without_hanging_the_plan(self, orchestrator):
        async def _execute(step, backend, request):
            await asyncio.sleep(30)  # a stuck backend
            return {"analysis": "never"}

        plan = orchestrator.build_plan("market_deep_dive", MARKET_PAYLOAD)
        started = time.monotonic()
        outcome = asyncio.run(
            orchestrator.run_plan(
                plan,
                executor=_execute,
                synthesizer=_echo_synthesizer,
                step_timeout=0.05,
            )
        )
        elapsed = time.monotonic() - started

        assert elapsed < 5  # the RPC never waits for the stuck step
        assert outcome.partial is True
        # The stuck step times out; the tail is skipped rather than hung.
        assert "timed out" in outcome.steps[0].error
        assert "skipped" in outcome.steps[1].error
        assert "skipped" in outcome.steps[2].error
        assert outcome.synthesis == "SYNTHESIS"
        assert orchestrator.get_orchestration_metrics()["steps_timed_out"] >= 1

    def test_step_error_is_typed(self, orchestrator):
        async def _execute(step, backend, request):
            raise orchestrator.StepError(step.name, "typed failure", "llm-chain")

        plan = orchestrator.build_plan("market_deep_dive", MARKET_PAYLOAD)
        outcome = asyncio.run(
            orchestrator.run_plan(plan, executor=_execute, synthesizer=_echo_synthesizer)
        )

        assert outcome.steps[0].error == "typed failure"
        assert outcome.steps[0].ok is False

    def test_max_steps_caps_the_plan(self, orchestrator):
        plan = orchestrator.build_plan("opportunity_sweep", SWEEP_PAYLOAD)

        async def _collect():
            return [
                result
                async for result in orchestrator.execute_plan(
                    plan, executor=_recording_executor([]), max_steps=2
                )
            ]

        results = asyncio.run(_collect())
        assert [result.step for result in results] == ["scan_markets", "deep_dive_1"]


class TestCliAgentDispatch:
    """Steps routed to cli-agent are dispatched over gRPC with mapped fields."""

    def test_step_is_dispatched_with_the_proto_request(self, orchestrator, monkeypatch):
        import analysis_pb2

        captured: dict[str, Any] = {}

        class _Stub:
            async def AnalyzeMarket(self, request, timeout=None):
                captured["request"] = request
                captured["timeout"] = timeout
                return analysis_pb2.AnalysisResponse(
                    success=True,
                    analysis="cli-agent analysis",
                    research_context="from the cli agent",
                    timestamp="2026-01-01T00:00:00",
                    metadata={"model": "github-model"},
                )

        monkeypatch.setattr(orchestrator, "_get_cli_agent_stub", lambda: _Stub())
        step = orchestrator.Step(name="analyze_market", rpc="AnalyzeMarket")

        output = asyncio.run(
            orchestrator._run_on_cli_agent(
                step,
                {"market_title": "BTC 150k?", "include_research": True, "yes_price": 0.6},
                user_id="7",
            )
        )

        request = captured["request"]
        assert isinstance(request, analysis_pb2.MarketAnalysisRequest)
        assert request.market_title == "BTC 150k?"
        assert request.include_research is True
        assert request.yes_price == pytest.approx(0.6)
        # user_id is injected into the proto request when the message has it.
        assert request.user_id == "7"
        assert captured["timeout"] == orchestrator._step_timeout()
        # The response is normalised into the chain's result shape.
        assert output["analysis"] == "cli-agent analysis"
        assert output["research_context"] == "from the cli agent"
        assert output["metadata"] == {"model": "github-model"}

    def test_failed_cli_agent_response_becomes_a_step_error(self, orchestrator, monkeypatch):
        import analysis_pb2

        class _Stub:
            async def AssessRisk(self, request, timeout=None):
                return analysis_pb2.AnalysisResponse(success=False, error="model offline")

        monkeypatch.setattr(orchestrator, "_get_cli_agent_stub", lambda: _Stub())
        step = orchestrator.Step(name="assess_risk", rpc="AssessRisk")

        with pytest.raises(orchestrator.StepError) as excinfo:
            asyncio.run(orchestrator._run_on_cli_agent(step, {"market_title": "BTC 150k?"}))

        assert "model offline" in str(excinfo.value)
        assert excinfo.value.backend == "cli-agent"

    def test_trade_plan_order_book_is_serialised_for_the_proto(self, orchestrator, monkeypatch):
        import analysis_pb2

        captured: dict[str, Any] = {}

        class _Stub:
            async def GenerateTradePlan(self, request, timeout=None):
                captured["request"] = request
                return analysis_pb2.AnalysisResponse(success=True, analysis="plan")

        monkeypatch.setattr(orchestrator, "_get_cli_agent_stub", lambda: _Stub())
        step = orchestrator.Step(name="trade_plan", rpc="GenerateTradePlan")

        asyncio.run(
            orchestrator._run_on_cli_agent(
                step, {"action": "buy_yes", "order_book": {"bids": [["0.61", 100]]}}
            ),
        )

        assert json.loads(captured["request"].order_book_json) == {"bids": [["0.61", 100]]}


# ── Synthesis ────────────────────────────────────────────────


class TestSynthesis:
    def test_fallback_concatenates_step_outputs(self, orchestrator):
        ok_result = orchestrator.StepResult(
            step="analyze_market", ok=True, backend="llm-chain", duration_ms=120
        )
        ok_result.output = {"analysis": "BTC looks fairly priced."}
        failed_result = orchestrator.StepResult(
            step="assess_risk",
            ok=False,
            backend="llm-chain",
            error="StepError: risk engine exploded",
            duration_ms=5,
        )

        text = orchestrator._fallback_synthesis([ok_result, failed_result])

        assert "analyze_market" in text
        assert "BTC looks fairly priced." in text
        assert "assess_risk" in text
        assert "FAILED" in text
        # An empty plan still yields a deterministic answer.
        assert orchestrator._fallback_synthesis([]) == "No steps ran; no synthesis available."

    def test_failed_synthesis_falls_back_deterministically(self, orchestrator):
        plan = orchestrator.build_plan("market_deep_dive", MARKET_PAYLOAD)

        async def _execute(step, backend, request):
            return {"analysis": f"output of {step.name}"}

        async def _broken_synthesizer(results):
            raise RuntimeError("LLM unavailable")

        outcome = asyncio.run(
            orchestrator.run_plan(plan, executor=_execute, synthesizer=_broken_synthesizer)
        )

        assert outcome.synthesis.startswith("Synthesis unavailable")
        assert "analyze_market" in outcome.synthesis
        assert outcome.metadata["synthesis_fallback"] == "true"
        assert outcome.partial is False
        assert orchestrator.get_orchestration_metrics()["synthesis_fallbacks"] >= 1

    def test_stream_plan_yields_step_then_recommendation(self, orchestrator):
        plan = orchestrator.build_plan("market_deep_dive", MARKET_PAYLOAD)

        async def _collect():
            return [
                event
                async for event in orchestrator.stream_plan(
                    plan,
                    executor=_recording_executor([]),
                    synthesizer=_echo_synthesizer,
                )
            ]

        events = asyncio.run(_collect())

        assert [event["type"] for event in events] == [
            "step",
            "step",
            "step",
            "recommendation",
        ]
        assert events[0]["step"] == "analyze_market"
        assert events[0]["ok"] is True
        assert events[-1]["synthesis"] == "SYNTHESIS"
        assert events[-1]["partial"] is False


# ── Generated stubs ──────────────────────────────────────────


class TestGeneratedStubs:
    def test_stubs_expose_the_orchestration_rpcs(self):
        from app.grpc_clients import analysis_pb2_grpc

        assert hasattr(analysis_pb2, "OrchestrateRequest")
        assert hasattr(analysis_pb2, "OrchestrateResponse")
        assert hasattr(analysis_pb2, "OrchestrateStepResult")
        assert hasattr(analysis_pb2_grpc.AnalysisServiceServicer, "OrchestrateAnalysis")
        assert hasattr(analysis_pb2_grpc.AnalysisServiceServicer, "OrchestrateAnalysisStream")

        service = analysis_pb2.DESCRIPTOR.services_by_name["AnalysisService"]
        methods = {method.name: method for method in service.methods}
        assert "OrchestrateAnalysis" in methods
        assert "OrchestrateAnalysisStream" in methods
        assert methods["OrchestrateAnalysisStream"].server_streaming is True
        assert methods["OrchestrateAnalysisStream"].client_streaming is False

    def test_orchestrate_messages_carry_the_planned_fields(self):
        request = analysis_pb2.OrchestrateRequest(
            intent="market_deep_dive",
            payload_json="{}",
            steps=["analyze_market"],
            user_id="7",
            stream=False,
        )
        response = analysis_pb2.OrchestrateResponse(
            success=True,
            synthesis="merged",
            steps=[
                analysis_pb2.OrchestrateStepResult(
                    step="analyze_market",
                    ok=True,
                    output_json="{}",
                    backend="llm-chain",
                    duration_ms=12,
                )
            ],
            partial=False,
            metadata={"intent": "market_deep_dive"},
        )

        assert request.intent == "market_deep_dive"
        assert request.stream is False
        assert response.steps[0].duration_ms == 12
        assert dict(response.metadata) == {"intent": "market_deep_dive"}


# ── Routes ───────────────────────────────────────────────────


def _db_double():
    db = MagicMock()
    # No user settings row and no active experiment.
    db.query.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    return db


def _client_double(*, orchestrate_result=None, stream_events=None) -> MagicMock:
    """A replacement AnalysisClient that records the kwargs it was called with."""
    client = MagicMock()
    client.backend = AIBackend.LLM_CHAIN
    client.close = AsyncMock()
    calls: dict[str, Any] = {}

    async def _orchestrate(**kwargs):
        calls["orchestrate"] = kwargs
        return orchestrate_result

    client.orchestrate = AsyncMock(side_effect=_orchestrate)

    async def _stream(**kwargs):
        calls["stream"] = kwargs
        for event in stream_events or []:
            yield event

    client.orchestrate_stream = _stream
    client.calls = calls
    return client


def _patch_client(monkeypatch, client: MagicMock) -> None:
    monkeypatch.setattr(
        "app.api.routes.analysis.orchestration.AnalysisClient", lambda **kwargs: client
    )


@pytest.fixture
def api():
    """An app mounting only the orchestration router (main.py is owned elsewhere)."""
    from app.api.routes.analysis.orchestration import router as orchestration_router

    app = FastAPI()
    app.include_router(orchestration_router)
    return TestClient(app)


@pytest.fixture
def authed(api):
    user = {"user_id": 1, "wallet_address": WALLET, "is_admin": False}
    api.app.dependency_overrides[get_current_user_from_token] = lambda: user
    api.app.dependency_overrides[get_db] = _db_double
    yield api
    api.app.dependency_overrides.clear()


ORCHESTRATE_RESULT = {
    "success": True,
    "synthesis": '{"summary": "merged verdict", "recommendation": "hold"}',
    "steps": [
        {
            "step": "analyze_market",
            "ok": True,
            "output": {"analysis": "full analysis"},
            "error": "",
            "backend": "llm-chain",
            "duration_ms": 12,
        }
    ],
    "partial": False,
    "metadata": {"intent": "market_deep_dive", "llm_tier": "strong"},
}

STREAM_EVENTS = [
    {
        "chunk_type": "step",
        "type": "step",
        "step": "analyze_market",
        "ok": True,
        "backend": "llm-chain",
        "duration_ms": 12,
        "output": {"analysis": "full analysis"},
        "error": "",
        "is_final": False,
    },
    {
        "chunk_type": "recommendation",
        "type": "recommendation",
        "success": True,
        "synthesis": "merged verdict",
        "partial": False,
        "metadata": {"intent": "market_deep_dive"},
        "is_final": True,
    },
]


class TestOrchestrationRoutes:
    def test_orchestrate_requires_authentication(self, api):
        response = api.post("/api/analysis/orchestrate", json={"intent": "market_deep_dive"})
        assert response.status_code == 401

    def test_orchestrate_stream_requires_authentication(self, api):
        response = api.post("/api/analysis/orchestrate/stream", json={"intent": "market_deep_dive"})
        assert response.status_code == 401

    def test_orchestrate_success(self, authed, monkeypatch):
        client = _client_double(orchestrate_result=ORCHESTRATE_RESULT)
        _patch_client(monkeypatch, client)

        response = authed.post(
            "/api/analysis/orchestrate",
            json={"intent": "market_deep_dive", "payload": MARKET_PAYLOAD},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["synthesis"] == ORCHESTRATE_RESULT["synthesis"]
        assert data["steps"][0]["step"] == "analyze_market"
        assert data["partial"] is False
        # Orchestration is pinned to llm-chain: cli-agent cannot serve the RPC.
        assert data["backend"] == "llm_chain"

        sent = client.calls["orchestrate"]
        assert sent["intent"] == "market_deep_dive"
        assert sent["payload"]["market_title"] == MARKET_PAYLOAD["market_title"]
        assert sent["user_id"] == "1"
        # Attribution rides along inside llm_config, where the service echoes
        # it back on the response metadata (plan 04).
        assert sent["llm_config"]["prompt_version"]
        assert sent["llm_config"]["llm_tier"] == "strong"

    def test_orchestrate_unknown_intent_returns_400(self, authed, monkeypatch):
        rejected = {
            "success": False,
            "synthesis": "",
            "steps": [],
            "partial": False,
            "metadata": {"error": "unknown orchestration intent 'nope'", "intent": "nope"},
        }
        _patch_client(monkeypatch, _client_double(orchestrate_result=rejected))

        response = authed.post("/api/analysis/orchestrate", json={"intent": "nope", "payload": {}})

        assert response.status_code == 400
        assert "unknown orchestration intent" in response.json()["detail"]

    def test_orchestrate_backend_failure_returns_502(self, authed, monkeypatch):
        client = MagicMock()
        client.close = AsyncMock()

        async def _boom(**kwargs):
            raise RuntimeError("llm-chain unreachable")

        client.orchestrate = _boom
        _patch_client(monkeypatch, client)

        response = authed.post(
            "/api/analysis/orchestrate",
            json={"intent": "market_deep_dive", "payload": MARKET_PAYLOAD},
        )

        assert response.status_code == 502
        # Plan B1: the transport failure is logged, not echoed to the caller.
        assert response.json()["detail"] == "Orchestration backend unavailable"
        assert "llm-chain unreachable" not in response.text

    def test_orchestrate_stream_emits_step_recommendation_done(self, authed, monkeypatch):
        _patch_client(monkeypatch, _client_double(stream_events=STREAM_EVENTS))

        with authed.stream(
            "POST",
            "/api/analysis/orchestrate/stream",
            json={"intent": "market_deep_dive", "payload": MARKET_PAYLOAD},
        ) as response:
            assert response.status_code == 200
            body = "\n".join(response.iter_lines())

        events = _sse_events(body)
        assert events[0]["status"] == "started"
        assert events[1]["step"]["step"] == "analyze_market"
        assert events[1]["step"]["backend"] == "llm-chain"
        assert events[2]["recommendation"] == "merged verdict"
        assert events[2]["partial"] is False
        assert events[-1] == {"done": True, "partial": False}

    def test_orchestrate_stream_surfaces_transport_errors(self, authed, monkeypatch):
        _patch_client(
            monkeypatch,
            _client_double(
                stream_events=[{"chunk_type": "error", "error": "grpc down", "is_final": True}]
            ),
        )

        with authed.stream(
            "POST",
            "/api/analysis/orchestrate/stream",
            json={"intent": "market_deep_dive", "payload": MARKET_PAYLOAD},
        ) as response:
            body = "\n".join(response.iter_lines())

        assert _sse_events(body)[-1]["error"] == "grpc down"

    def test_stream_llm_config_carries_attribution(self, authed, monkeypatch):
        client = _client_double(stream_events=STREAM_EVENTS)
        _patch_client(monkeypatch, client)

        with authed.stream(
            "POST",
            "/api/analysis/orchestrate/stream",
            json={"intent": "market_deep_dive", "payload": MARKET_PAYLOAD},
        ) as response:
            list(response.iter_lines())

        sent = client.calls["stream"]
        assert sent["llm_config"]["prompt_version"]
        assert sent["llm_config"]["llm_tier"] == "strong"


def _sse_events(body: str) -> list[dict]:
    """Decode the ``data: {json}`` frames of an SSE body."""
    events = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        events.append(json.loads(line[len("data: ") :]))
    return events


class TestOrchestrationClient:
    """The gRPC client wrapper, without a live service."""

    def test_orchestrate_decodes_the_response(self, monkeypatch):
        from app.grpc_clients import analysis_pb2
        from app.grpc_clients.analysis_client import AnalysisClient

        response = analysis_pb2.OrchestrateResponse(
            success=True,
            synthesis="merged",
            steps=[
                analysis_pb2.OrchestrateStepResult(
                    step="analyze_market",
                    ok=True,
                    output_json=json.dumps({"analysis": "text"}),
                    backend="llm-chain",
                    duration_ms=9,
                ),
                analysis_pb2.OrchestrateStepResult(
                    step="assess_risk", ok=False, error="StepError: boom", backend=""
                ),
            ],
            partial=True,
            metadata={"intent": "market_deep_dive"},
        )
        stub = MagicMock()
        stub.OrchestrateAnalysis = _returns(response)

        client = AnalysisClient(backend=AIBackend.LLM_CHAIN)
        monkeypatch.setattr(client, "_get_stub", _returns_stub(stub))

        result = asyncio.run(
            client.orchestrate(
                intent="market_deep_dive",
                payload=MARKET_PAYLOAD,
                user_id="1",
                llm_config={
                    "provider": 1,  # proto LLM_PROVIDER_OPENAI
                    "model": "gpt-4o",
                    "prompt_version": "abc123",
                    "llm_tier": "strong",
                },
            )
        )

        assert result["partial"] is True
        assert result["steps"][0]["output"] == {"analysis": "text"}
        assert result["steps"][1]["error"] == "StepError: boom"
        # Attribution keys ride along in llm_config and come back on metadata.
        assert result["metadata"]["prompt_version"] == "abc123"
        assert result["metadata"]["llm_tier"] == "strong"

    def test_orchestrate_stream_yields_decoded_events(self, monkeypatch):
        from app.grpc_clients import analysis_pb2
        from app.grpc_clients.analysis_client import AnalysisClient

        chunks = [
            analysis_pb2.AnalysisChunk(
                chunk=json.dumps({"type": "step", "step": "analyze_market", "ok": True}),
                chunk_type="step",
                is_final=False,
            ),
            analysis_pb2.AnalysisChunk(
                chunk=json.dumps({"type": "recommendation", "synthesis": "merged"}),
                chunk_type="recommendation",
                is_final=True,
            ),
        ]
        stub = MagicMock()

        def _stream(request, timeout=None):
            async def _gen():
                for chunk in chunks:
                    yield chunk

            return _gen()

        stub.OrchestrateAnalysisStream = _stream

        client = AnalysisClient(backend=AIBackend.LLM_CHAIN)
        monkeypatch.setattr(client, "_get_stub", _returns_stub(stub))

        async def _collect():
            return [
                event
                async for event in client.orchestrate_stream(
                    intent="market_deep_dive", payload=MARKET_PAYLOAD
                )
            ]

        events = asyncio.run(_collect())

        assert [event["type"] for event in events] == ["step", "recommendation"]
        assert events[-1]["synthesis"] == "merged"
        assert events[-1]["is_final"] is True

    def test_orchestrate_stream_masks_transport_failure(self, monkeypatch):
        """A failing stream yields one generic error event (plan B1.3)."""
        from app.grpc_clients.analysis_client import AnalysisClient

        stub = MagicMock()

        def _stream(request, timeout=None):
            async def _gen():
                if False:  # pragma: no cover - makes this a generator
                    yield None
                raise RuntimeError("failed to connect to 127.0.0.1:50051")

            return _gen()

        stub.OrchestrateAnalysisStream = _stream

        client = AnalysisClient(backend=AIBackend.LLM_CHAIN)
        monkeypatch.setattr(client, "_get_stub", _returns_stub(stub))

        async def _collect():
            return [
                event
                async for event in client.orchestrate_stream(
                    intent="market_deep_dive", payload=MARKET_PAYLOAD
                )
            ]

        events = asyncio.run(_collect())

        assert len(events) == 1
        assert events[0]["chunk_type"] == "error"
        assert events[0]["error"] == "Stream processing failed"
        # No host/port, no exception text, no proto internals reach the SSE body.
        assert "127.0.0.1" not in json.dumps(events)
        assert "50051" not in json.dumps(events)
        assert "failed to connect" not in json.dumps(events)


def _returns(value):
    async def _call(*args, **kwargs):
        return value

    return _call


def _returns_stub(stub):
    async def _get_stub():
        return stub

    return _get_stub
