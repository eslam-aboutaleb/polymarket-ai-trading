"""Behavioural tests for :mod:`app.services.users.price_alert_service`.

Covers the plan-03 verdict pipeline end to end:

* ``evaluate_trigger`` — prompt-field forwarding, the kill switch, backend
  selection from ``UserSettings.ai_backend``, and the client-failure path
  (metric counted, client closed, alert still fires un-enriched).
* ``_format_position`` / ``_format_price_change`` — the prompt placeholders,
  including the ``""`` fallback when the token was never priced before.
* ``format_trigger_message`` / ``build_trigger_payload`` — the payload shape
  chat channels render, with and without a verdict.
* ``enrich_trigger_alert`` — the fire-and-forget entry point: the kill switch
  short-circuits to a plain dispatch, a verdict is awaited when enabled, and a
  dispatch failure never escapes.

No gRPC service, LLM, browser or database engine is used: the analysis client
and the alert dispatcher are replaced with doubles.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.users.price_alert_service as price_alert_service
from app.config import AIBackend
from app.models.users.user_settings import AIBackendType


def _order(**overrides):
    """A trigger row shaped like ``StopLossOrder`` / ``TakeProfitOrder``."""
    base = {
        "id": 7,
        "user_id": 3,
        "token_id": "token-1",
        "market_id": "condition-1",
        "market_title": "Will BTC hit $150k?",
        "outcome": "Yes",
        "size": 120.0,
        "stop_price": 0.35,
        "take_profit_price": 0.75,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _settings_row(**overrides):
    base = {"user_id": 3, "ai_backend": AIBackendType.LLM_CHAIN.value}
    base.update(overrides)
    return SimpleNamespace(**base)


def _session(settings_row=None):
    """Session double returning ``settings_row`` for the UserSettings lookup."""
    db = MagicMock(name="db")
    query = MagicMock(name="query")
    db.query.return_value = query
    query.filter.return_value.first.return_value = settings_row
    return db


def _verdict(**overrides):
    base = {
        "condition_id": "condition-1",
        "verdict": "reduce",
        "thesis_changed": True,
        "confidence": 0.62,
        "driver": "news",
        "reasoning": "Exchange hack dominates the tape.",
    }
    base.update(overrides)
    return base


def _client(**overrides):
    """AnalysisClient double with the price-alert method stubbed."""
    client = MagicMock(name="client")
    client.analyze_price_alert = AsyncMock(return_value=_verdict())
    client.close = AsyncMock()
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


class _MetricsMixin:
    """Pins the process-global counters to a known baseline per test.

    ``_metrics`` is module state, so without this reset an earlier
    module (``test_stop_loss_monitor``'s price-alert classes run the same
    code) leaves non-zero counts behind and exact-value assertions break
    under collection order.
    """

    def setUp(self):
        original = dict(price_alert_service._metrics)
        price_alert_service.reset_price_alert_metrics()

        def _restore():
            price_alert_service._metrics.clear()
            price_alert_service._metrics.update(original)

        self.addCleanup(_restore)


class FormatHelperTests(_MetricsMixin, unittest.TestCase):
    """Prompt-placeholder rendering."""

    def test_price_change_is_rounded_signed_percentage(self):
        self.assertEqual(
            price_alert_service._format_price_change(0.50, 0.40),
            "+25.0",
        )
        self.assertEqual(
            price_alert_service._format_price_change(0.30, 0.40),
            "-25.0",
        )

    def test_price_change_is_empty_without_a_reference_price(self):
        # The prompt placeholder must always be supplied — never a KeyError.
        self.assertEqual(price_alert_service._format_price_change(0.5, None), "")
        self.assertEqual(price_alert_service._format_price_change(0.5, 0), "")
        self.assertEqual(price_alert_service._format_price_change(0.5, -1.0), "")

    def test_position_includes_avg_entry_when_known(self):
        self.assertEqual(
            price_alert_service._format_position("Yes", 120.0, 0.42),
            "120 shares of Yes (avg entry $0.4200)",
        )

    def test_position_without_avg_entry_or_outcome(self):
        self.assertEqual(
            price_alert_service._format_position("", None, None),
            "an unrecorded position of the position",
        )
        self.assertEqual(
            price_alert_service._format_position("No", 7.5, 0.0),
            "7.5 shares of No",
        )


class VerdictsEnabledTests(unittest.TestCase):
    """Kill-switch resolution."""

    def test_enabled_flag_is_read_from_settings(self):
        with patch.object(
            price_alert_service,
            "get_settings",
            return_value=SimpleNamespace(price_alert_llm_enabled=True),
        ):
            self.assertTrue(price_alert_service.verdicts_enabled())

    def test_disabled_flag_is_honoured(self):
        with patch.object(
            price_alert_service,
            "get_settings",
            return_value=SimpleNamespace(price_alert_llm_enabled=False),
        ):
            self.assertFalse(price_alert_service.verdicts_enabled())

    def test_unreadable_settings_fail_closed(self):
        with patch.object(price_alert_service, "get_settings", side_effect=RuntimeError("x")):
            self.assertFalse(price_alert_service.verdicts_enabled())


class EvaluateTriggerTests(_MetricsMixin, unittest.IsolatedAsyncioTestCase):
    """``evaluate_trigger`` prompt forwarding and degradation."""

    def setUp(self):
        super().setUp()
        self.client = _client()
        patcher = patch.object(price_alert_service, "AnalysisClient", return_value=self.client)
        self.analysis_cls = patcher.start()
        self.addCleanup(patcher.stop)
        enabled = patch.object(price_alert_service, "verdicts_enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    async def _evaluate(self, settings_row=None, **overrides):
        kwargs = {
            "user_id": 3,
            "condition_id": "condition-1",
            "market_title": "Will BTC hit $150k?",
            "outcome": "Yes",
            "position_size": 120.0,
            "avg_price": 0.42,
            "trigger_condition": "price <= stop 0.3500",
            "current_price": 0.30,
            "prev_price": 0.40,
            "db": _session(settings_row),
        }
        kwargs.update(overrides)
        return await price_alert_service.evaluate_trigger(**kwargs)

    async def test_forwards_every_prompt_field_to_the_client(self):
        result = await self._evaluate(settings_row=_settings_row())

        self.client.analyze_price_alert.assert_awaited_once_with(
            market_title="Will BTC hit $150k?",
            position="120 shares of Yes (avg entry $0.4200)",
            trigger_condition="price <= stop 0.3500",
            current_price="0.3000",
            price_change="-25.0",
            condition_id="condition-1",
        )
        self.client.close.assert_awaited_once_with()
        # The client's own verdict dict is returned untouched.
        self.assertEqual(result["verdict"], "reduce")
        self.assertTrue(result["thesis_changed"])
        self.assertEqual(result["confidence"], 0.62)
        self.assertEqual(price_alert_service._metrics["verdicts_total"], 1)

    async def test_missing_previous_price_yields_empty_price_change(self):
        await self._evaluate(settings_row=_settings_row(), prev_price=None)

        kwargs = self.client.analyze_price_alert.await_args.kwargs
        self.assertEqual(kwargs["price_change"], "")

    async def test_kill_switch_skips_the_llm_entirely(self):
        with patch.object(price_alert_service, "verdicts_enabled", return_value=False):
            result = await self._evaluate()

        self.assertIsNone(result)
        self.analysis_cls.assert_not_called()
        self.client.analyze_price_alert.assert_not_awaited()
        self.assertEqual(price_alert_service._metrics["verdicts_total"], 0)

    async def test_cli_agent_backend_is_selected_from_settings(self):
        await self._evaluate(settings_row=_settings_row(ai_backend=AIBackendType.CLI_AGENT.value))

        self.assertEqual(
            self.analysis_cls.call_args.kwargs,
            {"backend": AIBackend.CLI_AGENT},
        )

    async def test_llm_chain_is_the_default_backend_without_settings(self):
        await self._evaluate(settings_row=None)

        self.assertEqual(
            self.analysis_cls.call_args.kwargs,
            {"backend": AIBackend.LLM_CHAIN},
        )

    async def test_client_failure_returns_none_and_counts_the_error(self):
        self.client.analyze_price_alert.side_effect = RuntimeError("grpc down")

        result = await self._evaluate(settings_row=_settings_row())

        self.assertIsNone(result)
        self.client.close.assert_awaited_once_with()
        self.assertEqual(price_alert_service._metrics["verdicts_failed"], 1)
        self.assertEqual(price_alert_service._metrics["mcp_errors_total"], 1)

    async def test_empty_verdict_from_fail_open_client_is_returned(self):
        """The client's own fail-open default must not be re-labelled."""
        self.client.analyze_price_alert.return_value = {
            "verdict": "hold",
            "thesis_changed": False,
            "confidence": 0.0,
            "driver": "mixed",
            "reasoning": "",
        }

        result = await self._evaluate(settings_row=_settings_row())

        self.assertEqual(result["confidence"], 0.0)
        self.assertEqual(result["verdict"], "hold")

    async def test_unusable_verdict_counts_a_failure_instead_of_advice(self):
        """A silent fail-open default must not masquerade as a "hold".

        ``analyze_price_alert`` marks a response that carried no usable
        JSON with the ``unknown`` verdict; the verdict path then drops it,
        so the alert fires plain and the failure is counted rather than
        persisted as if the model had recommended holding.
        """
        self.client.analyze_price_alert.return_value = _verdict(verdict="unknown", confidence=0.0)

        result = await self._evaluate(settings_row=_settings_row())

        self.assertIsNone(result)
        self.assertEqual(price_alert_service._metrics["verdicts_failed"], 1)


class MessageFormattingTests(unittest.TestCase):
    """The payload ``message`` chat channels render."""

    def test_plain_message_without_a_verdict(self):
        message = price_alert_service.format_trigger_message(
            _order(),
            event_type=price_alert_service.STOP_LOSS_EVENT,
            live_price=0.30,
            trigger_condition="price <= stop 0.3500",
            verdict=None,
        )

        self.assertIn("Stop-loss triggered: Will BTC hit $150k? (Yes)", message)
        self.assertIn("sold 120 shares @ $0.3000", message)
        self.assertIn("[price <= stop 0.3500]", message)
        self.assertNotIn("AI verdict", message)

    def test_take_profit_message_uses_the_target_rule(self):
        message = price_alert_service.format_trigger_message(
            _order(),
            event_type=price_alert_service.TAKE_PROFIT_EVENT,
            live_price=0.80,
            trigger_condition="price >= target 0.7500",
            verdict=None,
        )

        self.assertIn("Take-profit triggered:", message)
        self.assertIn("[price >= target 0.7500]", message)

    def test_verdict_block_appends_the_verdict_and_reasoning(self):
        message = price_alert_service.format_trigger_message(
            _order(),
            event_type=price_alert_service.STOP_LOSS_EVENT,
            live_price=0.30,
            trigger_condition="price <= stop 0.3500",
            verdict=_verdict(),
        )

        self.assertIn("AI verdict: REDUCE (confidence 62%, driver news)", message)
        self.assertIn("Thesis changed: yes", message)
        self.assertIn("Reasoning: Exchange hack dominates the tape.", message)

    def test_empty_fail_open_verdict_renders_no_advice(self):
        """confidence 0.0 with no reasoning is not a recommendation."""
        message = price_alert_service.format_trigger_message(
            _order(),
            event_type=price_alert_service.STOP_LOSS_EVENT,
            live_price=0.30,
            trigger_condition="",
            verdict=_verdict(confidence=0.0, reasoning=""),
        )

        self.assertNotIn("AI verdict", message)

    def test_holding_verdict_with_reasoning_still_renders(self):
        message = price_alert_service.format_trigger_message(
            _order(),
            event_type=price_alert_service.STOP_LOSS_EVENT,
            live_price=0.30,
            trigger_condition="",
            verdict=_verdict(verdict="hold", thesis_changed=False, confidence=0.1, driver="mixed"),
        )

        self.assertIn("AI verdict: HOLD (confidence 10%, driver mixed)", message)
        self.assertIn("Thesis changed: no", message)

    def test_missing_order_fields_do_not_raise(self):
        message = price_alert_service.format_trigger_message(
            SimpleNamespace(id=1, size=None),
            event_type=price_alert_service.STOP_LOSS_EVENT,
            live_price=0.5,
            trigger_condition="",
            verdict=None,
        )

        self.assertIn("sold an unrecorded position @ $0.5000", message)
        self.assertIn("unknown market", message)

    def test_paper_mode_says_simulated_sell(self):
        """A paper fill never happened, so it is never reported as a sale."""
        message = price_alert_service.format_trigger_message(
            _order(),
            event_type=price_alert_service.STOP_LOSS_EVENT,
            live_price=0.30,
            trigger_condition="price <= stop 0.3500",
            verdict=None,
            simulation_mode=True,
        )

        self.assertIn("simulated sell of 120 shares @ $0.3000", message)
        self.assertNotIn("sold 120", message)

    def test_live_mode_keeps_the_sold_wording(self):
        message = price_alert_service.format_trigger_message(
            _order(),
            event_type=price_alert_service.STOP_LOSS_EVENT,
            live_price=0.30,
            trigger_condition="price <= stop 0.3500",
            verdict=None,
            simulation_mode=False,
        )

        self.assertIn("sold 120 shares @ $0.3000", message)
        self.assertNotIn("simulated", message)


class PayloadTests(unittest.TestCase):
    """``build_trigger_payload`` shape and feed-event fields."""

    def test_plain_payload_carries_the_alert_fields(self):
        payload = price_alert_service.build_trigger_payload(
            _order(),
            event_type=price_alert_service.STOP_LOSS_EVENT,
            live_price=0.30,
            trigger_condition="price <= stop 0.3500",
            verdict=None,
        )

        self.assertIn("Stop-loss triggered", payload["message"])
        self.assertEqual(payload["market_id"], "condition-1")
        self.assertEqual(payload["token_id"], "token-1")
        self.assertEqual(payload["side"], "sell")
        self.assertEqual(payload["size"], 120.0)
        self.assertEqual(payload["price"], 0.30)
        self.assertEqual(payload["trigger"], "price <= stop 0.3500")
        self.assertNotIn("verdict", payload)

    def test_enriched_payload_adds_the_verdict_fields(self):
        payload = price_alert_service.build_trigger_payload(
            _order(),
            event_type=price_alert_service.TAKE_PROFIT_EVENT,
            live_price=0.80,
            trigger_condition="price >= target 0.7500",
            verdict=_verdict(),
        )

        self.assertEqual(payload["verdict"], "reduce")
        self.assertEqual(payload["confidence"], 0.62)

    def test_paper_mode_payload_marks_the_simulation(self):
        payload = price_alert_service.build_trigger_payload(
            _order(),
            event_type=price_alert_service.STOP_LOSS_EVENT,
            live_price=0.30,
            trigger_condition="price <= stop 0.3500",
            verdict=None,
            simulation_mode=True,
        )

        self.assertIn("simulated sell of 120 shares", payload["message"])
        self.assertTrue(payload["simulation_mode"])


class EnrichTriggerAlertTests(_MetricsMixin, unittest.IsolatedAsyncioTestCase):
    """The fire-and-forget entry point the monitor spawns."""

    def setUp(self):
        super().setUp()
        self.db = MagicMock(name="db")
        self.session_factory = MagicMock(name="SessionLocal", return_value=self.db)
        self._patch(price_alert_service, "SessionLocal", new=self.session_factory)
        self._patch(price_alert_service, "average_entry_price", return_value=0.42)

    def _patch(self, *args, **kwargs):
        """Start one patcher with its own cleanup, never ``stopall``.

        ``patch.stopall`` also undoes patches started by other modules in
        the same pytest process, which is how cross-module ordering
        failures start.
        """
        patcher = patch.object(*args, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def _patch_enabled(self, enabled=True):
        self._patch(price_alert_service, "verdicts_enabled", return_value=enabled)

    def _patch_client(self, verdict=None):
        client = _client()
        if verdict is not None:
            client.analyze_price_alert = AsyncMock(return_value=verdict)
        self._patch(price_alert_service, "AnalysisClient", return_value=client)
        return client

    async def _run(self, **overrides):
        kwargs = {
            "event_type": price_alert_service.STOP_LOSS_EVENT,
            "live_price": 0.30,
            "trigger_condition": "price <= stop 0.3500",
            "prev_price": 0.40,
        }
        kwargs.update(overrides)
        return await price_alert_service.enrich_trigger_alert(_order(), **kwargs)

    def _patch_dispatch(self, dispatch_mock):
        self._patch(price_alert_service, "alert_dispatch", new=dispatch_mock)

    async def test_enriched_alert_is_dispatched_with_the_verdict(self):
        self._patch_enabled()
        self._patch_client(_verdict())
        dispatch_mock = AsyncMock(return_value={"status": "dispatched"})
        self._patch_dispatch(dispatch_mock)

        result = await self._run()

        self.assertEqual(result, {"status": "dispatched"})
        event_type, user_id, payload = dispatch_mock.await_args.args
        self.assertEqual(event_type, "stop_loss_hit")
        self.assertEqual(user_id, 3)
        self.assertIn("AI verdict: REDUCE", payload["message"])
        self.assertEqual(payload["verdict"], "reduce")
        # The background task owns its session and always closes it.
        self.db.close.assert_called_once_with()
        self.assertEqual(price_alert_service._metrics["alerts_enriched"], 1)

    async def test_kill_switch_dispatches_immediately_without_a_session(self):
        self._patch_enabled(enabled=False)
        dispatch_mock = AsyncMock(return_value={"status": "dispatched"})
        self._patch_dispatch(dispatch_mock)

        result = await self._run()

        self.assertEqual(result, {"status": "dispatched"})
        payload = dispatch_mock.await_args.args[2]
        self.assertNotIn("AI verdict", payload["message"])
        self.assertNotIn("verdict", payload)
        self.session_factory.assert_not_called()
        self.assertEqual(price_alert_service._metrics["alerts_plain"], 1)

    async def test_verdict_failure_still_dispatches_the_plain_alert(self):
        self._patch_enabled()
        client = self._patch_client()
        client.analyze_price_alert.side_effect = RuntimeError("grpc down")
        dispatch_mock = AsyncMock(return_value={"status": "dispatched"})
        self._patch_dispatch(dispatch_mock)

        result = await self._run()

        self.assertEqual(result, {"status": "dispatched"})
        payload = dispatch_mock.await_args.args[2]
        self.assertNotIn("AI verdict", payload["message"])
        self.assertEqual(price_alert_service._metrics["alerts_plain"], 1)

    async def test_dispatch_failure_never_escapes(self):
        self._patch_enabled()
        self._patch_client()
        dispatch_mock = AsyncMock(side_effect=RuntimeError("db down"))
        self._patch_dispatch(dispatch_mock)

        result = await self._run()

        self.assertIsNone(result)
        self.assertEqual(price_alert_service._metrics["dispatch_failed"], 1)

    async def test_session_is_closed_when_the_verdict_raises(self):
        self._patch_enabled()
        client = self._patch_client()
        client.analyze_price_alert.side_effect = RuntimeError("boom")
        self._patch_dispatch(AsyncMock())

        await self._run()

        self.db.close.assert_called_once_with()

    async def test_average_entry_lookup_failure_is_survivable(self):
        self._patch_enabled()
        self._patch(
            price_alert_service,
            "average_entry_price",
            side_effect=RuntimeError("no trades"),
        )
        client = self._patch_client(_verdict())
        self._patch_dispatch(AsyncMock())

        await self._run()

        kwargs = client.analyze_price_alert.await_args.kwargs
        self.assertEqual(kwargs["position"], "120 shares of Yes")

    async def test_session_failure_does_not_escape(self):
        """A surprise outside ``evaluate_trigger`` costs only the verdict."""
        self._patch_enabled()
        self.session_factory.side_effect = RuntimeError("pool exhausted")
        dispatch_mock = AsyncMock(return_value={"status": "dispatched"})
        self._patch_dispatch(dispatch_mock)

        result = await self._run()

        self.assertEqual(result, {"status": "dispatched"})
        payload = dispatch_mock.await_args.args[2]
        self.assertNotIn("AI verdict", payload["message"])
        self.assertEqual(price_alert_service._metrics["verdicts_failed"], 1)
        self.assertEqual(price_alert_service._metrics["alerts_plain"], 1)

    async def test_unusable_verdict_falls_back_to_the_plain_alert(self):
        """The "unknown" sentinel is not advice, so it is not rendered."""
        self._patch_enabled()
        self._patch_client(_verdict(verdict="unknown", confidence=0.0, reasoning=""))
        dispatch_mock = AsyncMock(return_value={"status": "dispatched"})
        self._patch_dispatch(dispatch_mock)

        result = await self._run()

        self.assertEqual(result, {"status": "dispatched"})
        payload = dispatch_mock.await_args.args[2]
        self.assertNotIn("AI verdict", payload["message"])
        self.assertNotIn("verdict", payload)
        self.assertEqual(price_alert_service._metrics["verdicts_failed"], 1)
        self.assertEqual(price_alert_service._metrics["alerts_plain"], 1)

    async def test_payload_build_failure_never_escapes(self):
        """A detached order must cost the enrichment, not the alert.

        The monitor hands over a snapshot precisely so this cannot
        happen; the guard is the belt-and-braces that keeps a raising
        payload build from swallowing the alert silently.
        """
        self._patch_enabled()
        self._patch_client(_verdict())
        self._patch_dispatch(AsyncMock())
        self._patch(
            price_alert_service,
            "build_trigger_payload",
            side_effect=RuntimeError("DetachedInstanceError"),
        )

        result = await self._run()

        self.assertIsNone(result)
        self.assertEqual(price_alert_service._metrics["dispatch_failed"], 1)

    async def test_disabled_enrichment_skips_the_llm_entirely(self):
        """Past the verdict cap the alert still fires, just un-enriched."""
        self._patch_enabled()
        client = self._patch_client(_verdict())
        dispatch_mock = AsyncMock(return_value={"status": "dispatched"})
        self._patch_dispatch(dispatch_mock)

        result = await self._run(enrich=False)

        self.assertEqual(result, {"status": "dispatched"})
        client.analyze_price_alert.assert_not_awaited()
        self.session_factory.assert_not_called()
        payload = dispatch_mock.await_args.args[2]
        self.assertNotIn("AI verdict", payload["message"])
        self.assertEqual(price_alert_service._metrics["verdicts_total"], 0)
        self.assertEqual(price_alert_service._metrics["alerts_plain"], 1)

    async def test_paper_mode_order_dispatches_the_simulated_fill_message(self):
        self._patch_enabled()
        self._patch_client(_verdict())
        dispatch_mock = AsyncMock(return_value={"status": "dispatched"})
        self._patch_dispatch(dispatch_mock)

        await self._run(simulation_mode=True)

        payload = dispatch_mock.await_args.args[2]
        self.assertIn("simulated sell of 120 shares", payload["message"])
        self.assertTrue(payload["simulation_mode"])


class MetricsTests(_MetricsMixin, unittest.TestCase):
    def test_metrics_are_exposed_as_a_copy(self):
        price_alert_service._metric_inc("verdicts_total", 2)
        snapshot = price_alert_service.get_price_alert_metrics()
        self.assertEqual(snapshot["verdicts_total"], 2)
        snapshot["verdicts_total"] = 99
        self.assertEqual(price_alert_service.get_price_alert_metrics()["verdicts_total"], 2)


class EventTypeTests(unittest.TestCase):
    def test_price_event_types_are_catalog_entries(self):
        from app.services.users.alert_service import ALERT_EVENT_TYPES

        for event_type in price_alert_service.PRICE_EVENT_TYPES:
            self.assertIn(event_type, ALERT_EVENT_TYPES)


if __name__ == "__main__":
    unittest.main()
