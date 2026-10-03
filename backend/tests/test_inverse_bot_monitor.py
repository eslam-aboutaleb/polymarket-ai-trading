"""Tests for :mod:`app.services.inverse_bot_monitor`.

The monitor is a guard-railed auto-reverser, so the tests concentrate on the
decision logic rather than on plumbing:

* **Position evaluation** — ``evaluate_inverse_position_row`` walks a strict
  ladder of guards (position present, size valid, market tokens resolvable, a
  tradable alternative exists, delta above trigger, AI says reverse,
  confidence above threshold, persistence, cooldown, daily cap, credentials
  present). Each rung is asserted on the exact return payload *and* the exact
  row mutation it leaves behind, including the ``InverseBotAction`` audit
  record written for every execution outcome.
* **Flip / execution logic** — ``_execute_reversal`` is exercised on both legs
  of the trade: how notional is derived, how the marketable buy price is
  derived from the best ask, and how a partial (sell-only) outcome is
  reported. ``_resolve_alt_token`` and ``_resolve_size_mode`` cover which
  token the flip targets and how much it buys.
* **Guard ceilings** — ``force=True`` (manual "evaluate now") may exceed the
  user's configured cooldown and daily cap, but never
  ``HARD_MIN_COOLDOWN_SECONDS`` / ``HARD_MAX_REVERSALS_PER_DAY``.

No network, chain node, gRPC service or key material is used: httpx, the
credential store, the Polymarket service and the analysis client are all
replaced with local doubles.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.inverse_bot_monitor as inverse_bot_monitor
from app.models.inverse_bot_action import InverseBotAction
from app.models.inverse_bot_position import InverseBotPosition
from app.models.user_settings import AIBackendType, InverseBotSizeMode
from app.security.credential_store import CredentialStoreError

CLOB_API = inverse_bot_monitor.POLYMARKET_CLOB_API
HELD_TOKEN = "token-held"
ALT_TOKEN = "token-alt"
THIRD_TOKEN = "token-third"
CONDITION_ID = "cond-1"


# ─────────────── row / settings doubles ───────────────


def _row(**overrides) -> InverseBotPosition:
    """An InverseBotPosition with every column the evaluator reads set."""
    base = {
        "id": 11,
        "user_id": 3,
        "token_id": HELD_TOKEN,
        "condition_id": CONDITION_ID,
        "market_title": "Will it rain?",
        "outcome": "Yes",
        "enabled": True,
        "size_mode_override": "inherit",
        "fixed_amount_override": None,
        "status": "active",
        "last_signal": None,
        "last_confidence": None,
        "last_reasoning": None,
        "last_web_summary": None,
        "last_x_summary": None,
        "last_error": None,
        "last_recommendation": None,
        "last_alt_outcome": None,
        "last_alt_token_id": None,
        "last_evaluated_at": None,
        "last_reversed_at": None,
        "reversals_today": 0,
        "reversals_day": None,
        "persistence_count": 0,
    }
    base.update(overrides)
    return InverseBotPosition(**base)


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "user_id": 3,
        "inverse_bot_enabled": True,
        "inverse_bot_default_size_mode": InverseBotSizeMode.FULL_NOTIONAL.value,
        "inverse_bot_fixed_amount": 50.0,
        "inverse_bot_confidence_threshold": 75,
        "inverse_bot_cooldown_minutes": 30,
        "inverse_bot_max_reversals_per_day": 3,
        "ai_backend": AIBackendType.LLM_CHAIN.value,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _user(**overrides) -> SimpleNamespace:
    base = {"id": 3, "wallet_address": "0xwallet"}
    base.update(overrides)
    return SimpleNamespace(**base)


def _session(first_results=(), all_results=None) -> MagicMock:
    """A session double whose ``.first()``/``.all()`` results are scripted.

    ``db.add`` assigns a synthetic primary key so callers can assert on the id
    that would come back from a real ``refresh``.
    """
    db = MagicMock(name="db")
    query = MagicMock(name="query")
    db.query.return_value = query
    if first_results:
        query.filter.return_value.first.side_effect = list(first_results)
    else:
        query.filter.return_value.first.return_value = None
    query.filter.return_value.all.return_value = [] if all_results is None else list(all_results)

    def _add(obj):
        if isinstance(obj, InverseBotAction) and obj.id is None:
            obj.id = 4242
        db.added.append(obj)

    db.added = []
    db.add.side_effect = _add
    return db


def _market(held_price: float = 0.30, alt_price: float = 0.70) -> dict:
    return {
        "tokens": [
            {"token_id": HELD_TOKEN, "outcome": "Yes", "price": held_price},
            {"token_id": ALT_TOKEN, "outcome": "No", "price": alt_price},
        ],
        "question": "Will it rain?",
    }


def _positions(size: float = 100.0) -> dict:
    return {HELD_TOKEN: {"asset_id": HELD_TOKEN, "size": size, "title": "Will it rain?"}}


def _ai(**overrides) -> dict:
    """A reverse recommendation that clears every threshold by default."""
    base = {
        "recommendation": "reverse",
        "confidence": 88.0,
        "reasoning": "Model flipped its view.",
        "alt_outcome": "No",
        "alt_token_id": ALT_TOKEN,
        "web_summary": "web",
        "x_summary": "x",
    }
    base.update(overrides)
    return base


# ─────────────── httpx doubles ───────────────


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.init_kwargs: dict = {}
        self.get_calls: list[tuple[str, dict | None]] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, url, params=None):
        self.get_calls.append((url, params))
        if self.error is not None:
            raise self.error
        return self.response


class _FakeSyncClient:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.init_kwargs: dict = {}
        self.get_calls: list[tuple[str, dict | None]] = []

    def __enter__(self) -> _FakeSyncClient:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def get(self, url, params=None):
        self.get_calls.append((url, params))
        if self.error is not None:
            raise self.error
        return self.response


def _async_client_patch(client: _FakeAsyncClient):
    def _factory(**kwargs):
        client.init_kwargs = kwargs
        return client

    return patch.object(inverse_bot_monitor.httpx, "AsyncClient", _factory)


def _sync_client_patch(client: _FakeSyncClient):
    def _factory(**kwargs):
        client.init_kwargs = kwargs
        return client

    return patch.object(inverse_bot_monitor.httpx, "Client", _factory)


# ─────────────── module metrics / registry isolation ───────────────


class _MonitorStateMixin:
    def isolate_monitor_state(self) -> None:
        """Snapshot the module-level singletons and restore them afterwards."""

        def _restore() -> None:
            inverse_bot_monitor._metrics.clear()
            inverse_bot_monitor._metrics.update(self._metrics)
            inverse_bot_monitor._inflight_keys.clear()
            inverse_bot_monitor._inflight_keys.update(self._inflight)
            inverse_bot_monitor._monitor_task = self._task
            inverse_bot_monitor._last_tick_at = self._last_tick

        self._metrics = dict(inverse_bot_monitor._metrics)
        self._inflight = set(inverse_bot_monitor._inflight_keys)
        self._task = inverse_bot_monitor._monitor_task
        self._last_tick = inverse_bot_monitor._last_tick_at
        inverse_bot_monitor._metrics.clear()
        inverse_bot_monitor._metrics.update(
            {
                "evaluations_total": 0,
                "reversals_total": 0,
                "reversals_failed": 0,
                "sell_only_total": 0,
                "mcp_errors_total": 0,
            }
        )
        inverse_bot_monitor._inflight_keys.clear()
        inverse_bot_monitor._monitor_task = None
        inverse_bot_monitor._last_tick_at = None
        self.addCleanup(_restore)


# ─────────────── metrics ───────────────


class MetricTests(_MonitorStateMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.isolate_monitor_state()

    def test_metric_inc_creates_unknown_keys(self):
        inverse_bot_monitor._metric_inc("brand_new")
        self.assertEqual(inverse_bot_monitor._metrics["brand_new"], 1)

    def test_metric_inc_accumulates_by_delta(self):
        inverse_bot_monitor._metric_inc("reversals_total")
        inverse_bot_monitor._metric_inc("reversals_total", 4)
        self.assertEqual(inverse_bot_monitor._metrics["reversals_total"], 5)

    def test_metrics_snapshot_shape_when_idle(self):
        metrics = inverse_bot_monitor.get_inverse_bot_metrics()

        self.assertEqual(
            metrics,
            {
                "evaluations_total": 0,
                "reversals_total": 0,
                "reversals_failed": 0,
                "sell_only_total": 0,
                "mcp_errors_total": 0,
                "running": False,
                "last_tick_at": None,
                "inflight": 0,
                "interval_seconds": inverse_bot_monitor.INVERSE_BOT_CHECK_INTERVAL,
            },
        )

    async def test_metrics_snapshot_reports_task_inflight_and_tick(self):
        tick = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        inverse_bot_monitor._last_tick_at = tick
        inverse_bot_monitor._inflight_keys.update({"3:cond-1", "3:cond-2"})
        task = await _pending_task()
        inverse_bot_monitor._monitor_task = task

        metrics = inverse_bot_monitor.get_inverse_bot_metrics()

        self.assertIs(metrics["running"], True)
        self.assertEqual(metrics["last_tick_at"], tick.isoformat())
        self.assertEqual(metrics["inflight"], 2)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertIs(inverse_bot_monitor.get_inverse_bot_metrics()["running"], False)


# ─────────────── small pure helpers ───────────────


class ResolveAltTokenTests(unittest.TestCase):
    def setUp(self):
        self.tokens = [
            {"token_id": HELD_TOKEN, "outcome": "Yes", "price": 0.3},
            {"token_id": ALT_TOKEN, "outcome": "No", "price": 0.7},
            {"token_id": THIRD_TOKEN, "outcome": "Maybe", "price": 0.2},
        ]
        self.best_alt = self.tokens[1]

    def test_token_id_match_wins(self):
        chosen = inverse_bot_monitor._resolve_alt_token(
            self.tokens, {"alt_token_id": THIRD_TOKEN, "alt_outcome": "No"}, self.best_alt
        )
        self.assertIs(chosen, self.tokens[2])

    def test_unknown_token_id_falls_back_to_outcome(self):
        chosen = inverse_bot_monitor._resolve_alt_token(
            self.tokens, {"alt_token_id": "nope", "alt_outcome": "maybe"}, self.best_alt
        )
        self.assertIs(chosen, self.tokens[2])

    def test_outcome_match_is_case_insensitive(self):
        chosen = inverse_bot_monitor._resolve_alt_token(
            self.tokens, {"alt_outcome": "  NO "}, self.best_alt
        )
        self.assertIs(chosen, self.tokens[1])

    def test_blank_token_id_is_ignored(self):
        chosen = inverse_bot_monitor._resolve_alt_token(
            self.tokens, {"alt_token_id": "   ", "alt_outcome": ""}, self.best_alt
        )
        self.assertIs(chosen, self.best_alt)

    def test_matching_the_held_token_is_accepted(self):
        # Documented quirk: the resolver never rejects the currently-held
        # token, so an AI that echoes it back resolves to the same token.
        chosen = inverse_bot_monitor._resolve_alt_token(
            self.tokens, {"alt_token_id": HELD_TOKEN}, self.best_alt
        )
        self.assertIs(chosen, self.tokens[0])

    def test_empty_ai_eval_returns_best_alt(self):
        self.assertIs(
            inverse_bot_monitor._resolve_alt_token(self.tokens, {}, self.best_alt),
            self.best_alt,
        )


class ResolveSizeModeTests(unittest.TestCase):
    def test_inherit_override_uses_settings_default(self):
        row = _row(size_mode_override="inherit")
        settings = _settings(inverse_bot_default_size_mode="FIXED_AMOUNT")
        self.assertEqual(inverse_bot_monitor._resolve_size_mode(row, settings), "fixed_amount")

    def test_missing_override_is_treated_as_inherit(self):
        row = _row(size_mode_override=None)
        settings = _settings(inverse_bot_default_size_mode=InverseBotSizeMode.FULL_NOTIONAL.value)
        self.assertEqual(inverse_bot_monitor._resolve_size_mode(row, settings), "full_notional")

    def test_null_settings_default_falls_back_to_full_notional(self):
        row = _row(size_mode_override="inherit")
        settings = _settings(inverse_bot_default_size_mode=None)
        self.assertEqual(
            inverse_bot_monitor._resolve_size_mode(row, settings),
            InverseBotSizeMode.FULL_NOTIONAL.value,
        )

    def test_explicit_override_wins_and_is_lowercased(self):
        row = _row(size_mode_override="FIXED_AMOUNT")
        settings = _settings(inverse_bot_default_size_mode="full_notional")
        self.assertEqual(inverse_bot_monitor._resolve_size_mode(row, settings), "fixed_amount")


# ─────────────── best-ask lookup ───────────────


class BestAskForTokenTests(unittest.TestCase):
    def test_takes_lowest_ask(self):
        client = _FakeSyncClient(
            _FakeResponse(payload={"asks": [{"price": 0.62}, {"price": "0.55"}, {"price": 0.9}]})
        )

        with _sync_client_patch(client):
            best = inverse_bot_monitor._best_ask_for_token(ALT_TOKEN)

        self.assertEqual(best, 0.55)
        self.assertEqual(client.get_calls, [(f"{CLOB_API}/book", {"token_id": ALT_TOKEN})])
        self.assertEqual(client.init_kwargs, {"timeout": 10.0})

    def test_clamps_into_unit_interval(self):
        for asks, expected in (
            ([{"price": 1.5}], 1.0),
            ([{"price": -0.25}], 0.0),
        ):
            with (
                self.subTest(asks=asks),
                _sync_client_patch(_FakeSyncClient(_FakeResponse(payload={"asks": asks}))),
            ):
                self.assertEqual(inverse_bot_monitor._best_ask_for_token(ALT_TOKEN), expected)

    def test_non_200_returns_zero(self):
        with _sync_client_patch(_FakeSyncClient(_FakeResponse(status_code=500))):
            self.assertEqual(inverse_bot_monitor._best_ask_for_token(ALT_TOKEN), 0.0)

    def test_empty_ask_side_returns_zero(self):
        with _sync_client_patch(_FakeSyncClient(_FakeResponse(payload={"asks": []}))):
            self.assertEqual(inverse_bot_monitor._best_ask_for_token(ALT_TOKEN), 0.0)

    def test_all_prices_none_raises_inside_and_is_swallowed(self):
        client = _FakeSyncClient(_FakeResponse(payload={"asks": [{"price": None}]}))

        with _sync_client_patch(client):
            self.assertEqual(inverse_bot_monitor._best_ask_for_token(ALT_TOKEN), 0.0)

    def test_transport_error_returns_zero(self):
        with _sync_client_patch(_FakeSyncClient(error=RuntimeError("boom"))):
            self.assertEqual(inverse_bot_monitor._best_ask_for_token(ALT_TOKEN), 0.0)


# ─────────────── market token lookup ───────────────


class FetchMarketTokensTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalises_tokens(self):
        client = _FakeAsyncClient(
            _FakeResponse(
                payload={
                    "question": "Will it rain?",
                    "tokens": [
                        {"token_id": 111, "outcome": "Yes", "price": "0.3"},
                        {"token_id": "", "outcome": "Ghost", "price": 0.9},
                        {"token_id": 222, "outcome": "No", "price": "not-a-number"},
                    ],
                }
            )
        )

        with _async_client_patch(client):
            market = await inverse_bot_monitor._fetch_market_tokens(CONDITION_ID)

        self.assertEqual(
            market,
            {
                "tokens": [
                    {"token_id": "111", "outcome": "Yes", "price": 0.3},
                    {"token_id": "222", "outcome": "No", "price": 0.0},
                ],
                "question": "Will it rain?",
            },
        )
        self.assertEqual(client.get_calls, [(f"{CLOB_API}/markets/{CONDITION_ID}", None)])
        self.assertEqual(client.init_kwargs, {"timeout": 15.0})

    async def test_non_200_returns_empty_dict(self):
        with _async_client_patch(_FakeAsyncClient(_FakeResponse(status_code=404))):
            self.assertEqual(await inverse_bot_monitor._fetch_market_tokens(CONDITION_ID), {})

    async def test_transport_error_returns_empty_dict(self):
        with _async_client_patch(_FakeAsyncClient(error=RuntimeError("boom"))):
            self.assertEqual(await inverse_bot_monitor._fetch_market_tokens(CONDITION_ID), {})

    async def test_payload_without_tokens_still_returns_a_mapping(self):
        with _async_client_patch(_FakeAsyncClient(_FakeResponse(payload={}))):
            market = await inverse_bot_monitor._fetch_market_tokens(CONDITION_ID)
        self.assertEqual(market, {"tokens": [], "question": ""})


# ─────────────── reversal execution (the flip) ───────────────


class ExecuteReversalTests(unittest.TestCase):
    def setUp(self):
        self.sell = MagicMock(return_value={"success": True, "order_hash": "0xsell"})
        self.buy = MagicMock(return_value={"success": True, "order_hash": "0xbuy"})
        patcher = patch.object(inverse_bot_monitor, "_place_order_on_polymarket")
        self.place = patcher.start()
        self.addCleanup(patcher.stop)
        self.place.side_effect = [self.sell.return_value, self.buy.return_value]
        self.best_ask = patch.object(
            inverse_bot_monitor, "_best_ask_for_token", return_value=0.6
        ).start()
        self.addCleanup(patch.stopall)

    def _execute(self, row=None, settings=None, **overrides):
        params = {
            "row": row if row is not None else _row(),
            "settings": settings if settings is not None else _settings(),
            "stored_creds": {"private_key": "0xpk", "clob_creds": {"api_key": "k"}},
            "from_size": 100.0,
            "from_price": 0.5,
            "to_token_id": ALT_TOKEN,
            "to_outcome": "No",
            "to_price": 0.65,
            "confidence": 88.0,
            "recommendation": "reverse",
        }
        params.update(overrides)
        return inverse_bot_monitor._execute_reversal(**params)

    def test_successful_flip_places_sell_then_buy(self):
        result = self._execute()

        self.assertEqual(
            result,
            {
                "status": "success",
                "error": None,
                "sell_order_hash": "0xsell",
                "buy_order_hash": "0xbuy",
                # 100 shares * 0.5 = $50 of notional, less the 1.5% slippage guard.
                "buy_notional": 49.25,
            },
        )
        self.assertEqual(
            [call.kwargs for call in self.place.call_args_list],
            [
                {
                    "private_key": "0xpk",
                    "clob_creds": {"api_key": "k"},
                    "token_id": HELD_TOKEN,
                    "side": "SELL",
                    "price": 0.5,
                    "size": 100.0,
                },
                {
                    "private_key": "0xpk",
                    "clob_creds": {"api_key": "k"},
                    "token_id": ALT_TOKEN,
                    "side": "BUY",
                    # best ask 0.60, marked up by the 1.5% buy guard.
                    "price": 0.6 * (1.0 + inverse_bot_monitor.BUY_SLIPPAGE_GUARD),
                    "size": 49.25,
                },
            ],
        )
        self.best_ask.assert_called_once_with(ALT_TOKEN)

    def test_sell_price_is_clamped_into_the_book(self):
        for from_price, expected in ((1.5, 0.999), (0.0001, 0.001), (0.42, 0.42)):
            with self.subTest(from_price=from_price):
                self.place.reset_mock()
                self.place.side_effect = [
                    {"success": True, "order_hash": "s"},
                    {"success": True, "order_hash": "b"},
                ]
                self._execute(from_price=from_price)
                self.assertEqual(self.place.call_args_list[0].kwargs["price"], expected)

    def test_failed_sell_leg_stops_before_the_buy(self):
        self.place.side_effect = [{"success": False, "error": "no allowance", "order_hash": "s"}]

        result = self._execute()

        self.assertEqual(
            result,
            {
                "status": "failed",
                "error": "no allowance",
                "sell_order_hash": "s",
                "buy_order_hash": None,
                "buy_notional": None,
            },
        )
        self.assertEqual(self.place.call_count, 1)

    def test_failed_sell_without_error_uses_default_message(self):
        self.place.side_effect = [{"success": False}]
        result = self._execute()
        self.assertEqual(result["error"], "SELL leg failed")

    def test_full_notional_is_floored_at_the_minimum_buy(self):
        # Full-notional sizing wraps the derived amount in ``max(1.0, ...)``,
        # so a $0.50 dust position still buys the $1.00 minimum rather than
        # degrading to sell-only. The "below minimum" branch is only
        # reachable through fixed-amount sizing (see the next test).
        result = self._execute(from_size=1.0, from_price=0.5)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["buy_notional"], 1.0)
        self.assertEqual(self.place.call_args_list[1].kwargs["size"], 1.0)

    def test_fixed_amount_below_minimum_becomes_sell_only(self):
        row = _row(size_mode_override="fixed_amount", fixed_amount_override=0.4)

        result = self._execute(row=row)

        self.assertEqual(result["status"], "sell_only")
        self.assertEqual(result["error"], "Buy notional below minimum ($0.4000) after sell.")
        self.assertEqual(result["buy_notional"], 0.4)
        self.assertEqual(result["sell_order_hash"], "0xsell")
        self.assertIsNone(result["buy_order_hash"])
        # The buy leg is never priced or sent.
        self.assertEqual(self.place.call_count, 1)
        self.best_ask.assert_not_called()

    def test_fixed_amount_override_sizes_the_buy(self):
        row = _row(size_mode_override="fixed_amount", fixed_amount_override=25.0)
        result = self._execute(row=row)
        self.assertEqual(result["buy_notional"], 25.0)
        self.assertEqual(self.place.call_args_list[1].kwargs["size"], 25.0)

    def test_fixed_amount_falls_back_to_settings_then_default(self):
        self.place.reset_mock()
        self.place.side_effect = [
            {"success": True, "order_hash": "s"},
            {"success": True, "order_hash": "b"},
        ]
        result = self._execute(
            row=_row(size_mode_override="fixed_amount", fixed_amount_override=0.0),
            settings=_settings(inverse_bot_fixed_amount=12.5),
        )
        self.assertEqual(result["buy_notional"], 12.5)

        self.place.reset_mock()
        self.place.side_effect = [
            {"success": True, "order_hash": "s"},
            {"success": True, "order_hash": "b"},
        ]
        result = self._execute(
            row=_row(size_mode_override="fixed_amount"),
            settings=_settings(inverse_bot_fixed_amount=None),
        )
        self.assertEqual(result["buy_notional"], 50.0)

    def test_marketable_price_uses_reported_to_price_when_book_is_empty(self):
        self.best_ask.return_value = 0.0
        result = self._execute()
        self.assertEqual(result["status"], "success")
        self.assertEqual(
            self.place.call_args_list[1].kwargs["price"],
            0.65 * (1.0 + inverse_bot_monitor.BUY_SLIPPAGE_GUARD),
        )

    def test_marketable_price_is_capped_at_0999(self):
        self.best_ask.return_value = 0.999
        self._execute()
        self.assertEqual(self.place.call_args_list[1].kwargs["price"], 0.999)

    def test_failed_buy_leg_degrades_to_sell_only(self):
        self.place.side_effect = [
            {"success": True, "order_hash": "s"},
            {"success": False, "error": "insufficient USDC", "order_hash": None},
        ]

        result = self._execute()

        self.assertEqual(
            result,
            {
                "status": "sell_only",
                "error": "insufficient USDC",
                "sell_order_hash": "s",
                "buy_order_hash": None,
                "buy_notional": 49.25,
            },
        )

    def test_failed_buy_without_error_uses_default_message(self):
        self.place.side_effect = [
            {"success": True, "order_hash": "s"},
            {"success": False},
        ]
        result = self._execute()
        self.assertEqual(result["error"], "BUY leg failed after SELL")
        self.assertIsNone(result["buy_order_hash"])

    def test_vestigial_parameters_do_not_change_any_call(self):
        """``to_outcome`` / ``confidence`` / ``recommendation`` are ignored.

        The signature keeps them for call-site compatibility, but the function
        body never reads them: the flip is driven purely by ``row.token_id``,
        ``to_token_id`` and the two prices. Two invocations that differ only in
        those three arguments must therefore place byte-identical orders.
        """
        baseline = None
        for to_outcome, confidence, recommendation in (
            ("No", 88.0, "reverse"),
            ("Yes", 5.0, "hold"),
            ("", 0.0, ""),
        ):
            with self.subTest(to_outcome=to_outcome, confidence=confidence):
                self.place.reset_mock()
                self.place.side_effect = [
                    {"success": True, "order_hash": "s"},
                    {"success": True, "order_hash": "b"},
                ]
                result = self._execute(
                    to_outcome=to_outcome,
                    confidence=confidence,
                    recommendation=recommendation,
                )
                calls = [call.kwargs for call in self.place.call_args_list]
                if baseline is None:
                    baseline = (calls, result)
                    self.assertEqual(calls[0]["token_id"], HELD_TOKEN)
                    self.assertEqual(calls[1]["token_id"], ALT_TOKEN)
                else:
                    self.assertEqual(calls, baseline[0])
                    self.assertEqual(result, baseline[1])


# ─────────────── AI evaluation ───────────────


class EvaluateWithAiTests(_MonitorStateMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.isolate_monitor_state()
        self.client = MagicMock()
        self.client.evaluate_inverse_position = AsyncMock(return_value=dict(_ai()))
        self.client.close = AsyncMock()
        patcher = patch.object(inverse_bot_monitor, "AnalysisClient", return_value=self.client)
        self.analysis_cls = patcher.start()
        self.addCleanup(patcher.stop)

    async def test_forwards_every_prompt_field_to_the_client(self):
        db = _session(first_results=[_settings()])
        alternatives = [{"token_id": ALT_TOKEN, "outcome": "No", "price": 0.7}]

        result = await inverse_bot_monitor._evaluate_with_ai(
            db=db,
            user_id=3,
            condition_id=CONDITION_ID,
            market_title="Will it rain?",
            held_outcome="Yes",
            held_pct=0.3,
            best_alt_outcome="No",
            best_alt_pct=0.7,
            delta_pct=0.4,
            alternatives=alternatives,
        )

        self.client.evaluate_inverse_position.assert_awaited_once_with(
            condition_id=CONDITION_ID,
            market_title="Will it rain?",
            held_outcome="Yes",
            held_pct=0.3,
            best_alt_outcome="No",
            best_alt_pct=0.7,
            delta_pct=0.4,
            alternatives_json=alternatives,
            include_research=True,
            user_id="3",
        )
        self.client.close.assert_awaited_once_with()
        self.assertEqual(result["recommendation"], "reverse")
        self.assertEqual(result["confidence"], 88.0)
        self.assertEqual(result["reasoning"], "Model flipped its view.")
        self.assertEqual(result["alt_outcome"], "No")
        self.assertEqual(result["alt_token_id"], ALT_TOKEN)
        self.assertEqual(result["web_summary"], "web")
        self.assertEqual(result["x_summary"], "x")
        self.assertEqual(result["key_risks"], [])

    async def test_cli_agent_backend_is_selected_from_settings(self):
        db = _session(first_results=[_settings(ai_backend=AIBackendType.CLI_AGENT.value)])
        await inverse_bot_monitor._evaluate_with_ai(
            db=db,
            user_id=3,
            condition_id=CONDITION_ID,
            market_title="t",
            held_outcome="Yes",
            held_pct=0.3,
            best_alt_outcome="No",
            best_alt_pct=0.7,
            delta_pct=0.4,
            alternatives=[],
        )

        self.assertEqual(
            self.analysis_cls.call_args.kwargs,
            {"backend": inverse_bot_monitor.AIBackend.CLI_AGENT},
        )

    async def test_llm_chain_is_the_default_backend(self):
        db = _session(first_results=[None])
        await inverse_bot_monitor._evaluate_with_ai(
            db=db,
            user_id=3,
            condition_id=CONDITION_ID,
            market_title="t",
            held_outcome="Yes",
            held_pct=0.3,
            best_alt_outcome="No",
            best_alt_pct=0.7,
            delta_pct=0.4,
            alternatives=[],
        )
        self.assertEqual(
            self.analysis_cls.call_args.kwargs,
            {"backend": inverse_bot_monitor.AIBackend.LLM_CHAIN},
        )

    async def test_missing_x_summary_caps_confidence_at_seventy(self):
        self.client.evaluate_inverse_position.return_value = _ai(confidence=95.0, x_summary="")
        db = _session(first_results=[_settings()])

        result = await inverse_bot_monitor._evaluate_with_ai(
            db=db,
            user_id=3,
            condition_id=CONDITION_ID,
            market_title="t",
            held_outcome="Yes",
            held_pct=0.3,
            best_alt_outcome="No",
            best_alt_pct=0.7,
            delta_pct=0.4,
            alternatives=[],
        )

        self.assertEqual(result["confidence"], 70.0)

    async def test_cap_does_not_raise_a_low_confidence(self):
        self.client.evaluate_inverse_position.return_value = _ai(confidence=61.0, x_summary="")
        db = _session(first_results=[_settings()])

        result = await inverse_bot_monitor._evaluate_with_ai(
            db=db,
            user_id=3,
            condition_id=CONDITION_ID,
            market_title="t",
            held_outcome="Yes",
            held_pct=0.3,
            best_alt_outcome="No",
            best_alt_pct=0.7,
            delta_pct=0.4,
            alternatives=[],
        )

        self.assertEqual(result["confidence"], 61.0)

    async def test_client_failure_degrades_to_hold(self):
        self.client.evaluate_inverse_position.side_effect = RuntimeError("grpc down")
        db = _session(first_results=[_settings()])

        result = await inverse_bot_monitor._evaluate_with_ai(
            db=db,
            user_id=3,
            condition_id=CONDITION_ID,
            market_title="t",
            held_outcome="Yes",
            held_pct=0.3,
            best_alt_outcome="No",
            best_alt_pct=0.7,
            delta_pct=0.4,
            alternatives=[],
        )

        self.assertEqual(result["recommendation"], "hold")
        self.assertEqual(result["confidence"], 0.0)
        self.assertEqual(result["reasoning"], "")
        self.assertEqual(result["alt_outcome"], "No")
        self.assertEqual(result["alt_token_id"], "")
        self.assertEqual(inverse_bot_monitor._metrics["mcp_errors_total"], 1)
        # The client is closed even though the call blew up.
        self.client.close.assert_awaited_once_with()

    async def test_analysis_key_is_used_as_reasoning_fallback(self):
        self.client.evaluate_inverse_position.return_value = {
            "recommendation": "HOLD",
            "analysis": "prose only",
        }
        db = _session(first_results=[_settings()])

        result = await inverse_bot_monitor._evaluate_with_ai(
            db=db,
            user_id=3,
            condition_id=CONDITION_ID,
            market_title="t",
            held_outcome="Yes",
            held_pct=0.3,
            best_alt_outcome="No",
            best_alt_pct=0.7,
            delta_pct=0.4,
            alternatives=[],
        )

        self.assertEqual(result["recommendation"], "hold")
        self.assertEqual(result["reasoning"], "prose only")


# ─────────────── the evaluation ladder ───────────────


class EvaluatePositionRowTests(_MonitorStateMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.isolate_monitor_state()
        self.db = _session()
        self.user = _user()
        self.settings = _settings()
        self.row = _row()
        self.market = patch.object(
            inverse_bot_monitor, "_fetch_market_tokens", new=AsyncMock(return_value=_market())
        ).start()
        self.addCleanup(patch.stopall)
        self.ai = patch.object(
            inverse_bot_monitor, "_evaluate_with_ai", new=AsyncMock(return_value=_ai())
        ).start()
        self.execute = patch.object(
            inverse_bot_monitor,
            "_execute_reversal",
            return_value={
                "status": "success",
                "error": None,
                "sell_order_hash": "0xsell",
                "buy_order_hash": "0xbuy",
                "buy_notional": 49.25,
            },
        ).start()

    async def _evaluate(self, row=None, positions=None, force=False, stored=None, market=None):
        if row is not None:
            self.row = row
        if market is not None:
            self.market.return_value = market
        return await inverse_bot_monitor.evaluate_inverse_position_row(
            db=self.db,
            user=self.user,
            settings=self.settings,
            row=self.row,
            stored_creds=stored if stored is not None else {"private_key": "0xpk"},
            positions_by_token=positions if positions is not None else _positions(),
            force=force,
        )

    def _action(self) -> InverseBotAction:
        self.assertEqual(len(self.db.added), 1)
        action = self.db.added[0]
        self.assertIsInstance(action, InverseBotAction)
        return action

    @staticmethod
    def _persisted_row(**overrides) -> InverseBotPosition:
        """A row whose persistence requirement is already satisfied.

        ``evaluate_inverse_position_row`` only advances ``persistence_count``
        when the previous signal already matches the current one, so reaching
        the cooldown / cap / execution rungs needs both fields primed.
        """
        fields = {"last_signal": f"reverse:{ALT_TOKEN}", "persistence_count": 1}
        fields.update(overrides)
        return _row(**fields)

    # ── pre-flight guards ──

    async def test_missing_position_disables_the_row(self):
        result = await self._evaluate(positions={})

        self.assertEqual(
            result,
            {"status": "disabled", "reason": "Position closed or token no longer held."},
        )
        self.assertFalse(self.row.enabled)
        self.assertEqual(self.row.status, "error")
        self.assertEqual(self.row.last_error, "Position closed or token no longer held.")
        self.assertIsNotNone(self.row.last_evaluated_at)
        self.ai.assert_not_awaited()

    async def test_zero_size_position_disables_the_row(self):
        result = await self._evaluate(positions=_positions(size=0.0))

        self.assertEqual(result, {"status": "disabled", "reason": "Invalid position size."})
        self.assertFalse(self.row.enabled)
        self.assertEqual(self.row.status, "error")

    async def test_unfetchable_market_keeps_the_row_enabled(self):
        self.market.return_value = {}

        result = await self._evaluate()

        self.assertEqual(
            result,
            {"status": "error", "reason": "Unable to fetch market token distribution."},
        )
        self.assertTrue(self.row.enabled)
        self.assertEqual(self.row.status, "error")

    async def test_market_without_tokens_is_an_error(self):
        result = await self._evaluate(market={"tokens": [], "question": "q"})
        self.assertEqual(
            result, {"status": "error", "reason": "Unable to fetch market token distribution."}
        )

    async def test_held_token_missing_from_market(self):
        market = {"tokens": [{"token_id": ALT_TOKEN, "outcome": "No", "price": 0.7}]}

        result = await self._evaluate(market=market)

        self.assertEqual(result, {"status": "error", "reason": "Held token not found in market."})
        self.assertTrue(self.row.enabled)

    async def test_no_tradable_alternative(self):
        market = _market(alt_price=0.0)

        result = await self._evaluate(market=market)

        self.assertEqual(
            result, {"status": "error", "reason": "No tradable alternative token found."}
        )
        self.ai.assert_not_awaited()

    # ── delta gate ──

    async def test_delta_below_trigger_holds_and_resets_persistence(self):
        self.row.persistence_count = 3

        result = await self._evaluate(market=_market(held_price=0.32, alt_price=0.36))

        self.assertEqual(result["status"], "hold")
        self.assertAlmostEqual(result["delta_pct"], 0.04)
        self.assertEqual(self.row.persistence_count, 0)
        self.assertEqual(self.row.status, "active")
        self.assertEqual(self.row.last_recommendation, "hold")
        self.assertEqual(
            self.row.last_reasoning,
            "Delta below trigger: best alternative 0.360 vs held 0.320.",
        )
        self.assertEqual(self.row.last_signal, "delta=0.0400")
        self.ai.assert_not_awaited()

    async def test_delta_exactly_at_trigger_consults_the_model(self):
        held, alt = 0.32, 0.40  # delta == INVERSE_DELTA_TRIGGER

        result = await self._evaluate(market=_market(held_price=held, alt_price=alt))

        self.ai.assert_awaited_once()
        self.assertAlmostEqual(self.ai.await_args.kwargs["delta_pct"], 0.08)
        self.assertEqual(result["status"], "pending_persistence")

    async def test_ai_prompt_prefers_row_metadata_over_market_data(self):
        await self._evaluate(row=_row(market_title="", outcome=""))

        kwargs = self.ai.await_args.kwargs
        self.assertEqual(kwargs["market_title"], "Will it rain?")  # position title
        self.assertEqual(kwargs["held_outcome"], "Yes")  # market token outcome
        self.assertEqual(kwargs["best_alt_outcome"], "No")
        self.assertEqual(kwargs["best_alt_pct"], 0.7)

    # ── AI gates ──

    async def test_ai_hold_resets_persistence(self):
        self.ai.return_value = _ai(recommendation="hold", confidence=99.0)
        self.row.persistence_count = 2

        result = await self._evaluate()

        self.assertEqual(result, {"status": "hold", "reason": "AI recommendation not reverse"})
        self.assertEqual(self.row.persistence_count, 0)
        self.assertEqual(self.row.status, "active")
        self.assertEqual(self.row.last_confidence, 99.0)
        self.assertEqual(self.row.last_recommendation, "hold")
        self.assertEqual(self.row.last_alt_outcome, "No")
        self.assertEqual(self.row.last_alt_token_id, ALT_TOKEN)
        self.execute.assert_not_called()

    async def test_confidence_below_threshold_holds(self):
        self.ai.return_value = _ai(confidence=74.9)

        result = await self._evaluate()

        self.assertEqual(result, {"status": "hold", "reason": "Confidence below threshold"})
        self.assertEqual(self.row.persistence_count, 0)
        self.assertEqual(self.row.status, "active")
        self.execute.assert_not_called()

    async def test_confidence_exactly_at_threshold_proceeds(self):
        self.ai.return_value = _ai(confidence=75.0)

        result = await self._evaluate()

        self.assertEqual(result, {"status": "pending_persistence", "count": 1})

    async def test_null_threshold_falls_back_to_seventy_five(self):
        self.settings.inverse_bot_confidence_threshold = None
        self.ai.return_value = _ai(confidence=74.0)

        result = await self._evaluate()

        self.assertEqual(result, {"status": "hold", "reason": "Confidence below threshold"})

    async def test_ai_metadata_is_persisted_onto_the_row(self):
        self.ai.return_value = {
            "recommendation": "reverse",
            "confidence": 81.5,
            "reasoning": "why",
            "web_summary": "web",
            "x_summary": "x",
            "alt_outcome": "Maybe",
            "alt_token_id": THIRD_TOKEN,
        }

        await self._evaluate()

        self.assertEqual(self.row.last_confidence, 81.5)
        self.assertEqual(self.row.last_recommendation, "reverse")
        self.assertEqual(self.row.last_reasoning, "why")
        self.assertEqual(self.row.last_web_summary, "web")
        self.assertEqual(self.row.last_x_summary, "x")
        self.assertEqual(self.row.last_alt_outcome, "Maybe")
        self.assertEqual(self.row.last_alt_token_id, THIRD_TOKEN)

    # ── persistence ──

    async def test_first_signal_only_records_persistence(self):
        result = await self._evaluate()

        self.assertEqual(result, {"status": "pending_persistence", "count": 1})
        self.assertEqual(self.row.persistence_count, 1)
        self.assertEqual(self.row.last_signal, f"reverse:{ALT_TOKEN}")
        self.assertEqual(self.row.status, "active")
        self.execute.assert_not_called()

    async def test_repeated_signal_satisfies_persistence(self):
        result = await self._evaluate(row=self._persisted_row())

        self.assertEqual(result["status"], "success")
        self.assertEqual(self.row.persistence_count, 0)

    async def test_changing_signal_restarts_persistence(self):
        result = await self._evaluate(row=_row(last_signal=f"reverse:{THIRD_TOKEN}"))

        self.assertEqual(result, {"status": "pending_persistence", "count": 1})
        self.assertEqual(self.row.persistence_count, 1)
        self.assertEqual(self.row.last_signal, f"reverse:{ALT_TOKEN}")

    async def test_force_skips_persistence(self):
        result = await self._evaluate(force=True)

        self.assertEqual(result["status"], "success")
        self.assertEqual(self.row.persistence_count, 0)

    # ── cooldown ──

    async def test_configured_cooldown_blocks_a_recent_reversal(self):
        row = self._persisted_row(last_reversed_at=datetime.now(UTC) - timedelta(minutes=5))

        result = await self._evaluate(row=row)

        self.assertEqual(result["status"], "cooldown")
        self.assertAlmostEqual(result["seconds_remaining"], 1500.0, delta=2.0)
        self.assertEqual(self.row.status, "cooldown")
        self.execute.assert_not_called()

    async def test_cooldown_expiry_lets_the_reversal_through(self):
        row = self._persisted_row(last_reversed_at=datetime.now(UTC) - timedelta(minutes=31))

        result = await self._evaluate(row=row)

        self.assertEqual(result["status"], "success")

    async def test_naive_last_reversed_at_is_treated_as_utc(self):
        naive = (datetime.now(UTC) - timedelta(minutes=5)).replace(tzinfo=None)
        row = self._persisted_row(last_reversed_at=naive)

        result = await self._evaluate(row=row)

        self.assertEqual(result["status"], "cooldown")

    async def test_force_uses_the_hard_cooldown_floor(self):
        self.settings.inverse_bot_cooldown_minutes = 1  # 60s configured

        # 120s since the last flip clears both the configured cooldown and the
        # 60s hard floor, so force proceeds.
        result = await self._evaluate(
            row=self._persisted_row(last_reversed_at=datetime.now(UTC) - timedelta(seconds=120)),
            force=True,
        )
        self.assertEqual(result["status"], "success")
        self.execute.assert_called_once()

        # 30s is inside the hard floor even though force skips the configured
        # cooldown entirely.
        self.execute.reset_mock()
        result = await self._evaluate(
            row=self._persisted_row(last_reversed_at=datetime.now(UTC) - timedelta(seconds=30)),
            force=True,
        )
        self.assertEqual(result["status"], "cooldown")
        self.assertAlmostEqual(result["seconds_remaining"], 30.0, delta=2.0)
        self.execute.assert_not_called()

    # ── daily cap ──

    async def test_user_daily_cap_blocks(self):
        row = self._persisted_row(reversals_day=datetime.now(UTC).date(), reversals_today=3)

        result = await self._evaluate(row=row)

        self.assertEqual(result, {"status": "daily_cap_reached"})
        self.assertEqual(self.row.status, "cooldown")
        self.execute.assert_not_called()

    async def test_new_day_resets_the_counter(self):
        yesterday = datetime.now(UTC).date() - timedelta(days=1)
        row = self._persisted_row(reversals_day=yesterday, reversals_today=3)

        result = await self._evaluate(row=row)

        self.assertEqual(result["status"], "success")
        self.assertEqual(self.row.reversals_today, 1)
        # The rollover is keyed on the UTC date so it agrees with the
        # UTC-stamped `last_reversed_at` cooldown arithmetic. Using the host's
        # local date here reset the cap early on non-UTC hosts, allowing extra
        # reversals per day.
        self.assertEqual(self.row.reversals_day, datetime.now(UTC).date())

    async def test_force_raises_the_cap_to_the_hard_ceiling(self):
        # Above the user's cap of 3 but below the server cap of 10.
        result = await self._evaluate(
            row=self._persisted_row(reversals_day=datetime.now(UTC).date(), reversals_today=3),
            force=True,
        )
        self.assertEqual(result["status"], "success")
        self.execute.assert_called_once()

        self.execute.reset_mock()
        result = await self._evaluate(
            row=self._persisted_row(
                reversals_day=datetime.now(UTC).date(),
                reversals_today=inverse_bot_monitor.HARD_MAX_REVERSALS_PER_DAY,
            ),
            force=True,
        )
        self.assertEqual(result, {"status": "daily_cap_reached"})
        self.execute.assert_not_called()

    async def test_null_max_reversals_falls_back_to_three(self):
        self.settings.inverse_bot_max_reversals_per_day = None
        row = self._persisted_row(reversals_day=datetime.now(UTC).date(), reversals_today=3)

        result = await self._evaluate(row=row)

        self.assertEqual(result, {"status": "daily_cap_reached"})

    # ── credentials ──

    async def test_missing_credentials_block_execution(self):
        result = await self._evaluate(row=self._persisted_row(), stored={})

        self.assertEqual(
            result,
            {
                "status": "error",
                "reason": "No trading credentials found. Re-login required.",
            },
        )
        self.assertEqual(self.row.status, "error")
        self.execute.assert_not_called()
        self.assertEqual(self.db.added, [])

    # ── execution outcomes ──

    async def test_successful_flip_mutates_the_row_and_writes_an_action(self):
        result = await self._evaluate(row=self._persisted_row())

        self.assertEqual(
            result,
            {
                "status": "success",
                "action_id": 4242,
                "sell_order_hash": "0xsell",
                "buy_order_hash": "0xbuy",
                "buy_notional": 49.25,
            },
        )
        self.assertEqual(self.row.token_id, ALT_TOKEN)
        self.assertEqual(self.row.outcome, "No")
        self.assertEqual(self.row.status, "active")
        self.assertTrue(self.row.enabled)
        self.assertIsNotNone(self.row.last_reversed_at)
        self.assertEqual(self.row.reversals_today, 1)
        self.assertEqual(self.row.persistence_count, 0)
        self.assertIsNone(self.row.last_error)
        self.assertEqual(inverse_bot_monitor._metrics["reversals_total"], 1)
        self.assertEqual(inverse_bot_monitor._metrics["evaluations_total"], 1)
        self.db.refresh.assert_called_once_with(self.db.added[0])

        action = self._action()
        self.assertEqual(action.inverse_bot_position_id, 11)
        self.assertEqual(action.user_id, 3)
        self.assertEqual(action.condition_id, CONDITION_ID)
        self.assertEqual(action.from_token_id, HELD_TOKEN)
        self.assertEqual(action.to_token_id, ALT_TOKEN)
        self.assertEqual(action.from_outcome, "Yes")
        self.assertEqual(action.to_outcome, "No")
        self.assertEqual(action.sell_order_hash, "0xsell")
        self.assertEqual(action.buy_order_hash, "0xbuy")
        self.assertEqual(action.sell_size, 100.0)
        self.assertEqual(action.buy_notional, 49.25)
        self.assertEqual(action.confidence, 88.0)
        self.assertEqual(action.recommendation, "reverse")
        self.assertEqual(action.status, "success")
        self.assertIsNone(action.error)
        self.assertIsNotNone(action.executed_at)

    async def test_execution_receives_the_resolved_flip_target(self):
        await self._evaluate(row=self._persisted_row())

        self.execute.assert_called_once_with(
            row=self.row,
            settings=self.settings,
            stored_creds={"private_key": "0xpk"},
            from_size=100.0,
            from_price=0.3,
            to_token_id=ALT_TOKEN,
            to_outcome="No",
            to_price=0.7,
            confidence=88.0,
            recommendation="reverse",
        )

    async def test_ai_chosen_token_overrides_the_best_alternative(self):
        market = {
            "tokens": [
                {"token_id": HELD_TOKEN, "outcome": "Yes", "price": 0.1},
                {"token_id": ALT_TOKEN, "outcome": "No", "price": 0.5},
                {"token_id": THIRD_TOKEN, "outcome": "Maybe", "price": 0.8},
            ]
        }
        self.ai.return_value = _ai(alt_token_id=THIRD_TOKEN, alt_outcome="Maybe")

        result = await self._evaluate(
            row=self._persisted_row(last_signal=f"reverse:{THIRD_TOKEN}"),
            market=market,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(self.row.token_id, THIRD_TOKEN)
        self.assertEqual(self.row.outcome, "Maybe")
        self.assertEqual(self._action().to_token_id, THIRD_TOKEN)

    async def test_sell_only_outcome_disables_the_row(self):
        self.execute.return_value = {
            "status": "sell_only",
            "error": "Buy notional below minimum ($0.4925) after sell.",
            "sell_order_hash": "0xsell",
            "buy_order_hash": None,
            "buy_notional": 0.4925,
        }

        result = await self._evaluate(row=self._persisted_row())

        self.assertEqual(result["status"], "sell_only")
        self.assertEqual(result["buy_notional"], 0.4925)
        self.assertEqual(self.row.status, "sell_only")
        self.assertFalse(self.row.enabled)
        self.assertEqual(self.row.last_error, "Buy notional below minimum ($0.4925) after sell.")
        self.assertEqual(self.row.reversals_today, 1)
        self.assertIsNotNone(self.row.last_reversed_at)
        self.assertEqual(self.row.persistence_count, 0)
        # The held token is deliberately *not* moved: only the sell happened.
        self.assertEqual(self.row.token_id, HELD_TOKEN)
        self.assertEqual(inverse_bot_monitor._metrics["sell_only_total"], 1)
        self.assertIsNotNone(self._action().executed_at)

    async def test_failed_execution_keeps_the_position_and_records_the_error(self):
        self.execute.return_value = {
            "status": "failed",
            "error": "SELL leg failed",
            "sell_order_hash": None,
            "buy_order_hash": None,
            "buy_notional": None,
        }

        result = await self._evaluate(row=self._persisted_row())

        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.row.status, "error")
        self.assertEqual(self.row.last_error, "SELL leg failed")
        self.assertTrue(self.row.enabled)
        self.assertEqual(self.row.token_id, HELD_TOKEN)
        self.assertIsNone(self.row.last_reversed_at)
        # The daily counter is left alone: the attempt never fired. The
        # persistence count stays at its incremented value, so the next
        # evaluation still qualifies immediately.
        self.assertEqual(self.row.reversals_today, 0)
        self.assertEqual(self.row.persistence_count, 2)
        self.assertEqual(inverse_bot_monitor._metrics["reversals_failed"], 1)

        action = self._action()
        self.assertEqual(action.status, "failed")
        self.assertEqual(action.error, "SELL leg failed")
        self.assertIsNone(action.executed_at)

    async def test_execution_without_a_status_key_is_reported_as_failed(self):
        self.execute.return_value = {"error": "weird"}

        result = await self._evaluate(row=self._persisted_row())

        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.row.status, "error")
        self.assertEqual(self.row.last_error, "weird")
        self.assertEqual(self._action().status, "failed")

    async def test_evaluation_clears_a_stale_error_and_stamps_the_timestamp(self):
        self.row.last_error = "old failure"
        self.row.market_title = None

        await self._evaluate()

        self.assertIsNone(self.row.last_error)
        self.assertIsNotNone(self.row.last_evaluated_at)


# ─────────────── per-user evaluation ───────────────


class EvaluateUserPositionsTests(_MonitorStateMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.isolate_monitor_state()
        self.creds = patch.object(
            inverse_bot_monitor,
            "load_wallet_credentials",
            return_value={"private_key": "0xpk", "clob_creds": {"api_key": "k"}},
        )
        self.load_creds = self.creds.start()
        self.addCleanup(self.creds.stop)
        self.service = MagicMock()
        self.service.get_positions = AsyncMock(return_value=[_positions()[HELD_TOKEN]])
        patcher = patch.object(
            inverse_bot_monitor, "get_polymarket_service", return_value=self.service
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.evaluate = patch.object(
            inverse_bot_monitor, "evaluate_inverse_position_row", new=AsyncMock(return_value={})
        ).start()
        self.addCleanup(patch.stopall)

    async def test_missing_user_returns_early(self):
        db = _session(first_results=[None, _settings()])

        self.assertIsNone(await inverse_bot_monitor._evaluate_user_positions(db, 3))
        self.load_creds.assert_not_called()

    async def test_missing_settings_returns_early(self):
        db = _session(first_results=[_user(), None])

        self.assertIsNone(await inverse_bot_monitor._evaluate_user_positions(db, 3))
        self.load_creds.assert_not_called()

    async def test_disabled_setting_returns_early(self):
        db = _session(first_results=[_user(), _settings(inverse_bot_enabled=False)])

        self.assertIsNone(await inverse_bot_monitor._evaluate_user_positions(db, 3))
        self.load_creds.assert_not_called()

    async def test_no_configured_positions_returns_early(self):
        db = _session(first_results=[_user(), _settings()], all_results=[])

        self.assertIsNone(await inverse_bot_monitor._evaluate_user_positions(db, 3))
        self.evaluate.assert_not_awaited()

    async def test_credential_store_failure_is_tolerated(self):
        self.load_creds.side_effect = CredentialStoreError("redis down")
        db = _session(first_results=[_user(), _settings()], all_results=[_row()])

        self.assertIsNone(await inverse_bot_monitor._evaluate_user_positions(db, 3))
        self.evaluate.assert_not_awaited()
        self.assertEqual(db.added, [])

    async def test_absent_credentials_flag_every_row(self):
        self.load_creds.return_value = None
        rows = [_row(id=1), _row(id=2, condition_id="cond-2")]
        db = _session(first_results=[_user(), _settings()], all_results=rows)

        self.assertIsNone(await inverse_bot_monitor._evaluate_user_positions(db, 3))

        for row in rows:
            self.assertEqual(row.status, "error")
            # Unlike a closed position, missing credentials do *not* disable
            # the row: it stays armed so a later cycle can retry once the user
            # has re-authenticated.
            self.assertTrue(row.enabled)
            self.assertEqual(row.last_error, "No trading credentials found. Re-login required.")
            self.assertIsNotNone(row.last_evaluated_at)
        self.assertGreaterEqual(db.commit.call_count, 1)
        self.evaluate.assert_not_awaited()

    async def test_positions_fetch_failure_is_tolerated(self):
        self.service.get_positions.side_effect = RuntimeError("502")
        db = _session(first_results=[_user(), _settings()], all_results=[_row()])

        self.assertIsNone(await inverse_bot_monitor._evaluate_user_positions(db, 3))
        self.evaluate.assert_not_awaited()

    async def test_each_row_is_evaluated_with_forced_disabled(self):
        row = _row()
        db = _session(first_results=[_user(), _settings()], all_results=[row])

        await inverse_bot_monitor._evaluate_user_positions(db, 3)

        self.service.get_positions.assert_awaited_once_with(
            "0xwallet", private_key="0xpk", clob_creds={"api_key": "k"}
        )
        self.evaluate.assert_awaited_once_with(
            db=db,
            user=self.evaluate.await_args.kwargs["user"],
            settings=self.evaluate.await_args.kwargs["settings"],
            row=row,
            stored_creds={"private_key": "0xpk", "clob_creds": {"api_key": "k"}},
            positions_by_token={
                HELD_TOKEN: {"asset_id": HELD_TOKEN, "size": 100.0, "title": "Will it rain?"}
            },
            force=False,
        )
        self.assertEqual(self.evaluate.await_args.kwargs["user"].id, 3)
        # The per-condition lock is always released.
        self.assertEqual(inverse_bot_monitor._inflight_keys, set())

    async def test_positions_without_asset_id_are_dropped(self):
        self.service.get_positions.return_value = [
            {"size": 5},
            {"asset_id": None, "size": 5},
            {"asset_id": ALT_TOKEN, "size": 7},
        ]
        db = _session(first_results=[_user(), _settings()], all_results=[_row()])

        await inverse_bot_monitor._evaluate_user_positions(db, 3)

        self.assertEqual(
            self.evaluate.await_args.kwargs["positions_by_token"],
            {ALT_TOKEN: {"asset_id": ALT_TOKEN, "size": 7}},
        )

    async def test_locked_condition_is_skipped(self):
        rows = [_row(id=1, condition_id="cond-locked"), _row(id=2, condition_id="cond-free")]
        db = _session(first_results=[_user(), _settings()], all_results=rows)
        inverse_bot_monitor._inflight_keys.add("3:cond-locked")

        await inverse_bot_monitor._evaluate_user_positions(db, 3)

        self.assertEqual([c.kwargs["row"].id for c in self.evaluate.await_args_list], [2])

    async def test_lock_is_released_when_evaluation_raises(self):
        db = _session(first_results=[_user(), _settings()], all_results=[_row()])
        self.evaluate.side_effect = RuntimeError("kaboom")

        with self.assertRaises(RuntimeError):
            await inverse_bot_monitor._evaluate_user_positions(db, 3)

        self.assertEqual(inverse_bot_monitor._inflight_keys, set())


# ─────────────── monitor cycle ───────────────


class RunMonitorCycleTests(_MonitorStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_evaluates_every_enabled_user_and_closes_the_session(self):
        db = _session(all_results=[(3,), (9,)])
        evaluate = AsyncMock(return_value=None)

        with (
            patch.object(inverse_bot_monitor, "SessionLocal", return_value=db),
            patch.object(inverse_bot_monitor, "_evaluate_user_positions", new=evaluate),
        ):
            await inverse_bot_monitor._run_monitor_cycle()

        self.assertEqual([call.args for call in evaluate.await_args_list], [(db, 3), (db, 9)])
        db.close.assert_called_once_with()

    async def test_session_is_closed_even_when_a_user_evaluation_raises(self):
        db = _session(all_results=[(3,)])
        evaluate = AsyncMock(side_effect=RuntimeError("boom"))

        with (
            patch.object(inverse_bot_monitor, "SessionLocal", return_value=db),
            patch.object(inverse_bot_monitor, "_evaluate_user_positions", new=evaluate),
            self.assertRaises(RuntimeError),
        ):
            await inverse_bot_monitor._run_monitor_cycle()

        db.close.assert_called_once_with()

    async def test_no_enabled_users_does_nothing(self):
        db = _session(all_results=[])
        evaluate = AsyncMock()

        with (
            patch.object(inverse_bot_monitor, "SessionLocal", return_value=db),
            patch.object(inverse_bot_monitor, "_evaluate_user_positions", new=evaluate),
        ):
            await inverse_bot_monitor._run_monitor_cycle()

        evaluate.assert_not_awaited()
        db.close.assert_called_once_with()


# ─────────────── manual trigger ───────────────


class ManualEvaluateTests(_MonitorStateMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.isolate_monitor_state()
        self.creds = patch.object(
            inverse_bot_monitor,
            "load_wallet_credentials",
            return_value={"private_key": "0xpk", "clob_creds": {"api_key": "k"}},
        )
        self.load_creds = self.creds.start()
        self.addCleanup(self.creds.stop)
        self.service = MagicMock()
        self.service.get_positions = AsyncMock(return_value=[_positions()[HELD_TOKEN], {"size": 1}])
        patcher = patch.object(
            inverse_bot_monitor, "get_polymarket_service", return_value=self.service
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.evaluate = patch.object(
            inverse_bot_monitor,
            "evaluate_inverse_position_row",
            new=AsyncMock(return_value={"status": "success", "action_id": 4242}),
        ).start()
        self.addCleanup(patch.stopall)

    async def test_unknown_user(self):
        db = _session(first_results=[None])
        result = await inverse_bot_monitor.manual_evaluate_inverse_position(db, 3, 11)
        self.assertEqual(result, {"success": False, "detail": "User not found"})

    async def test_unknown_settings(self):
        db = _session(first_results=[_user(), None])
        result = await inverse_bot_monitor.manual_evaluate_inverse_position(db, 3, 11)
        self.assertEqual(result, {"success": False, "detail": "User settings not found"})

    async def test_unknown_position(self):
        db = _session(first_results=[_user(), _settings(), None])
        result = await inverse_bot_monitor.manual_evaluate_inverse_position(db, 3, 11)
        self.assertEqual(result, {"success": False, "detail": "Inverse bot position not found"})

    async def test_credential_store_outage_is_reported(self):
        self.load_creds.side_effect = CredentialStoreError("redis down")
        db = _session(first_results=[_user(), _settings(), _row()])

        result = await inverse_bot_monitor.manual_evaluate_inverse_position(db, 3, 11)

        self.assertEqual(
            result, {"success": False, "detail": "Credential store unavailable: redis down"}
        )
        self.service.get_positions.assert_not_awaited()

    async def test_manual_trigger_forces_evaluation(self):
        row = _row()
        db = _session(first_results=[_user(), _settings(), row])

        result = await inverse_bot_monitor.manual_evaluate_inverse_position(db, 3, 11)

        self.assertEqual(
            result, {"success": True, "result": {"status": "success", "action_id": 4242}}
        )
        self.service.get_positions.assert_awaited_once_with(
            "0xwallet", private_key="0xpk", clob_creds={"api_key": "k"}
        )
        kwargs = self.evaluate.await_args.kwargs
        self.assertIs(kwargs["row"], row)
        self.assertIs(kwargs["user"].id, 3)
        self.assertIs(kwargs["settings"].user_id, 3)
        self.assertIs(kwargs["force"], True)
        self.assertEqual(
            kwargs["positions_by_token"],
            {HELD_TOKEN: {"asset_id": HELD_TOKEN, "size": 100.0, "title": "Will it rain?"}},
        )

    async def test_manual_trigger_without_credentials_still_evaluates(self):
        self.load_creds.return_value = None
        db = _session(first_results=[_user(), _settings(), _row()])

        result = await inverse_bot_monitor.manual_evaluate_inverse_position(db, 3, 11)

        self.assertIs(result["success"], True)
        self.service.get_positions.assert_not_awaited()
        self.assertEqual(self.evaluate.await_args.kwargs["positions_by_token"], {})
        self.assertIsNone(self.evaluate.await_args.kwargs["stored_creds"])


# ─────────────── loop lifecycle ───────────────


class MonitorLifecycleTests(_MonitorStateMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.isolate_monitor_state()

    async def test_start_is_idempotent_while_running(self):
        existing = await _pending_task()
        inverse_bot_monitor._monitor_task = existing
        self.addCleanup(existing.cancel)
        loop = AsyncMock()

        with patch.object(inverse_bot_monitor, "_monitor_loop", new=loop):
            await inverse_bot_monitor.start_inverse_bot_monitor()

        self.assertIs(inverse_bot_monitor._monitor_task, existing)
        loop.assert_not_awaited()

    async def test_start_replaces_a_finished_task(self):
        async def _noop():
            return None

        finished = asyncio.create_task(_noop())
        await asyncio.sleep(0)
        loop = AsyncMock()

        with patch.object(inverse_bot_monitor, "_monitor_loop", new=loop):
            await inverse_bot_monitor.start_inverse_bot_monitor()
            task = inverse_bot_monitor._monitor_task
            await task

        self.assertIsNot(task, finished)
        loop.assert_awaited_once_with()

    async def test_stop_cancels_and_clears_the_task(self):
        task = await _pending_task()
        inverse_bot_monitor._monitor_task = task

        await inverse_bot_monitor.stop_inverse_bot_monitor()

        self.assertTrue(task.cancelled())
        self.assertIsNone(inverse_bot_monitor._monitor_task)

    async def test_stop_when_idle_is_a_no_op(self):
        self.assertIsNone(inverse_bot_monitor._monitor_task)
        await inverse_bot_monitor.stop_inverse_bot_monitor()
        self.assertIsNone(inverse_bot_monitor._monitor_task)

    async def test_loop_records_the_tick_and_sleeps_the_interval(self):
        cycle = AsyncMock(return_value=None)
        delays: list[float] = []

        async def _fake_sleep(seconds):
            delays.append(seconds)
            raise asyncio.CancelledError

        with (
            patch.object(inverse_bot_monitor, "_run_monitor_cycle", new=cycle),
            patch.object(inverse_bot_monitor.asyncio, "sleep", new=_fake_sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await inverse_bot_monitor._monitor_loop()

        cycle.assert_awaited_once_with()
        self.assertEqual(delays, [inverse_bot_monitor.INVERSE_BOT_CHECK_INTERVAL])
        self.assertIsNotNone(inverse_bot_monitor._last_tick_at)
        self.assertEqual(
            inverse_bot_monitor.get_inverse_bot_metrics()["last_tick_at"],
            inverse_bot_monitor._last_tick_at.isoformat(),
        )

    async def test_cycle_cancellation_is_re_raised_not_swallowed(self):
        cycle = AsyncMock(side_effect=asyncio.CancelledError)
        logger = MagicMock()

        with (
            patch.object(inverse_bot_monitor, "_run_monitor_cycle", new=cycle),
            patch.object(inverse_bot_monitor, "logger", logger),
            self.assertRaises(asyncio.CancelledError),
        ):
            await inverse_bot_monitor._monitor_loop()

        # Cancellation is never logged as a cycle failure, and the loop does
        # not sleep again: it unwinds immediately.
        logger.error.assert_not_called()

    async def test_cycle_failure_is_logged_and_retried_after_the_interval(self):
        cycle = AsyncMock(side_effect=RuntimeError("cycle exploded"))
        delays: list[float] = []
        logger = MagicMock()

        async def _fake_sleep(seconds):
            delays.append(seconds)
            raise asyncio.CancelledError

        with (
            patch.object(inverse_bot_monitor, "_run_monitor_cycle", new=cycle),
            patch.object(inverse_bot_monitor.asyncio, "sleep", new=_fake_sleep),
            patch.object(inverse_bot_monitor, "logger", logger),
            self.assertRaises(asyncio.CancelledError),
        ):
            await inverse_bot_monitor._monitor_loop()

        self.assertEqual(delays, [inverse_bot_monitor.INVERSE_BOT_CHECK_INTERVAL])
        self.assertEqual(logger.error.call_args.args[0], "Inverse-bot monitor cycle failed: %s")
        self.assertIsInstance(logger.error.call_args.args[1], RuntimeError)
        self.assertIs(logger.error.call_args.kwargs["exc_info"], True)
        # The tick stamp is set before the cycle runs, so it survives a crash.
        self.assertIsNotNone(inverse_bot_monitor._last_tick_at)


async def _pending_task() -> asyncio.Task:
    task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    return task


if __name__ == "__main__":
    unittest.main()
