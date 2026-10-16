"""Tests for app/services/forecast_resolution.py (plan 01).

Gamma lookups are mocked at the ``httpx.AsyncClient`` boundary
(patch-constructor convention from
test_binance_signals_routes_coverage.py); resolution and Brier writes
run against real in-memory SQLite.
"""

import asyncio
import contextlib
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base)
from app.models.analytics.assessment import Assessment
from app.models.base import Base
from app.services.intelligence import forecast_resolution
from app.services.intelligence.forecast_resolution import (
    ResolutionRun,
    brier_score,
    fetch_resolutions,
    outcome_value,
    resolve_pending,
    winning_outcome,
)
from app.utils.time import utc_now

CID = "0x" + "ab" * 32
CID_2 = "0x" + "cd" * 32


def _gamma_market(cid, outcomes, prices):
    return {
        "conditionId": cid,
        "question": "Will it happen?",
        "outcomes": json.dumps(outcomes),
        "outcomePrices": json.dumps(prices),
        "closed": True,
        "umaResolutionStatus": "resolved",
    }


def _mock_httpx_client(responses=None, status_code=200, get_exc=None):
    """Build an ``httpx.AsyncClient`` double.

    ``responses`` is either a list of payloads consumed one per GET
    call, or a single payload reused for every call. Each payload is
    what ``response.json()`` returns (the Gamma markets array).
    """
    calls: list[tuple[str, dict]] = []
    queue = list(responses) if isinstance(responses, list) else None
    fixed = responses if queue is None else None

    async def _get(url, params=None, **kwargs):
        calls.append((url, params or {}))
        if get_exc is not None:
            raise get_exc
        response = MagicMock()
        response.status_code = status_code
        payload = queue.pop(0) if queue else fixed
        response.json = MagicMock(return_value=payload if payload is not None else [])
        return response

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=_get)
    client.calls = calls
    return client


def _make_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _verdict(db, cid=CID, probability=0.7, price=0.55, model="gpt-4", resolved=False, **extra):
    row = Assessment(
        trade_history_id=None,
        condition_id=cid,
        task="opportunity_scan",
        ai_score=70,
        reasoning="r",
        recommendation="buy",
        risk_level="unknown",
        confidence=80,
        ai_probability=probability,
        market_price_at_assessment=price,
        model_name=model,
        provider="openai",
        **extra,
    )
    if resolved:
        row.resolved_outcome = "YES"
        row.brier_score = brier_score(probability, "YES")
        row.resolved_at = utc_now()
    db.add(row)
    db.commit()
    return row


class WinningOutcomeTests(unittest.TestCase):
    def test_yes_wins(self):
        market = _gamma_market(CID, ["Yes", "No"], ["1", "0"])
        self.assertEqual(winning_outcome(market), "YES")

    def test_no_wins(self):
        market = _gamma_market(CID, ["Yes", "No"], ["0", "1"])
        self.assertEqual(winning_outcome(market), "NO")

    def test_multi_outcome_market_uses_winning_label(self):
        market = _gamma_market(CID, ["Alice", "Bob"], ["0", "1"])
        self.assertEqual(winning_outcome(market), "BOB")

    def test_no_outcome_reaching_threshold_returns_none(self):
        market = _gamma_market(CID, ["Yes", "No"], ["0", "0"])
        self.assertIsNone(winning_outcome(market))

    def test_malformed_payloads_return_none(self):
        self.assertIsNone(winning_outcome({}))
        self.assertIsNone(winning_outcome({"outcomes": "not json", "outcomePrices": "[]"}))
        self.assertIsNone(winning_outcome({"outcomes": '["Yes"]', "outcomePrices": '["1", "0"]'}))
        self.assertIsNone(
            winning_outcome({"outcomes": '["Yes", "No"]', "outcomePrices": '["x", "y"]'}),
        )

    def test_already_parsed_lists_are_accepted(self):
        market = {"outcomes": ["Yes", "No"], "outcomePrices": [0.99, 0.01]}
        self.assertEqual(winning_outcome(market), "YES")


class BrierScoreTests(unittest.TestCase):
    def test_yes_resolution(self):
        self.assertAlmostEqual(brier_score(0.8, "YES"), 0.04)

    def test_no_resolution(self):
        self.assertAlmostEqual(brier_score(0.8, "NO"), 0.64)

    def test_coin_flip_scores_baseline(self):
        self.assertAlmostEqual(brier_score(0.5, "NO"), 0.25)
        self.assertAlmostEqual(brier_score(0.5, "YES"), 0.25)

    def test_outcome_value_mapping(self):
        self.assertEqual(outcome_value("YES"), 1.0)
        self.assertEqual(outcome_value("yes"), 1.0)
        self.assertEqual(outcome_value("NO"), 0.0)
        self.assertEqual(outcome_value("ALICE"), 0.0)
        self.assertEqual(outcome_value(None), 0.0)


class FetchResolutionsTests(unittest.IsolatedAsyncioTestCase):
    def _patch_client(self, client):
        patcher = patch(
            "app.services.intelligence.forecast_resolution.httpx.AsyncClient",
            lambda **kwargs: client,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_empty_input_makes_no_request(self):
        client = _mock_httpx_client([])
        self._patch_client(client)
        self.assertEqual(await fetch_resolutions([]), {})
        self.assertEqual(await fetch_resolutions(None), {})
        client.get.assert_not_awaited()

    async def test_maps_winning_outcome_per_condition_id(self):
        yes_market = _gamma_market(CID, ["Yes", "No"], ["1", "0"])
        no_market = _gamma_market(CID_2, ["Yes", "No"], ["0", "1"])
        client = _mock_httpx_client([[yes_market], [no_market]])
        self._patch_client(client)
        result = await fetch_resolutions([CID, CID_2])
        self.assertEqual(result, {CID: "YES", CID_2: "NO"})

    async def test_uses_condition_ids_param_and_closed_filter(self):
        market = _gamma_market(CID, ["Yes", "No"], ["1", "0"])
        client = _mock_httpx_client([[market]])
        self._patch_client(client)
        await fetch_resolutions([CID])
        url, params = client.calls[0]
        self.assertTrue(url.endswith("/markets"))
        self.assertEqual(params, {"closed": "true", "condition_ids": CID})

    async def test_duplicate_ids_are_deduped(self):
        market = _gamma_market(CID, ["Yes", "No"], ["1", "0"])
        client = _mock_httpx_client([[market]])
        self._patch_client(client)
        result = await fetch_resolutions([CID, CID, CID])
        self.assertEqual(result, {CID: "YES"})
        self.assertEqual(client.get.await_count, 1)

    async def test_empty_result_is_skipped(self):
        client = _mock_httpx_client([[]])
        self._patch_client(client)
        self.assertEqual(await fetch_resolutions([CID]), {})

    async def test_non_200_is_skipped(self):
        client = _mock_httpx_client([[]], status_code=500)
        self._patch_client(client)
        self.assertEqual(await fetch_resolutions([CID]), {})

    async def test_network_error_is_skipped(self):
        client = _mock_httpx_client([None], get_exc=RuntimeError("network down"))
        self._patch_client(client)
        self.assertEqual(await fetch_resolutions([CID]), {})

    async def test_lookups_are_bounded_per_batch(self):
        """A slow Gamma must not stretch the pass into a serial hour.

        20 pending ids are walked in concurrent chunks of at most
        ``_RESOLUTION_CONCURRENCY`` (8), which bounds the wall clock of
        one resolution pass instead of letting it run request by request.
        """
        ids = [f"0x{i:064x}" for i in range(20)]
        in_flight = 0
        peak = 0

        async def _get(url, params=None, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            response = MagicMock()
            response.status_code = 200
            response.json = MagicMock(return_value=[])
            return response

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(side_effect=_get)
        self._patch_client(client)

        self.assertEqual(await fetch_resolutions(ids), {})
        self.assertEqual(client.get.await_count, 20)
        self.assertEqual(peak, forecast_resolution._RESOLUTION_CONCURRENCY)


class ResolvePendingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine, self.session_factory = _make_db()
        self.db = self.session_factory()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    async def test_scores_pending_rows_with_brier(self):
        row = _verdict(self.db, probability=0.8, price=0.55)
        with patch.object(
            forecast_resolution, "fetch_resolutions", AsyncMock(return_value={CID: "YES"})
        ):
            run = await resolve_pending(self.db)
        self.assertIsInstance(run, ResolutionRun)
        self.assertEqual(run.checked, 1)
        self.assertEqual(run.resolved, 1)
        self.assertAlmostEqual(run.mean_brier, 0.04)
        self.db.refresh(row)
        self.assertEqual(row.resolved_outcome, "YES")
        self.assertAlmostEqual(row.brier_score, 0.04)
        self.assertIsNotNone(row.resolved_at)

    async def test_no_resolution_leaves_row_untouched(self):
        row = _verdict(self.db)
        with patch.object(forecast_resolution, "fetch_resolutions", AsyncMock(return_value={})):
            run = await resolve_pending(self.db)
        self.assertEqual(run.resolved, 0)
        self.assertEqual(run.unresolved_condition_ids, [CID])
        self.db.refresh(row)
        self.assertIsNone(row.resolved_outcome)
        self.assertIsNone(row.brier_score)
        self.assertIsNone(row.resolved_at)

    async def test_second_run_is_a_noop(self):
        _verdict(self.db, probability=0.8)
        with patch.object(
            forecast_resolution, "fetch_resolutions", AsyncMock(return_value={CID: "YES"})
        ):
            first = await resolve_pending(self.db)
            second = await resolve_pending(self.db)
        self.assertEqual(first.resolved, 1)
        self.assertEqual(second.checked, 0)
        self.assertEqual(second.resolved, 0)

    async def test_rows_without_probability_or_condition_id_are_skipped(self):
        legacy = _verdict(self.db)
        legacy.ai_probability = None
        self.db.commit()
        no_cid = _verdict(self.db, cid=CID_2)
        no_cid.condition_id = None
        self.db.commit()
        fetch = AsyncMock(return_value={})
        with patch.object(forecast_resolution, "fetch_resolutions", fetch):
            run = await resolve_pending(self.db)
        self.assertEqual(run.checked, 0)
        fetch.assert_not_awaited()

    async def test_limit_caps_the_batch(self):
        for i in range(3):
            _verdict(self.db, cid=f"0x{i:064x}")
        fetch = AsyncMock(return_value={})
        with patch.object(forecast_resolution, "fetch_resolutions", fetch):
            run = await resolve_pending(self.db, limit=2)
        self.assertEqual(run.checked, 2)
        self.assertEqual(len(fetch.await_args.args[0]), 2)

    async def test_non_hex_condition_ids_never_enter_the_batch(self):
        """Market-id fallbacks can never resolve, so they must not queue.

        A verdict whose ``condition_id`` is the market id (markets that
        never exposed one) is unresolvable: leaving it in the oldest 200
        would block every newer verdict behind one doomed HTTP lookup.
        """
        _verdict(self.db, cid="market-123")
        _verdict(self.db, cid="market-456")
        _verdict(self.db, cid=CID)
        fetch = AsyncMock(return_value={})
        with patch.object(forecast_resolution, "fetch_resolutions", fetch):
            run = await resolve_pending(self.db)
        self.assertEqual(run.checked, 1)
        self.assertEqual(fetch.await_args.args[0], [CID])

    async def test_no_pending_rows_skips_fetch(self):
        fetch = AsyncMock(return_value={})
        with patch.object(forecast_resolution, "fetch_resolutions", fetch):
            run = await resolve_pending(self.db)
        self.assertEqual(run.checked, 0)
        fetch.assert_not_awaited()


class LoopLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.lock = patch.object(forecast_resolution, "acquire_scheduler_lock")
        self.release = patch.object(forecast_resolution, "release_scheduler_lock")
        self.heartbeat = patch.object(forecast_resolution, "scheduler_heartbeat")
        self.session_local = patch.object(forecast_resolution, "SessionLocal")
        self.mock_lock = self.lock.start()
        self.mock_release = self.release.start()
        self.mock_heartbeat = self.heartbeat.start()
        self.mock_session_local = self.session_local.start()
        for patcher in (self.lock, self.release, self.heartbeat, self.session_local):
            self.addCleanup(patcher.stop)
        self.mock_lock.return_value = True

        engine, session_factory = _make_db()
        self.addCleanup(engine.dispose)
        self.db = session_factory()
        self.addCleanup(self.db.close)
        self.mock_session_local.return_value = self.db

    async def test_loop_runs_one_pass_then_stops_on_cancel(self):
        resolve = AsyncMock(return_value=ResolutionRun(checked=2, resolved=1, brier_sum=0.04))
        with (
            patch.object(forecast_resolution, "resolve_pending", resolve),
            patch.object(forecast_resolution, "_resolution_interval", lambda: 3600),
        ):
            task = asyncio.create_task(forecast_resolution._resolution_loop())
            for _ in range(100):
                if resolve.await_count:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.mock_lock.assert_called_once_with("forecast_resolution")
        self.assertEqual(resolve.await_count, 1)
        self.mock_heartbeat.assert_called_with("forecast_resolution")
        self.mock_release.assert_called_once_with("forecast_resolution")

    async def test_loop_survives_a_failing_pass(self):
        resolve = AsyncMock(side_effect=RuntimeError("db down"))
        with (
            patch.object(forecast_resolution, "resolve_pending", resolve),
            patch.object(forecast_resolution, "_resolution_interval", lambda: 3600),
        ):
            task = asyncio.create_task(forecast_resolution._resolution_loop())
            for _ in range(100):
                if resolve.await_count:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.assertEqual(resolve.await_count, 1)
        self.mock_release.assert_called_once_with("forecast_resolution")

    async def test_start_skips_work_when_lock_held(self):
        self.mock_lock.return_value = False
        resolve = AsyncMock(return_value=ResolutionRun())
        with patch.object(forecast_resolution, "resolve_pending", resolve):
            await forecast_resolution.start_forecast_resolution()
            for _ in range(100):
                if forecast_resolution._resolution_task is not None and (
                    forecast_resolution._resolution_task.done()
                ):
                    break
                await asyncio.sleep(0.01)
            await forecast_resolution.stop_forecast_resolution()
        resolve.assert_not_awaited()
        self.mock_release.assert_not_called()
        self.assertIsNone(forecast_resolution._resolution_task)

    async def test_start_and_stop_manage_the_background_task(self):
        resolve = AsyncMock(return_value=ResolutionRun())
        with (
            patch.object(forecast_resolution, "resolve_pending", resolve),
            patch.object(forecast_resolution, "_resolution_interval", lambda: 3600),
        ):
            await forecast_resolution.start_forecast_resolution()
            task = forecast_resolution._resolution_task
            self.assertIsNotNone(task)
            await forecast_resolution.start_forecast_resolution()
            self.assertIs(forecast_resolution._resolution_task, task)
            await forecast_resolution.stop_forecast_resolution()
        self.assertTrue(task.done())
        self.assertIsNone(forecast_resolution._resolution_task)


if __name__ == "__main__":
    unittest.main()
