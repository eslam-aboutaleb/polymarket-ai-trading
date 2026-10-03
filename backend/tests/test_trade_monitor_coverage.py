"""Coverage tests for the trade monitor (``app.services.trade_monitor``).

All external I/O is mocked: the DB session factory, the CLOB HTTP
API (httpx), the WebSocket client, ``asyncio.to_thread`` (used for
blocking CLOB calls), the credential store, the alert dispatcher,
the scheduler lock, the e-mail sender and the gRPC analysis client.

Covered:

* ``refresh_watched_wallets`` -- union of copy + notification watch
  lists, lower-cased, with empty rows dropped.
* ``_poll_trader_trades`` -- shared client vs. throw-away client,
  list / ``trades`` / ``data`` response shapes, non-200 and errors.
* ``seed_wallet_cursor`` -- early return when seeded, cursor seeding
  from the newest trade, empty / failed responses.
* ``_detect_new_trades`` -- empty input, first-observation seeding,
  trades without ids.
* ``_trade_dedup_key`` -- id / trade_id preference, condition_id and
  asset_id fallbacks, timestamp fallback, unidentifiable events.
* ``_get_processed_ids_cache`` / ``_shared_already_processed`` /
  ``_shared_mark_processed`` / ``_already_processed`` -- lazy cache
  creation, shared hit/miss/error paths, LRU eviction.
* ``_ws_monitor`` -- poll cycle runs, WS timeout propagation, WS
  failure counting, idle 60s sleep.
* ``_ws_session`` -- cursor seeding, per-wallet subscribe, JSON
  decode errors, handler exceptions.
* ``_handle_ws_event`` -- event-type filter, pending-fill marking
  with parsed fill fields, watched-wallet filter.
* ``_event_order_hash`` -- field precedence.
* ``_mark_pending_trades_filled`` -- empty hash, no pending rows,
  fill field updates, slippage / latency derivation, defaults.
* ``_http_poll_cycle`` / ``_poll_and_process`` -- empty watch list,
  gather with errors, per-wallet processing.
* ``_process_new_trade`` -- dedup, missing market / zero amount,
  notional derivation, opened/closed notification dispatch, copy
  triggers per follower, per-follower error isolation, pipeline
  rollback.
* ``_trigger_copy_trade`` -- no settings, AI approval off, approved,
  unavailable assessment (fail closed), evaluation exception.
* ``_run_ai_evaluation`` -- LLM-chain vs. CLI-agent backend, winner
  stats, success, evaluation failure, client construction failure.
* ``_update_trader_position_state`` -- empty token, new BUY (opened),
  SELL to zero (closed), no transition, epsilon collapse.
* ``_smtp_ready`` / ``_create_follow_notifications`` -- every email
  skip reason, sent, failed, both-disabled follower, no followers.
* ``_emit_alert`` -- no dispatcher, dispatch, dispatch failure.
* ``_gtc_ttl_seconds`` / ``_acquire_job_lock`` / ``_job_heartbeat``.
* ``_fill_reconciliation_loop`` / ``_gtc_reconciliation_loop`` --
  lock contention, heartbeat, reconcile, exception isolation,
  cancellation propagation, GTC interval gating.
* ``_reconcile_pending_fills`` / ``_reconcile_fills_for_user`` --
  empty pending, per-user grouping, missing user, credential-store
  failure, missing credentials, lookup failure, no fills, fill
  marking.
* ``_resolve_proxy_address`` -- success and failure paths.
* ``_reconcile_orphaned_gtc_orders`` / ``_reconcile_gtc_for_user``
  -- empty stale set, per-user grouping, missing user, credential
  failures, open-order lookup failure, resting-order cancellation
  (success + failure), filled orphan, dropped orphan, trade-lookup
  failure, empty open-order book.
* ``_ensure_market`` -- existing, create, question/title fallbacks,
  rollback on failure.
* ``_to_float`` / ``_parse_timestamp`` -- all branches.
* ``start_trade_monitor`` / ``stop_trade_monitor`` -- task creation,
  HTTP client lifecycle, task cancellation.
* ``add_watched_wallet`` / ``remove_watched_wallet``.
"""

import asyncio
import builtins
import contextlib
import json
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.trade_monitor as trade_monitor
from app.models.followed_trader import FollowedTrader
from app.models.market import Market
from app.models.notification_feed_event import NotificationFeedEvent
from app.models.notification_followed_trader import NotificationFollowedTrader
from app.models.trader_position_state import TraderPositionState
from app.models.user import User
from app.models.user_settings import UserSettings
from app.models.user_trade import UserTrade
from app.security.credential_store import CredentialStoreError
from app.services.execution_analytics import (
    compute_latency_ms,
    compute_slippage_bps,
)
from app.utils.time import utc_now

WALLET = "0x" + "ab" * 20
WALLET_UPPER = WALLET.upper()


class _BreakLoop(Exception):
    """Sentinel that breaks otherwise-infinite monitor loops."""


class FakeQuery:
    def __init__(self, first=None, all_results=None, update_count=0, first_results=None):
        self._first = first
        self._first_results = list(first_results) if first_results else None
        self._all = list(all_results or [])
        self._update_count = update_count

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def distinct(self):
        return self

    def first(self):
        if self._first_results:
            return self._first_results.pop(0)
        return self._first

    def all(self):
        return list(self._all)

    def update(self, values):
        return self._update_count


class FakeSession:
    def __init__(self):
        self._registry = {}
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.flushed = 0
        self.refreshed = []
        self.closed = False
        self.commit_error = None
        self._next_id = 1

    def register(self, model, first=None, all_results=None, update_count=0, first_results=None):
        query = FakeQuery(
            first=first,
            all_results=all_results,
            update_count=update_count,
            first_results=first_results,
        )
        self._registry[model] = query
        return query

    def query(self, *models):
        return self._registry.setdefault(models[0], FakeQuery())

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1
            if getattr(obj, "created_at", None) is None:
                obj.created_at = utc_now()
            if getattr(obj, "updated_at", None) is None:
                obj.updated_at = utc_now()

    def rollback(self):
        self.rollbacks += 1

    def flush(self):
        self.flushed += 1

    def close(self):
        self.closed = True

    def refresh(self, obj):
        self.refreshed.append(obj)


class _Rows:
    """Query result double for ``refresh_watched_wallets``."""

    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def distinct(self):
        return self

    def all(self):
        return self._rows


class _FakeWs:
    """Async-context-manager WebSocket double."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, message):
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration


class _MonitorTestCase(unittest.IsolatedAsyncioTestCase):
    """Reset the module-level monitor state around each test."""

    def setUp(self):
        self._saved = (
            trade_monitor._watched_wallets,
            trade_monitor._last_seen_trades,
            trade_monitor._processed_trade_ids,
            trade_monitor._processed_ids_cache,
        )
        trade_monitor._watched_wallets = set()
        trade_monitor._last_seen_trades = {}
        trade_monitor._processed_trade_ids = {}
        trade_monitor._processed_ids_cache = None

    def tearDown(self):
        (
            trade_monitor._watched_wallets,
            trade_monitor._last_seen_trades,
            trade_monitor._processed_trade_ids,
            trade_monitor._processed_ids_cache,
        ) = self._saved


# ────────────── Wallet watch list ──────────────


class RefreshWatchedWalletsTests(unittest.TestCase):
    def test_refresh_builds_union_of_watch_lists(self):
        db = MagicMock()
        db.query.side_effect = [
            _Rows([(WALLET_UPPER,), ("0xbbb",), (None,)]),
            _Rows([("0xCCC",)]),
        ]
        trade_monitor.refresh_watched_wallets(db)

        self.assertEqual(
            trade_monitor._watched_wallets,
            {WALLET, "0xbbb", "0xccc"},
        )

    def test_refresh_with_no_rows(self):
        db = MagicMock()
        db.query.side_effect = [_Rows([]), _Rows([])]
        trade_monitor.refresh_watched_wallets(db)

        self.assertEqual(trade_monitor._watched_wallets, set())


# ────────────── HTTP polling ──────────────


def _response(status_code=200, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    return resp


class PollTraderTradesTests(_MonitorTestCase):
    async def test_poll_uses_shared_client(self):
        resp = _response(payload=[{"id": "t1"}])
        client = MagicMock()
        client.get = AsyncMock(return_value=resp)
        trade_monitor._poll_http_client = client

        result = await trade_monitor._poll_trader_trades(WALLET)

        self.assertEqual(result, [{"id": "t1"}])
        client.get.assert_awaited_once()
        self.assertEqual(
            client.get.call_args.kwargs["params"],
            {"user": WALLET, "limit": 20},
        )

    async def test_poll_uses_temp_client_when_none(self):
        trade_monitor._poll_http_client = None
        resp = _response(payload=[{"id": "t1"}])
        temp = MagicMock()
        temp.get = AsyncMock(return_value=resp)
        temp.__aenter__ = AsyncMock(return_value=temp)
        temp.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=temp) as factory:
            result = await trade_monitor._poll_trader_trades(WALLET)

        self.assertEqual(result, [{"id": "t1"}])
        factory.assert_called_once()
        temp.get.assert_awaited_once()

    async def test_poll_unwraps_trades_key(self):
        trade_monitor._poll_http_client = None
        resp = _response(payload={"trades": [{"id": "t1"}]})
        temp = MagicMock()
        temp.get = AsyncMock(return_value=resp)
        temp.__aenter__ = AsyncMock(return_value=temp)
        temp.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=temp):
            result = await trade_monitor._poll_trader_trades(WALLET)

        self.assertEqual(result, [{"id": "t1"}])

    async def test_poll_unwraps_data_key(self):
        trade_monitor._poll_http_client = None
        resp = _response(payload={"data": [{"id": "t1"}]})
        temp = MagicMock()
        temp.get = AsyncMock(return_value=resp)
        temp.__aenter__ = AsyncMock(return_value=temp)
        temp.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=temp):
            result = await trade_monitor._poll_trader_trades(WALLET)

        self.assertEqual(result, [{"id": "t1"}])

    async def test_poll_empty_dict_returns_empty_list(self):
        trade_monitor._poll_http_client = None
        resp = _response(payload={})
        temp = MagicMock()
        temp.get = AsyncMock(return_value=resp)
        temp.__aenter__ = AsyncMock(return_value=temp)
        temp.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=temp):
            result = await trade_monitor._poll_trader_trades(WALLET)

        self.assertEqual(result, [])

    async def test_poll_non_200_returns_empty_list(self):
        trade_monitor._poll_http_client = None
        resp = _response(status_code=500, payload=[{"id": "t1"}])
        temp = MagicMock()
        temp.get = AsyncMock(return_value=resp)
        temp.__aenter__ = AsyncMock(return_value=temp)
        temp.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=temp):
            result = await trade_monitor._poll_trader_trades(WALLET)

        self.assertEqual(result, [])

    async def test_poll_request_failure_returns_empty_list(self):
        client = MagicMock()
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        trade_monitor._poll_http_client = client

        result = await trade_monitor._poll_trader_trades(WALLET)

        self.assertEqual(result, [])


class SeedWalletCursorTests(_MonitorTestCase):
    async def test_seed_skips_when_already_seeded(self):
        trade_monitor._last_seen_trades[WALLET] = "t9"
        client = MagicMock()
        client.get = AsyncMock()
        trade_monitor._poll_http_client = client

        await trade_monitor.seed_wallet_cursor(WALLET)

        client.get.assert_not_awaited()

    async def test_seed_records_newest_trade_id(self):
        resp = _response(payload=[{"id": "t9"}, {"id": "t8"}])
        client = MagicMock()
        client.get = AsyncMock(return_value=resp)
        trade_monitor._poll_http_client = client

        await trade_monitor.seed_wallet_cursor(WALLET_UPPER)

        self.assertEqual(trade_monitor._last_seen_trades[WALLET], "t9")
        self.assertEqual(
            client.get.call_args.kwargs["params"],
            {"user": WALLET, "limit": 1},
        )

    async def test_seed_with_temp_client(self):
        trade_monitor._poll_http_client = None
        resp = _response(payload=[{"id": "t9"}])
        temp = MagicMock()
        temp.get = AsyncMock(return_value=resp)
        temp.__aenter__ = AsyncMock(return_value=temp)
        temp.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=temp):
            await trade_monitor.seed_wallet_cursor(WALLET)

        self.assertEqual(trade_monitor._last_seen_trades[WALLET], "t9")

    async def test_seed_with_empty_trades(self):
        resp = _response(payload=[])
        client = MagicMock()
        client.get = AsyncMock(return_value=resp)
        trade_monitor._poll_http_client = client

        await trade_monitor.seed_wallet_cursor(WALLET)

        self.assertNotIn(WALLET, trade_monitor._last_seen_trades)

    async def test_seed_with_non_200(self):
        resp = _response(status_code=500, payload=[{"id": "t9"}])
        client = MagicMock()
        client.get = AsyncMock(return_value=resp)
        trade_monitor._poll_http_client = client

        await trade_monitor.seed_wallet_cursor(WALLET)

        self.assertNotIn(WALLET, trade_monitor._last_seen_trades)

    async def test_seed_failure_is_swallowed(self):
        client = MagicMock()
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        trade_monitor._poll_http_client = client

        await trade_monitor.seed_wallet_cursor(WALLET)  # no raise

        self.assertNotIn(WALLET, trade_monitor._last_seen_trades)


def _mock_session(followers=None):
    """MagicMock session that assigns primary keys on commit.

    A real DB session populates ``id`` on flush/commit; without
    this the ``logger.info("Recorded trade_history #%d …", th.id)``
    call in ``_process_new_trade`` raises a TypeError under the
    app's logging config (which pytest's log capture re-raises),
    aborting the pipeline.
    """
    session = MagicMock()
    session.query.return_value.filter.return_value.all.return_value = (
        list(followers) if followers is not None else []
    )
    added = []

    def _add(obj):
        added.append(obj)

    def _commit():
        for obj in added:
            if getattr(obj, "id", None) is None:
                obj.id = 1

    session.add.side_effect = _add
    session.commit.side_effect = _commit
    return session


class DetectNewTradesTests(_MonitorTestCase):
    async def test_empty_trades(self):
        self.assertEqual(await trade_monitor._detect_new_trades(WALLET, []), [])

    async def test_first_observation_seeds_and_skips(self):
        trades = [{"id": "t3"}, {"id": "t2"}]

        result = await trade_monitor._detect_new_trades(WALLET, trades)

        self.assertEqual(result, [])
        self.assertEqual(trade_monitor._last_seen_trades[WALLET], "t3")

    async def test_only_newer_than_cursor(self):
        await trade_monitor._detect_new_trades(WALLET, [{"id": "t2"}, {"id": "t1"}])
        newer = [{"id": "t4"}, {"id": "t2"}, {"id": "t1"}]

        result = await trade_monitor._detect_new_trades(WALLET, newer)

        self.assertEqual(result, [{"id": "t4"}])
        self.assertEqual(trade_monitor._last_seen_trades[WALLET], "t4")

    async def test_trade_without_id_is_appended(self):
        trade_monitor._last_seen_trades[WALLET] = "last"

        result = await trade_monitor._detect_new_trades(WALLET, [{"size": 1}])

        self.assertEqual(result, [{"size": 1}])
        self.assertEqual(trade_monitor._last_seen_trades[WALLET], "")


class TradeDedupKeyTests(unittest.TestCase):
    def test_prefers_id(self):
        self.assertEqual(
            trade_monitor._trade_dedup_key({"id": "a", "trade_id": "b"}),
            "a",
        )

    def test_falls_back_to_trade_id(self):
        self.assertEqual(trade_monitor._trade_dedup_key({"trade_id": "b"}), "b")

    def test_fingerprints_with_condition_id(self):
        key = trade_monitor._trade_dedup_key(
            {"condition_id": "c", "side": "BUY", "size": 5, "price": 0.5}
        )
        self.assertEqual(key, "fp:c:BUY:5:0.5:")

    def test_fingerprints_with_asset_id(self):
        key = trade_monitor._trade_dedup_key(
            {"asset_id": "a", "side": "SELL", "size": 1, "price": 0.6}
        )
        self.assertEqual(key, "fp:a:SELL:1:0.6:")

    def test_fingerprint_uses_timestamp_fallback(self):
        key = trade_monitor._trade_dedup_key(
            {
                "market": "m",
                "side": "BUY",
                "size": 5,
                "price": 0.5,
                "timestamp": "ts",
            }
        )
        self.assertEqual(key, "fp:m:BUY:5:0.5:ts")

    def test_unidentifiable_returns_none(self):
        self.assertIsNone(trade_monitor._trade_dedup_key({}))
        self.assertIsNone(trade_monitor._trade_dedup_key({"market": "m"}))
        self.assertIsNone(trade_monitor._trade_dedup_key({"market": "m", "side": "BUY", "size": 5}))


class ProcessedIdsCacheTests(_MonitorTestCase):
    def test_get_processed_ids_cache_is_lazy_and_cached(self):
        fake = MagicMock()
        with patch("app.utils.cache.get_cache", return_value=fake) as get_cache:
            self.assertIs(trade_monitor._get_processed_ids_cache(), fake)
            self.assertIs(trade_monitor._get_processed_ids_cache(), fake)
        get_cache.assert_called_once_with("trade_dedup")

    def test_shared_already_processed_true(self):
        cache = MagicMock()
        cache.get.return_value = True
        trade_monitor._processed_ids_cache = cache

        self.assertTrue(trade_monitor._shared_already_processed(WALLET, "k"))
        cache.get.assert_called_once_with(f"{WALLET}:k")

    def test_shared_already_processed_false(self):
        cache = MagicMock()
        cache.get.return_value = None
        trade_monitor._processed_ids_cache = cache

        self.assertFalse(trade_monitor._shared_already_processed(WALLET, "k"))

    def test_shared_already_processed_error_returns_false(self):
        cache = MagicMock()
        cache.get.side_effect = RuntimeError("boom")
        trade_monitor._processed_ids_cache = cache

        self.assertFalse(trade_monitor._shared_already_processed(WALLET, "k"))

    def test_shared_mark_processed(self):
        cache = MagicMock()
        trade_monitor._processed_ids_cache = cache

        trade_monitor._shared_mark_processed(WALLET, "k")

        cache.set.assert_called_once_with(
            f"{WALLET}:k", True, ttl_seconds=trade_monitor._PROCESSED_IDS_TTL_SECONDS
        )

    def test_shared_mark_processed_error_is_swallowed(self):
        cache = MagicMock()
        cache.set.side_effect = RuntimeError("boom")
        trade_monitor._processed_ids_cache = cache

        trade_monitor._shared_mark_processed(WALLET, "k")  # no raise

    def test_already_processed_shared_hit(self):
        with (
            patch.object(trade_monitor, "_shared_already_processed", return_value=True),
            patch.object(trade_monitor, "_shared_mark_processed") as mark,
        ):
            self.assertTrue(trade_monitor._already_processed(WALLET, "k"))

        mark.assert_not_called()
        self.assertIn("k", trade_monitor._processed_trade_ids[WALLET])

    def test_already_processed_local_miss_marks_and_returns_false(self):
        with (
            patch.object(trade_monitor, "_shared_already_processed", return_value=False),
            patch.object(trade_monitor, "_shared_mark_processed") as mark,
        ):
            self.assertFalse(trade_monitor._already_processed(WALLET, "k"))

        mark.assert_called_once_with(WALLET, "k")
        self.assertIn("k", trade_monitor._processed_trade_ids[WALLET])

    def test_already_processed_local_hit_moves_to_end(self):
        trade_monitor._already_processed(WALLET, "k1")
        trade_monitor._already_processed(WALLET, "k2")

        self.assertTrue(trade_monitor._already_processed(WALLET, "k1"))

        self.assertEqual(list(trade_monitor._processed_trade_ids[WALLET]), ["k2", "k1"])

    def test_already_processed_evicts_oldest_past_limit(self):
        with (
            patch.object(trade_monitor, "_shared_already_processed", return_value=False),
            patch.object(trade_monitor, "_shared_mark_processed"),
        ):
            for i in range(trade_monitor._PROCESSED_IDS_MAX + 1):
                trade_monitor._already_processed(WALLET, f"key-{i}")

        seen = trade_monitor._processed_trade_ids[WALLET]

        self.assertNotIn("key-0", seen)
        self.assertIn(f"key-{trade_monitor._PROCESSED_IDS_MAX}", seen)
        self.assertEqual(len(seen), trade_monitor._PROCESSED_IDS_MAX)

    def test_already_processed_shared_hit_evicts_oldest(self):
        with patch.object(trade_monitor, "_shared_already_processed", return_value=True):
            for i in range(trade_monitor._PROCESSED_IDS_MAX + 1):
                trade_monitor._already_processed(WALLET, f"key-{i}")

        seen = trade_monitor._processed_trade_ids[WALLET]

        self.assertNotIn("key-0", seen)
        self.assertEqual(len(seen), trade_monitor._PROCESSED_IDS_MAX)


# ────────────── Primary monitor loop ──────────────


class WsMonitorTests(_MonitorTestCase):
    async def test_poll_cycle_runs_then_ws_timeout_propagates(self):
        trade_monitor._watched_wallets = {WALLET}
        poll = AsyncMock()
        ws = AsyncMock(side_effect=TimeoutError())
        with (
            patch.object(trade_monitor, "_http_poll_cycle", poll),
            patch.object(trade_monitor, "_ws_session", ws),
            self.assertRaises(TimeoutError),
        ):
            await trade_monitor._ws_monitor()

        poll.assert_awaited_once()
        ws.assert_awaited_once()

    async def test_ws_failures_are_counted_and_logged(self):
        trade_monitor._watched_wallets = {WALLET}
        poll = AsyncMock()
        ws = AsyncMock(side_effect=RuntimeError("boom"))
        sleeps = []

        async def _sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                raise _BreakLoop()

        with (
            patch.object(trade_monitor, "_http_poll_cycle", poll),
            patch.object(trade_monitor, "_ws_session", ws),
            patch("asyncio.sleep", _sleep),
            self.assertRaises(_BreakLoop),
        ):
            await trade_monitor._ws_monitor()

        self.assertEqual(poll.await_count, 2)
        self.assertEqual(ws.await_count, 2)
        self.assertEqual(sleeps, [30, 30])

    async def test_idle_loop_sleeps_60s_without_polling(self):
        trade_monitor._watched_wallets = set()
        poll = AsyncMock()
        ws = AsyncMock()

        async def _sleep(seconds):
            raise _BreakLoop()

        with (
            patch.object(trade_monitor, "_http_poll_cycle", poll),
            patch.object(trade_monitor, "_ws_session", ws),
            patch("asyncio.sleep", _sleep),
            self.assertRaises(_BreakLoop),
        ):
            await trade_monitor._ws_monitor()

        poll.assert_not_awaited()
        ws.assert_not_awaited()

    async def test_ws_monitor_without_websockets_package(self):
        trade_monitor._watched_wallets = set()
        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "websockets":
                raise ImportError("websockets not installed")
            return real_import(name, *args, **kwargs)

        poll = AsyncMock()

        async def _sleep(seconds):
            raise _BreakLoop()

        with (
            patch("builtins.__import__", side_effect=_fake_import),
            patch.object(trade_monitor, "_http_poll_cycle", poll),
            patch("asyncio.sleep", _sleep),
            self.assertRaises(_BreakLoop),
        ):
            await trade_monitor._ws_monitor()

        poll.assert_not_awaited()

    async def test_ws_success_resets_failure_count(self):
        trade_monitor._watched_wallets = {WALLET}
        poll = AsyncMock()
        ws = AsyncMock(side_effect=[RuntimeError("boom"), None, None])
        sleeps = []

        async def _sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 3:
                raise _BreakLoop()

        with (
            patch.object(trade_monitor, "_http_poll_cycle", poll),
            patch.object(trade_monitor, "_ws_session", ws),
            patch("asyncio.sleep", _sleep),
            self.assertRaises(_BreakLoop),
        ):
            await trade_monitor._ws_monitor()

        # Failure, then two successful sessions (failure counter reset).
        self.assertEqual(poll.await_count, 3)
        self.assertEqual(ws.await_count, 3)

    async def test_ws_gives_up_after_max_consecutive_failures(self):
        trade_monitor._watched_wallets = {WALLET}
        poll = AsyncMock()
        ws = AsyncMock(side_effect=RuntimeError("boom"))
        sleeps = []

        async def _sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 6:
                raise _BreakLoop()

        with (
            patch.object(trade_monitor, "_http_poll_cycle", poll),
            patch.object(trade_monitor, "_ws_session", ws),
            patch("asyncio.sleep", _sleep),
            self.assertRaises(_BreakLoop),
        ):
            await trade_monitor._ws_monitor()

        # After MAX_WS_FAILS (5) consecutive failures the WS is
        # abandoned and only the HTTP poll keeps running.
        self.assertEqual(ws.await_count, 5)
        self.assertEqual(poll.await_count, 6)


class WsSessionTests(_MonitorTestCase):
    async def test_session_seeds_cursors_and_subscribes(self):
        trade_monitor._watched_wallets = {WALLET, "0xbbb"}
        messages = [
            json.dumps({"type": "trade", "id": "1"}),
            "not-json",
            json.dumps({"type": "fill", "id": "2"}),
        ]
        ws = _FakeWs(messages)
        handler = AsyncMock()
        seed = AsyncMock()
        with (
            patch("websockets.connect", return_value=ws),
            patch.object(trade_monitor, "seed_wallet_cursor", seed),
            patch.object(trade_monitor, "_handle_ws_event", handler),
        ):
            await trade_monitor._ws_session()

        self.assertEqual(len(ws.sent), 2)
        for message in ws.sent:
            payload = json.loads(message)
            self.assertEqual(payload["type"], "subscribe")
            self.assertEqual(payload["channel"], "user")
        self.assertEqual(seed.await_count, 2)
        self.assertEqual(handler.await_count, 2)

    async def test_session_swallows_handler_exceptions(self):
        trade_monitor._watched_wallets = {WALLET}
        messages = [json.dumps({"type": "trade", "id": "1"})]
        ws = _FakeWs(messages)
        handler = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch("websockets.connect", return_value=ws),
            patch.object(trade_monitor, "seed_wallet_cursor", AsyncMock()),
            patch.object(trade_monitor, "_handle_ws_event", handler),
        ):
            await trade_monitor._ws_session()  # no raise

        handler.assert_awaited_once()


class HandleWsEventTests(_MonitorTestCase):
    async def test_non_trade_event_is_ignored(self):
        session = MagicMock()
        with (
            patch.object(trade_monitor, "SessionLocal", return_value=session),
            patch.object(trade_monitor, "_mark_pending_trades_filled") as marked,
        ):
            await trade_monitor._handle_ws_event({"type": "subscribe"})

        marked.assert_not_called()
        session.close.assert_not_called()

    async def test_order_hash_marks_pending_trades_filled(self):
        session = MagicMock()
        marked = MagicMock()
        process = AsyncMock()
        data = {
            "type": "trade",
            "order_hash": "0xhash",
            "match_time": 1700000000,
            "price": "0.5",
            "size": "2",
            "fee": "0.01",
            "user": "0xunknown",
        }
        with (
            patch.object(trade_monitor, "SessionLocal", return_value=session),
            patch.object(trade_monitor, "_mark_pending_trades_filled", marked),
            patch.object(trade_monitor, "_process_new_trade", process),
        ):
            await trade_monitor._handle_ws_event(data)

        marked.assert_called_once()
        self.assertEqual(marked.call_args.args, (session, "0xhash"))
        kwargs = marked.call_args.kwargs
        self.assertEqual(kwargs["filled_at"], datetime.fromtimestamp(1700000000, tz=UTC))
        self.assertEqual(kwargs["fill_price"], 0.5)
        self.assertEqual(kwargs["fill_size"], 2.0)
        self.assertEqual(kwargs["fill_fee"], 0.01)
        session.close.assert_called_once()
        # Unknown wallet: no trade processing.
        process.assert_not_awaited()

    async def test_watched_wallet_triggers_processing(self):
        trade_monitor._watched_wallets = {WALLET}
        session = MagicMock()
        marked = MagicMock()
        process = AsyncMock()
        data = {"type": "trade", "user": WALLET_UPPER, "id": "t1"}
        with (
            patch.object(trade_monitor, "SessionLocal", return_value=session),
            patch.object(trade_monitor, "_mark_pending_trades_filled", marked),
            patch.object(trade_monitor, "_process_new_trade", process),
        ):
            await trade_monitor._handle_ws_event(data)

        # No order hash on the event → no fill marking, no DB session.
        marked.assert_not_called()
        process.assert_awaited_once_with(WALLET, data)

    async def test_taker_and_maker_wallet_fallbacks(self):
        trade_monitor._watched_wallets = {WALLET}
        session = MagicMock()
        process = AsyncMock()
        data = {"type": "trade", "taker": WALLET_UPPER, "id": "t1"}
        with (
            patch.object(trade_monitor, "SessionLocal", return_value=session),
            patch.object(trade_monitor, "_mark_pending_trades_filled", MagicMock()),
            patch.object(trade_monitor, "_process_new_trade", process),
        ):
            await trade_monitor._handle_ws_event(data)

        process.assert_awaited_once_with(WALLET, data)


class EventOrderHashTests(unittest.TestCase):
    def test_field_precedence(self):
        self.assertEqual(
            trade_monitor._event_order_hash(
                {"order_hash": "a", "order_id": "b", "hash": "c", "orderID": "d"}
            ),
            "a",
        )
        self.assertEqual(trade_monitor._event_order_hash({"order_id": "b"}), "b")
        self.assertEqual(trade_monitor._event_order_hash({"hash": "c"}), "c")
        self.assertEqual(trade_monitor._event_order_hash({"orderID": "d"}), "d")
        self.assertIsNone(trade_monitor._event_order_hash({}))
        self.assertIsNone(trade_monitor._event_order_hash({"order_hash": ""}))


class MarkPendingTradesFilledTests(_MonitorTestCase):
    def _row(self, **overrides):
        base = {
            "status": "pending",
            "executed_at": None,
            "filled_price": None,
            "filled_size": None,
            "fee_paid": None,
            "slippage_bps": None,
            "latency_ms": None,
            "expected_price": None,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_empty_order_hash_returns_zero(self):
        db = FakeSession()
        self.assertEqual(trade_monitor._mark_pending_trades_filled(db, ""), 0)
        self.assertEqual(db.commits, 0)

    def test_no_pending_rows_returns_zero(self):
        db = FakeSession()
        db.register(UserTrade, all_results=[])
        self.assertEqual(trade_monitor._mark_pending_trades_filled(db, "0xh"), 0)
        self.assertEqual(db.commits, 0)

    def test_marks_rows_with_defaults(self):
        rows = [self._row(), self._row()]
        db = FakeSession()
        db.register(UserTrade, all_results=rows)
        filled_at = datetime(2026, 1, 2, tzinfo=UTC)

        count = trade_monitor._mark_pending_trades_filled(db, "0xh", filled_at=filled_at)

        self.assertEqual(count, 2)
        for row in rows:
            self.assertEqual(row.status, "executed")
            self.assertEqual(row.executed_at, filled_at)
            self.assertEqual(row.fee_paid, 0.0)
            self.assertIsNone(row.filled_price)
            self.assertIsNone(row.slippage_bps)
            self.assertIsNotNone(row.latency_ms)
        self.assertEqual(db.commits, 1)

    def test_marks_rows_with_fill_details(self):
        rows = [self._row(expected_price=0.5)]
        db = FakeSession()
        db.register(UserTrade, all_results=rows)
        filled_at = datetime(2026, 1, 2, tzinfo=UTC)

        trade_monitor._mark_pending_trades_filled(
            db,
            "0xh",
            filled_at=filled_at,
            fill_price=0.55,
            fill_size=2.0,
            fill_fee=0.01,
        )

        row = rows[0]
        self.assertEqual(row.filled_price, 0.55)
        self.assertEqual(row.filled_size, 2.0)
        self.assertEqual(row.fee_paid, 0.01)
        self.assertEqual(row.slippage_bps, compute_slippage_bps(0.5, 0.55))
        self.assertEqual(
            row.latency_ms, compute_latency_ms(datetime(2026, 1, 1, tzinfo=UTC), filled_at)
        )

    def test_filled_at_defaults_to_now(self):
        rows = [self._row()]
        db = FakeSession()
        db.register(UserTrade, all_results=rows)

        trade_monitor._mark_pending_trades_filled(db, "0xh")

        self.assertIsNotNone(rows[0].executed_at)


# ────────────── HTTP polling loop ──────────────


class HttpPollCycleTests(_MonitorTestCase):
    async def test_empty_watch_list_returns(self):
        trade_monitor._watched_wallets = set()
        poll = AsyncMock()
        with patch.object(trade_monitor, "_poll_and_process", poll):
            await trade_monitor._http_poll_cycle()

        poll.assert_not_awaited()

    async def test_polls_every_wallet(self):
        trade_monitor._watched_wallets = {WALLET, "0xbbb"}
        poll = AsyncMock(side_effect=[None, RuntimeError("boom")])
        with patch.object(trade_monitor, "_poll_and_process", poll):
            await trade_monitor._http_poll_cycle()

        self.assertEqual(poll.await_count, 2)


class PollAndProcessTests(_MonitorTestCase):
    async def test_polls_detects_and_processes(self):
        trade = {"id": "t1", "market": "m", "side": "BUY", "size": 1, "price": 0.5}
        with (
            patch.object(
                trade_monitor,
                "_poll_trader_trades",
                AsyncMock(return_value=[trade]),
            ),
            patch.object(
                trade_monitor,
                "_detect_new_trades",
                AsyncMock(return_value=[trade]),
            ) as detect,
            patch.object(
                trade_monitor,
                "_process_new_trade",
                AsyncMock(),
            ) as process,
        ):
            await trade_monitor._poll_and_process(WALLET)

        detect.assert_awaited_once_with(WALLET, [trade])
        process.assert_awaited_once_with(WALLET, trade)


class ProcessNewTradeTests(_MonitorTestCase):
    @contextlib.contextmanager
    def _pipeline(
        self, session=None, ensure=None, position=None, notifications=None, copy_trade=None
    ):
        session = session if session is not None else _mock_session()
        ensure = ensure if ensure is not None else MagicMock()
        position = position if position is not None else MagicMock(return_value=(None, 0.0, 0.0))
        notifications = notifications if notifications is not None else AsyncMock()
        copy_trade = copy_trade if copy_trade is not None else AsyncMock()
        with (
            patch.object(trade_monitor, "SessionLocal", return_value=session),
            patch.object(trade_monitor, "_ensure_market", ensure),
            patch.object(trade_monitor, "_update_trader_position_state", position),
            patch.object(trade_monitor, "_create_follow_notifications", notifications),
            patch.object(trade_monitor, "_trigger_copy_trade", copy_trade),
        ):
            yield session, ensure, position, notifications, copy_trade

    async def test_missing_market_id_skips(self):
        with self._pipeline() as (_session, ensure, _pos, _notif, _copy):
            await trade_monitor._process_new_trade(WALLET, {"side": "BUY", "size": 1, "price": 0.5})

        ensure.assert_not_called()

    async def test_zero_amount_skips(self):
        with self._pipeline() as (_session, ensure, _pos, _notif, _copy):
            await trade_monitor._process_new_trade(
                WALLET, {"market": "m", "side": "BUY", "size": 0, "price": 0.5}
            )

        ensure.assert_not_called()

    async def test_records_trade_with_derived_notional(self):
        trade = {"id": "t1", "market": "m", "side": "BUY", "size": 10, "price": 0.5}
        with self._pipeline() as (session, ensure, _pos, _notif, _copy):
            await trade_monitor._process_new_trade(WALLET, trade)

        ensure.assert_called_once()
        recorded = session.add.call_args.args[0]
        self.assertEqual(recorded.market_id, "m")
        self.assertEqual(recorded.wallet_address, WALLET)
        self.assertEqual(recorded.order_type, "buy")
        self.assertEqual(recorded.amount, 10.0)
        self.assertEqual(recorded.price, 0.5)
        # usdcSize absent → notional derived from amount × price.
        self.assertEqual(recorded.notional_usdc, 5.0)
        self.assertEqual(recorded.source_trade_id_ext, "t1")

    async def test_explicit_notional_is_kept(self):
        trade = {
            "id": "t1",
            "market": "m",
            "side": "BUY",
            "size": 10,
            "price": 0.5,
            "usdcSize": 8.0,
        }
        with self._pipeline() as (session, _ensure, _pos, _notif, _copy):
            await trade_monitor._process_new_trade(WALLET, trade)

        self.assertEqual(session.add.call_args.args[0].notional_usdc, 8.0)

    async def test_opened_event_creates_notifications(self):
        trade = {"id": "t1", "market": "m", "side": "BUY", "size": 10, "price": 0.5}
        position = MagicMock(return_value=("opened", 0.0, 10.0))
        with self._pipeline(position=position) as (_session, _ensure, _pos, notifications, _copy):
            await trade_monitor._process_new_trade(WALLET, trade)

        notifications.assert_awaited_once()
        self.assertEqual(notifications.call_args.kwargs["event_type"], "opened")
        self.assertEqual(notifications.call_args.kwargs["prev_net_size"], 0.0)
        self.assertEqual(notifications.call_args.kwargs["new_net_size"], 10.0)

    async def test_no_transition_skips_notifications(self):
        trade = {"id": "t1", "market": "m", "side": "BUY", "size": 10, "price": 0.5}
        with self._pipeline() as (_session, _ensure, _pos, notifications, _copy):
            await trade_monitor._process_new_trade(WALLET, trade)

        notifications.assert_not_awaited()

    async def test_triggers_copy_trade_per_follower(self):
        trade = {"id": "t1", "market": "m", "side": "BUY", "size": 10, "price": 0.5}
        session = _mock_session(
            followers=[
                SimpleNamespace(user_id=1),
                SimpleNamespace(user_id=2),
            ]
        )
        with self._pipeline(session=session) as (_session, _ensure, _pos, _notif, copy_trade):
            await trade_monitor._process_new_trade(WALLET, trade)

        self.assertEqual(copy_trade.await_count, 2)
        self.assertEqual(copy_trade.call_args_list[0].kwargs["user_id"], 1)
        self.assertEqual(copy_trade.call_args_list[1].kwargs["user_id"], 2)

    async def test_copy_trade_failure_is_isolated_per_follower(self):
        trade = {"id": "t1", "market": "m", "side": "BUY", "size": 10, "price": 0.5}
        session = _mock_session(
            followers=[
                SimpleNamespace(user_id=1),
                SimpleNamespace(user_id=2),
            ]
        )
        copy_trade = AsyncMock(side_effect=[RuntimeError("boom"), None])
        with self._pipeline(session=session, copy_trade=copy_trade):
            await trade_monitor._process_new_trade(WALLET, trade)  # no raise

        self.assertEqual(copy_trade.await_count, 2)

    async def test_pipeline_error_rolls_back(self):
        trade = {"id": "t1", "market": "m", "side": "BUY", "size": 10, "price": 0.5}
        session = _mock_session()
        ensure = MagicMock(side_effect=RuntimeError("boom"))
        with self._pipeline(session=session, ensure=ensure):
            await trade_monitor._process_new_trade(WALLET, trade)  # no raise

        session.rollback.assert_called_once()
        session.close.assert_called_once()

    async def test_duplicate_trade_is_skipped(self):
        trade = {"id": "t1", "market": "m", "side": "BUY", "size": 10, "price": 0.5}
        with self._pipeline() as (_session, ensure, _pos, _notif, _copy):
            await trade_monitor._process_new_trade(WALLET, trade)
            await trade_monitor._process_new_trade(WALLET, dict(trade))

        # The second delivery is a duplicate → pipeline ran once.
        ensure.assert_called_once()

    async def test_event_without_dedup_key_is_processed(self):
        # No id/trade_id and no side → no fingerprint either,
        # so dedup is bypassed entirely.
        trade = {"market": "m", "size": 10, "price": 0.5}
        with self._pipeline() as (_session, ensure, _pos, _notif, _copy):
            await trade_monitor._process_new_trade(WALLET, trade)
            await trade_monitor._process_new_trade(WALLET, dict(trade))

        # No dedup key → both deliveries run the pipeline.
        self.assertEqual(ensure.call_count, 2)


class TriggerCopyTradeTests(_MonitorTestCase):
    def _db(self, settings):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = settings
        return db

    async def test_no_settings_executes_without_assessment(self):
        db = self._db(None)
        execute = AsyncMock()
        with patch("app.services.copy_trade_service.execute_copy_trade", execute):
            await trade_monitor._trigger_copy_trade(
                db=db,
                user_id=1,
                trader_wallet=WALLET,
                market_id="m",
                token_id="tok",
                side="BUY",
                price=0.5,
                trader_amount=10.0,
                trade_history_id=1,
            )

        execute.assert_awaited_once()
        self.assertIsNone(execute.call_args.kwargs["assessment"])

    async def test_ai_approval_disabled_executes_directly(self):
        db = self._db(SimpleNamespace(require_ai_approval=False))
        ai = AsyncMock()
        execute = AsyncMock()
        with (
            patch.object(trade_monitor, "_run_ai_evaluation", ai),
            patch("app.services.copy_trade_service.execute_copy_trade", execute),
        ):
            await trade_monitor._trigger_copy_trade(
                db=db,
                user_id=1,
                trader_wallet=WALLET,
                market_id="m",
                token_id="tok",
                side="BUY",
                price=0.5,
                trader_amount=10.0,
                trade_history_id=1,
            )

        ai.assert_not_awaited()
        execute.assert_awaited_once()

    async def test_approved_assessment_is_forwarded(self):
        db = self._db(SimpleNamespace(require_ai_approval=True))
        assessment = {"approved": True}
        ai = AsyncMock(return_value=assessment)
        execute = AsyncMock()
        with (
            patch.object(trade_monitor, "_run_ai_evaluation", ai),
            patch("app.services.copy_trade_service.execute_copy_trade", execute),
        ):
            await trade_monitor._trigger_copy_trade(
                db=db,
                user_id=1,
                trader_wallet=WALLET,
                market_id="m",
                token_id="tok",
                side="BUY",
                price=0.5,
                trader_amount=10.0,
                trade_history_id=1,
            )

        ai.assert_awaited_once()
        self.assertIs(execute.call_args.kwargs["assessment"], assessment)

    async def test_missing_assessment_fails_closed(self):
        db = self._db(SimpleNamespace(require_ai_approval=True))
        ai = AsyncMock(return_value=None)
        execute = AsyncMock()
        with (
            patch.object(trade_monitor, "_run_ai_evaluation", ai),
            patch("app.services.copy_trade_service.execute_copy_trade", execute),
        ):
            await trade_monitor._trigger_copy_trade(
                db=db,
                user_id=1,
                trader_wallet=WALLET,
                market_id="m",
                token_id="tok",
                side="BUY",
                price=0.5,
                trader_amount=10.0,
                trade_history_id=1,
            )

        execute.assert_not_awaited()

    async def test_evaluation_exception_fails_closed(self):
        db = self._db(SimpleNamespace(require_ai_approval=True))
        ai = AsyncMock(side_effect=RuntimeError("boom"))
        execute = AsyncMock()
        with (
            patch.object(trade_monitor, "_run_ai_evaluation", ai),
            patch("app.services.copy_trade_service.execute_copy_trade", execute),
        ):
            await trade_monitor._trigger_copy_trade(
                db=db,
                user_id=1,
                trader_wallet=WALLET,
                market_id="m",
                token_id="tok",
                side="BUY",
                price=0.5,
                trader_amount=10.0,
                trade_history_id=1,
            )

        execute.assert_not_awaited()


class RunAiEvaluationTests(_MonitorTestCase):
    def _db(self, settings, winner):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [
            settings,
            winner,
        ]
        return db

    async def test_llm_chain_backend_with_winner_stats(self):
        settings = SimpleNamespace(ai_backend="llm_chain")
        winner = SimpleNamespace(
            win_rate=60.0,
            total_pnl=100.0,
            trade_count=5,
            markets_traded=3,
        )
        db = self._db(settings, winner)
        client = MagicMock()
        client.evaluate_copy_trade = AsyncMock(return_value={"approved": True})
        client.close = AsyncMock()
        client_cls = MagicMock(return_value=client)
        with patch("app.grpc_clients.analysis_client.AnalysisClient", client_cls):
            result = await trade_monitor._run_ai_evaluation(
                db=db,
                user_id=1,
                trader_wallet=WALLET,
                market_id="m",
                side="BUY",
                price=0.5,
                amount=10.0,
            )

        self.assertEqual(result, {"approved": True})
        client_cls.assert_called_once_with(
            backend=trade_monitor.AIBackend.LLM_CHAIN
            if hasattr(trade_monitor, "AIBackend")
            else "llm_chain"
        )
        kwargs = client.evaluate_copy_trade.call_args.kwargs
        self.assertEqual(kwargs["trader_wallet"], WALLET)
        self.assertEqual(kwargs["market_id"], "m")
        self.assertEqual(kwargs["trade_side"], "BUY")
        self.assertEqual(kwargs["trade_size"], 10.0)
        self.assertEqual(kwargs["current_price"], 0.5)
        self.assertIn("Win rate: 60.0%", kwargs["trader_stats"])
        client.close.assert_awaited_once()

    async def test_cli_agent_backend_without_winner(self):
        settings = SimpleNamespace(ai_backend="cli_agent")
        db = self._db(settings, None)
        client = MagicMock()
        client.evaluate_copy_trade = AsyncMock(return_value={"approved": False})
        client.close = AsyncMock()
        client_cls = MagicMock(return_value=client)
        with patch("app.grpc_clients.analysis_client.AnalysisClient", client_cls):
            result = await trade_monitor._run_ai_evaluation(
                db=db,
                user_id=1,
                trader_wallet=WALLET,
                market_id="m",
                side="SELL",
                price=0.5,
                amount=10.0,
            )

        self.assertEqual(result, {"approved": False})
        client_cls.assert_called_once()
        self.assertEqual(client_cls.call_args.kwargs["backend"].value, "cli_agent")
        self.assertEqual(client.evaluate_copy_trade.call_args.kwargs["trader_stats"], "")

    async def test_evaluation_failure_returns_none(self):
        settings = SimpleNamespace(ai_backend="llm_chain")
        db = self._db(settings, None)
        client = MagicMock()
        client.evaluate_copy_trade = AsyncMock(side_effect=RuntimeError("boom"))
        client.close = AsyncMock()
        client_cls = MagicMock(return_value=client)
        with patch("app.grpc_clients.analysis_client.AnalysisClient", client_cls):
            result = await trade_monitor._run_ai_evaluation(
                db=db,
                user_id=1,
                trader_wallet=WALLET,
                market_id="m",
                side="BUY",
                price=0.5,
                amount=10.0,
            )

        self.assertIsNone(result)
        client.close.assert_awaited_once()

    async def test_client_construction_failure_returns_none(self):
        settings = SimpleNamespace(ai_backend="llm_chain")
        db = self._db(settings, None)
        client_cls = MagicMock(side_effect=RuntimeError("boom"))
        with patch("app.grpc_clients.analysis_client.AnalysisClient", client_cls):
            result = await trade_monitor._run_ai_evaluation(
                db=db,
                user_id=1,
                trader_wallet=WALLET,
                market_id="m",
                side="BUY",
                price=0.5,
                amount=10.0,
            )

        self.assertIsNone(result)


class UpdateTraderPositionStateTests(_MonitorTestCase):
    def test_empty_token_returns_no_event(self):
        db = FakeSession()
        event, prev, new = trade_monitor._update_trader_position_state(
            db, WALLET, "", "", "BUY", 5.0
        )

        self.assertIsNone(event)
        self.assertEqual(prev, 0.0)
        self.assertEqual(new, 0.0)
        self.assertEqual(db.added, [])

    def test_new_buy_opens_position(self):
        db = FakeSession()
        db.register(TraderPositionState, first=None)

        event, prev, new = trade_monitor._update_trader_position_state(
            db, WALLET_UPPER, "tok", "mkt", "BUY", 5.0
        )

        self.assertEqual(event, "opened")
        self.assertEqual(prev, 0.0)
        self.assertEqual(new, 5.0)
        row = db.added[0]
        self.assertEqual(row.trader_wallet, WALLET)
        self.assertEqual(row.token_id, "tok")
        self.assertEqual(row.market_id, "mkt")
        self.assertEqual(row.net_size, 5.0)
        self.assertEqual(db.commits, 1)

    def test_sell_to_zero_closes_position(self):
        row = SimpleNamespace(net_size=5.0, market_id="old", updated_at=None)
        db = FakeSession()
        db.register(TraderPositionState, first=row)

        event, prev, new = trade_monitor._update_trader_position_state(
            db, WALLET, "tok", "mkt", "SELL", 5.0
        )

        self.assertEqual(event, "closed")
        self.assertEqual(prev, 5.0)
        self.assertEqual(new, 0.0)
        self.assertEqual(row.net_size, 0.0)
        self.assertEqual(row.market_id, "mkt")

    def test_buy_with_existing_position_has_no_event(self):
        row = SimpleNamespace(net_size=5.0, market_id="mkt", updated_at=None)
        db = FakeSession()
        db.register(TraderPositionState, first=row)

        event, prev, new = trade_monitor._update_trader_position_state(
            db, WALLET, "tok", "mkt", "BUY", 5.0
        )

        self.assertIsNone(event)
        self.assertEqual(prev, 5.0)
        self.assertEqual(new, 10.0)

    def test_epsilon_collapse_produces_no_event(self):
        row = SimpleNamespace(net_size=0.0, market_id="mkt", updated_at=None)
        db = FakeSession()
        db.register(TraderPositionState, first=row)

        event, prev, new = trade_monitor._update_trader_position_state(
            db, WALLET, "tok", "mkt", "BUY", 1e-10
        )

        self.assertIsNone(event)
        self.assertEqual(new, 0.0)


class SmtpReadyTests(unittest.TestCase):
    def test_smtp_ready_delegates(self):
        with patch.object(trade_monitor, "is_smtp_configured", return_value=True):
            self.assertTrue(trade_monitor._smtp_ready())
        with patch.object(trade_monitor, "is_smtp_configured", return_value=False):
            self.assertFalse(trade_monitor._smtp_ready())


class CreateFollowNotificationsTests(_MonitorTestCase):
    def _db(self, followers, users, settings):
        db = FakeSession()
        db.register(NotificationFollowedTrader, all_results=followers)
        db.register(User, all_results=users)
        db.register(UserSettings, all_results=settings)
        return db

    def _kwargs(self, **overrides):
        base = {
            "trader_wallet": WALLET_UPPER,
            "event_type": "opened",
            "market_id": "mkt",
            "token_id": "tok",
            "side": "buy",
            "size": 1.0,
            "price": 0.5,
            "prev_net_size": 0.0,
            "new_net_size": 1.0,
            "source_trade_history_id": 9,
        }
        base.update(overrides)
        return base

    async def test_no_followers_returns_early(self):
        db = self._db([], [SimpleNamespace(id=1, email="a@b.c")], [])
        send = AsyncMock()
        with patch.object(trade_monitor, "send_follow_event_email", send):
            await trade_monitor._create_follow_notifications(db=db, **self._kwargs())

        self.assertEqual(db.added, [])
        send.assert_not_awaited()

    async def test_disabled_follower_is_skipped(self):
        followers = [
            SimpleNamespace(
                user_id=1,
                trader_wallet=WALLET,
                is_active=True,
                feed_enabled=False,
                email_enabled=False,
            )
        ]
        db = self._db(followers, [SimpleNamespace(id=1, email="a@b.c")], [])
        send = AsyncMock()
        with patch.object(trade_monitor, "send_follow_event_email", send):
            await trade_monitor._create_follow_notifications(db=db, **self._kwargs())

        self.assertEqual(db.added, [])
        send.assert_not_awaited()

    async def test_feed_only_follower_creates_skipped_event(self):
        followers = [
            SimpleNamespace(
                user_id=1,
                trader_wallet=WALLET,
                is_active=True,
                feed_enabled=True,
                email_enabled=False,
            )
        ]
        db = self._db(followers, [SimpleNamespace(id=1, email="a@b.c")], [])
        send = AsyncMock()
        with (
            patch.object(trade_monitor, "is_smtp_configured", return_value=True),
            patch.object(trade_monitor, "send_follow_event_email", send),
        ):
            await trade_monitor._create_follow_notifications(db=db, **self._kwargs())

        event = db.added[0]
        self.assertEqual(event.email_status, "skipped")
        self.assertIsNone(event.email_error)
        send.assert_not_awaited()
        self.assertEqual(db.commits, 1)

    async def test_missing_user_email_skips(self):
        followers = [
            SimpleNamespace(
                user_id=1,
                trader_wallet=WALLET,
                is_active=True,
                feed_enabled=True,
                email_enabled=True,
            )
        ]
        db = self._db(followers, [SimpleNamespace(id=1, email=None)], [])
        send = AsyncMock()
        with (
            patch.object(trade_monitor, "is_smtp_configured", return_value=True),
            patch.object(trade_monitor, "send_follow_event_email", send),
        ):
            await trade_monitor._create_follow_notifications(db=db, **self._kwargs())

        event = db.added[0]
        self.assertEqual(event.email_status, "skipped")
        self.assertEqual(event.email_error, "User email is not configured.")
        send.assert_not_awaited()

    async def test_disabled_global_setting_skips(self):
        followers = [
            SimpleNamespace(
                user_id=1,
                trader_wallet=WALLET,
                is_active=True,
                feed_enabled=True,
                email_enabled=True,
            )
        ]
        settings = [SimpleNamespace(user_id=1, follow_email_notifications_enabled=False)]
        db = self._db(followers, [SimpleNamespace(id=1, email="a@b.c")], settings)
        send = AsyncMock()
        with (
            patch.object(trade_monitor, "is_smtp_configured", return_value=True),
            patch.object(trade_monitor, "send_follow_event_email", send),
        ):
            await trade_monitor._create_follow_notifications(db=db, **self._kwargs())

        event = db.added[0]
        self.assertEqual(event.email_status, "skipped")
        self.assertEqual(event.email_error, "Global follow-email notifications are disabled.")

    async def test_missing_settings_record_skips(self):
        followers = [
            SimpleNamespace(
                user_id=1,
                trader_wallet=WALLET,
                is_active=True,
                feed_enabled=True,
                email_enabled=True,
            )
        ]
        db = self._db(followers, [SimpleNamespace(id=1, email="a@b.c")], [])
        send = AsyncMock()
        with (
            patch.object(trade_monitor, "is_smtp_configured", return_value=True),
            patch.object(trade_monitor, "send_follow_event_email", send),
        ):
            await trade_monitor._create_follow_notifications(db=db, **self._kwargs())

        event = db.added[0]
        self.assertEqual(event.email_status, "skipped")
        self.assertEqual(event.email_error, "Global follow-email notifications are disabled.")

    async def test_smtp_unavailable_skips(self):
        followers = [
            SimpleNamespace(
                user_id=1,
                trader_wallet=WALLET,
                is_active=True,
                feed_enabled=True,
                email_enabled=True,
            )
        ]
        settings = [SimpleNamespace(user_id=1, follow_email_notifications_enabled=True)]
        db = self._db(followers, [SimpleNamespace(id=1, email="a@b.c")], settings)
        send = AsyncMock()
        with (
            patch.object(trade_monitor, "is_smtp_configured", return_value=False),
            patch.object(trade_monitor, "send_follow_event_email", send),
        ):
            await trade_monitor._create_follow_notifications(db=db, **self._kwargs())

        event = db.added[0]
        self.assertEqual(event.email_status, "skipped")
        self.assertEqual(event.email_error, "SMTP is not configured.")
        send.assert_not_awaited()

    async def test_email_sent(self):
        followers = [
            SimpleNamespace(
                user_id=1,
                trader_wallet=WALLET,
                is_active=True,
                feed_enabled=True,
                email_enabled=True,
            )
        ]
        settings = [SimpleNamespace(user_id=1, follow_email_notifications_enabled=True)]
        db = self._db(followers, [SimpleNamespace(id=1, email="a@b.c")], settings)
        send = AsyncMock()
        with (
            patch.object(trade_monitor, "is_smtp_configured", return_value=True),
            patch.object(trade_monitor, "send_follow_event_email", send),
        ):
            await trade_monitor._create_follow_notifications(db=db, **self._kwargs())

        event = db.added[0]
        self.assertEqual(event.email_status, "sent")
        self.assertIsNone(event.email_error)
        self.assertIsNotNone(event.emailed_at)
        send.assert_awaited_once()
        self.assertEqual(send.call_args.kwargs["to_email"], "a@b.c")
        self.assertEqual(send.call_args.kwargs["event_type"], "opened")

    async def test_email_failure_marks_event_failed(self):
        followers = [
            SimpleNamespace(
                user_id=1,
                trader_wallet=WALLET,
                is_active=True,
                feed_enabled=True,
                email_enabled=True,
            )
        ]
        settings = [SimpleNamespace(user_id=1, follow_email_notifications_enabled=True)]
        db = self._db(followers, [SimpleNamespace(id=1, email="a@b.c")], settings)
        send = AsyncMock(side_effect=RuntimeError("smtp down"))
        with (
            patch.object(trade_monitor, "is_smtp_configured", return_value=True),
            patch.object(trade_monitor, "send_follow_event_email", send),
        ):
            await trade_monitor._create_follow_notifications(db=db, **self._kwargs())

        event = db.added[0]
        self.assertEqual(event.email_status, "failed")
        self.assertEqual(event.email_error, "smtp down")


class EmitAlertTests(_MonitorTestCase):
    async def test_no_dispatcher_is_a_noop(self):
        with patch.object(trade_monitor, "_alert_dispatch", None):
            await trade_monitor._emit_alert(1, "t", "m")  # no raise

    async def test_dispatches_to_alert_service(self):
        dispatch = AsyncMock()
        with patch.object(trade_monitor, "_alert_dispatch", dispatch):
            await trade_monitor._emit_alert(1, "gtc_order_cancelled", "msg")

        dispatch.assert_awaited_once_with("gtc_order_cancelled", 1, {"message": "msg"})

    async def test_dispatch_failure_is_swallowed(self):
        dispatch = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(trade_monitor, "_alert_dispatch", dispatch):
            await trade_monitor._emit_alert(1, "t", "m")  # no raise


class JobControlTests(_MonitorTestCase):
    def test_gtc_ttl_from_settings(self):
        with patch.object(
            trade_monitor,
            "get_settings",
            return_value=SimpleNamespace(gtc_ttl_seconds=3600),
        ):
            self.assertEqual(trade_monitor._gtc_ttl_seconds(), 3600)

    def test_gtc_ttl_falls_back_on_error(self):
        with patch.object(trade_monitor, "get_settings", side_effect=RuntimeError("boom")):
            self.assertEqual(trade_monitor._gtc_ttl_seconds(), 86400)

    def test_gtc_ttl_falls_back_on_bad_value(self):
        with patch.object(
            trade_monitor,
            "get_settings",
            return_value=SimpleNamespace(gtc_ttl_seconds="bad"),
        ):
            self.assertEqual(trade_monitor._gtc_ttl_seconds(), 86400)

    def test_acquire_job_lock_without_scheduler(self):
        with patch.object(trade_monitor, "acquire_scheduler_lock", None):
            self.assertTrue(trade_monitor._acquire_job_lock("job"))

    def test_acquire_job_lock_acquired(self):
        with patch.object(trade_monitor, "acquire_scheduler_lock", return_value=True):
            self.assertTrue(trade_monitor._acquire_job_lock("job"))

    def test_acquire_job_lock_contended(self):
        with patch.object(trade_monitor, "acquire_scheduler_lock", return_value=False):
            self.assertFalse(trade_monitor._acquire_job_lock("job"))

    def test_job_heartbeat_without_scheduler(self):
        with patch.object(trade_monitor, "scheduler_heartbeat", None):
            trade_monitor._job_heartbeat("job")  # no-op

    def test_job_heartbeat_delegates(self):
        heartbeat = MagicMock()
        with patch.object(trade_monitor, "scheduler_heartbeat", heartbeat):
            trade_monitor._job_heartbeat("job")

        heartbeat.assert_called_once_with("job")


class FillReconciliationLoopTests(_MonitorTestCase):
    async def test_lock_contention_returns_immediately(self):
        reconcile = AsyncMock()
        with (
            patch.object(trade_monitor, "_acquire_job_lock", return_value=False),
            patch.object(trade_monitor, "_reconcile_pending_fills", reconcile),
        ):
            await trade_monitor._fill_reconciliation_loop()

        reconcile.assert_not_awaited()

    async def test_loop_sleeps_heartbeats_and_reconciles(self):
        reconcile = AsyncMock()
        heartbeat = MagicMock()
        sleeps = []

        async def _sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                raise _BreakLoop()

        with (
            patch.object(trade_monitor, "_acquire_job_lock", return_value=True),
            patch.object(trade_monitor, "_job_heartbeat", heartbeat),
            patch.object(trade_monitor, "_reconcile_pending_fills", reconcile),
            patch("asyncio.sleep", _sleep),
            self.assertRaises(_BreakLoop),
        ):
            await trade_monitor._fill_reconciliation_loop()

        self.assertEqual(sleeps[0], trade_monitor.FILL_RECON_INTERVAL_SECONDS)
        reconcile.assert_awaited_once()
        heartbeat.assert_called_once_with("fill_reconciliation")

    async def test_reconcile_failure_is_logged_and_loop_continues(self):
        reconcile = AsyncMock(side_effect=[RuntimeError("boom"), None])
        sleeps = []

        async def _sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 3:
                raise _BreakLoop()

        with (
            patch.object(trade_monitor, "_acquire_job_lock", return_value=True),
            patch.object(trade_monitor, "_job_heartbeat", MagicMock()),
            patch.object(trade_monitor, "_reconcile_pending_fills", reconcile),
            patch("asyncio.sleep", _sleep),
            self.assertRaises(_BreakLoop),
        ):
            await trade_monitor._fill_reconciliation_loop()

        self.assertEqual(reconcile.await_count, 2)

    async def test_cancellation_propagates(self):
        reconcile = AsyncMock(side_effect=asyncio.CancelledError())
        with (
            patch.object(trade_monitor, "_acquire_job_lock", return_value=True),
            patch.object(trade_monitor, "_job_heartbeat", MagicMock()),
            patch.object(trade_monitor, "_reconcile_pending_fills", reconcile),
            patch("asyncio.sleep", AsyncMock()),
            self.assertRaises(asyncio.CancelledError),
        ):
            await trade_monitor._fill_reconciliation_loop()


class ReconcilePendingFillsTests(_MonitorTestCase):
    async def test_no_pending_returns(self):
        db = FakeSession()
        db.register(UserTrade, all_results=[])
        reconcile = AsyncMock()
        with (
            patch.object(trade_monitor, "SessionLocal", return_value=db),
            patch.object(trade_monitor, "_reconcile_fills_for_user", reconcile),
        ):
            await trade_monitor._reconcile_pending_fills()

        reconcile.assert_not_awaited()
        self.assertTrue(db.closed)

    async def test_groups_pending_rows_by_user(self):
        rows = [
            SimpleNamespace(user_id=1, order_hash="h1"),
            SimpleNamespace(user_id=2, order_hash="h2"),
            SimpleNamespace(user_id=1, order_hash="h3"),
        ]
        db = FakeSession()
        db.register(UserTrade, all_results=rows)
        reconcile = AsyncMock()
        with (
            patch.object(trade_monitor, "SessionLocal", return_value=db),
            patch.object(trade_monitor, "_reconcile_fills_for_user", reconcile),
        ):
            await trade_monitor._reconcile_pending_fills()

        self.assertEqual(reconcile.await_count, 2)
        by_user = {call.args[1]: call.args[2] for call in reconcile.call_args_list}
        self.assertEqual(len(by_user[1]), 2)
        self.assertEqual(len(by_user[2]), 1)


class ReconcileFillsForUserTests(_MonitorTestCase):
    def _db(self, user):
        db = FakeSession()
        db.register(User, first=user)
        return db

    async def test_missing_user_returns(self):
        db = self._db(None)
        load = MagicMock()
        with patch.object(trade_monitor, "load_wallet_credentials", load):
            await trade_monitor._reconcile_fills_for_user(db, 1, [])

        load.assert_not_called()

    async def test_credential_store_error_returns(self):
        db = self._db(SimpleNamespace(wallet_address=WALLET))
        with patch.object(
            trade_monitor,
            "load_wallet_credentials",
            side_effect=CredentialStoreError("down"),
        ):
            await trade_monitor._reconcile_fills_for_user(db, 1, [])  # no raise

    async def test_missing_credentials_returns(self):
        db = self._db(SimpleNamespace(wallet_address=WALLET))
        with patch.object(trade_monitor, "load_wallet_credentials", return_value=None):
            await trade_monitor._reconcile_fills_for_user(db, 1, [])

    async def test_lookup_failure_skips_row(self):
        db = self._db(SimpleNamespace(wallet_address=WALLET))
        row = SimpleNamespace(order_hash="0xh")
        marked = MagicMock()

        def _lookup(client, order_id):
            raise RuntimeError("boom")

        with (
            patch.object(
                trade_monitor,
                "load_wallet_credentials",
                return_value={"private_key": "pk", "clob_creds": None},
            ),
            patch.object(trade_monitor, "_resolve_proxy_address", return_value=None),
            patch.object(trade_monitor, "build_clob_client", return_value=MagicMock()),
            patch.object(trade_monitor, "clob_trades_for_order", side_effect=_lookup),
            patch.object(trade_monitor, "_mark_pending_trades_filled", marked),
        ):
            await trade_monitor._reconcile_fills_for_user(db, 1, [row])

        marked.assert_not_called()

    async def test_no_fills_leaves_row_pending(self):
        db = self._db(SimpleNamespace(wallet_address=WALLET))
        row = SimpleNamespace(order_hash="0xh")
        marked = MagicMock()
        with (
            patch.object(
                trade_monitor,
                "load_wallet_credentials",
                return_value={"private_key": "pk", "clob_creds": None},
            ),
            patch.object(trade_monitor, "_resolve_proxy_address", return_value=None),
            patch.object(trade_monitor, "build_clob_client", return_value=MagicMock()),
            patch.object(trade_monitor, "clob_trades_for_order", return_value=[]),
            patch.object(trade_monitor, "_mark_pending_trades_filled", marked),
        ):
            await trade_monitor._reconcile_fills_for_user(db, 1, [row])

        marked.assert_not_called()

    async def test_fills_mark_pending_trades(self):
        db = self._db(SimpleNamespace(wallet_address=WALLET))
        row = SimpleNamespace(order_hash="0xh")
        marked = MagicMock()
        fill = {
            "price": "0.5",
            "size": "2",
            "fee": "0.01",
            "match_time": 1700000000,
        }
        with (
            patch.object(
                trade_monitor,
                "load_wallet_credentials",
                return_value={"private_key": "pk", "clob_creds": {"api_key": "k"}},
            ),
            patch.object(trade_monitor, "_resolve_proxy_address", return_value="0xproxy"),
            patch.object(trade_monitor, "build_clob_client", return_value=MagicMock()) as build,
            patch.object(trade_monitor, "clob_trades_for_order", return_value=[fill]),
            patch.object(trade_monitor, "_mark_pending_trades_filled", marked),
        ):
            await trade_monitor._reconcile_fills_for_user(db, 1, [row])

        build.assert_called_once_with("pk", {"api_key": "k"}, proxy_address="0xproxy")
        marked.assert_called_once()
        self.assertEqual(marked.call_args.args, (db, "0xh"))
        kwargs = marked.call_args.kwargs
        self.assertEqual(kwargs["fill_price"], 0.5)
        self.assertEqual(kwargs["fill_size"], 2.0)
        self.assertEqual(kwargs["fill_fee"], 0.01)
        self.assertEqual(kwargs["filled_at"], datetime.fromtimestamp(1700000000, tz=UTC))


class ResolveProxyAddressTests(_MonitorTestCase):
    def test_resolves_proxy(self):
        signer = MagicMock()
        signer.return_value.address.return_value = "0xeoa"
        with (
            patch("py_clob_client.signer.Signer", signer),
            patch(
                "app.services.copy_trade_service._get_poly_proxy_wallet_address",
                return_value="0xproxy",
            ) as proxy,
        ):
            result = trade_monitor._resolve_proxy_address("pk")

        self.assertEqual(result, "0xproxy")
        proxy.assert_called_once_with("0xeoa")

    def test_signer_failure_returns_none(self):
        with patch(
            "py_clob_client.signer.Signer",
            side_effect=RuntimeError("boom"),
        ):
            self.assertIsNone(trade_monitor._resolve_proxy_address("pk"))

    def test_proxy_lookup_failure_returns_none(self):
        signer = MagicMock()
        signer.return_value.address.return_value = "0xeoa"
        with (
            patch("py_clob_client.signer.Signer", signer),
            patch(
                "app.services.copy_trade_service._get_poly_proxy_wallet_address",
                side_effect=RuntimeError("boom"),
            ),
        ):
            self.assertIsNone(trade_monitor._resolve_proxy_address("pk"))


class GtcReconciliationLoopTests(_MonitorTestCase):
    async def test_lock_contention_returns_immediately(self):
        reconcile = AsyncMock()
        with (
            patch.object(trade_monitor, "_acquire_job_lock", return_value=False),
            patch.object(trade_monitor, "_reconcile_orphaned_gtc_orders", reconcile),
        ):
            await trade_monitor._gtc_reconciliation_loop()

        reconcile.assert_not_awaited()

    async def test_loop_skips_until_interval_elapses(self):
        reconcile = AsyncMock()
        heartbeat = MagicMock()
        times = iter([100.0, 1000.0])
        sleeps = []

        async def _sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 3:
                raise _BreakLoop()

        with (
            patch.object(trade_monitor, "_acquire_job_lock", return_value=True),
            patch.object(trade_monitor, "_job_heartbeat", heartbeat),
            patch.object(trade_monitor, "_reconcile_orphaned_gtc_orders", reconcile),
            patch.object(trade_monitor, "perf_counter", lambda: next(times)),
            patch("asyncio.sleep", _sleep),
            self.assertRaises(_BreakLoop),
        ):
            await trade_monitor._gtc_reconciliation_loop()

        # First tick: 100s elapsed < 900s → skipped.
        # Second tick: 900s elapsed → reconciliation ran.
        reconcile.assert_awaited_once()
        self.assertEqual(heartbeat.call_count, 2)

    async def test_reconcile_failure_is_logged_and_loop_continues(self):
        reconcile = AsyncMock(side_effect=[RuntimeError("boom"), None])
        times = iter([1000.0, 2000.0, 3000.0])
        sleeps = []

        async def _sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 3:
                raise _BreakLoop()

        with (
            patch.object(trade_monitor, "_acquire_job_lock", return_value=True),
            patch.object(trade_monitor, "_job_heartbeat", MagicMock()),
            patch.object(trade_monitor, "_reconcile_orphaned_gtc_orders", reconcile),
            patch.object(trade_monitor, "perf_counter", lambda: next(times)),
            patch("asyncio.sleep", _sleep),
            self.assertRaises(_BreakLoop),
        ):
            await trade_monitor._gtc_reconciliation_loop()

        self.assertEqual(reconcile.await_count, 2)

    async def test_cancellation_propagates(self):
        reconcile = AsyncMock(side_effect=asyncio.CancelledError())
        with (
            patch.object(trade_monitor, "_acquire_job_lock", return_value=True),
            patch.object(trade_monitor, "_job_heartbeat", MagicMock()),
            patch.object(trade_monitor, "_reconcile_orphaned_gtc_orders", reconcile),
            patch.object(trade_monitor, "perf_counter", return_value=1000.0),
            patch("asyncio.sleep", AsyncMock()),
            self.assertRaises(asyncio.CancelledError),
        ):
            await trade_monitor._gtc_reconciliation_loop()


class ReconcileOrphanedGtcOrdersTests(_MonitorTestCase):
    async def test_no_stale_orders_returns(self):
        db = FakeSession()
        db.register(UserTrade, all_results=[])
        reconcile = AsyncMock()
        with (
            patch.object(trade_monitor, "SessionLocal", return_value=db),
            patch.object(trade_monitor, "_reconcile_gtc_for_user", reconcile),
        ):
            await trade_monitor._reconcile_orphaned_gtc_orders()

        reconcile.assert_not_awaited()
        self.assertTrue(db.closed)

    async def test_groups_stale_rows_by_user(self):
        rows = [
            SimpleNamespace(user_id=1, order_hash="h1"),
            SimpleNamespace(user_id=1, order_hash="h2"),
            SimpleNamespace(user_id=2, order_hash="h3"),
        ]
        db = FakeSession()
        db.register(UserTrade, all_results=rows)
        reconcile = AsyncMock()
        with (
            patch.object(trade_monitor, "SessionLocal", return_value=db),
            patch.object(trade_monitor, "_reconcile_gtc_for_user", reconcile),
        ):
            await trade_monitor._reconcile_orphaned_gtc_orders()

        self.assertEqual(reconcile.await_count, 2)
        by_user = {call.args[1]: call.args[2] for call in reconcile.call_args_list}
        self.assertEqual(len(by_user[1]), 2)
        self.assertEqual(by_user[1][0].order_hash, "h1")


class ReconcileGtcForUserTests(_MonitorTestCase):
    def _db(self, user):
        db = FakeSession()
        db.register(User, first=user)
        return db

    @contextlib.contextmanager
    def _env(self, to_thread_results):
        alert = AsyncMock()
        marked = MagicMock()
        to_thread = AsyncMock(side_effect=list(to_thread_results))
        with (
            patch.object(
                trade_monitor,
                "load_wallet_credentials",
                return_value={"private_key": "pk", "clob_creds": None},
            ),
            patch.object(trade_monitor, "_resolve_proxy_address", return_value=None),
            patch.object(
                trade_monitor,
                "build_clob_client",
                return_value=MagicMock(),
            ),
            patch("asyncio.to_thread", to_thread),
            patch.object(trade_monitor, "_emit_alert", alert),
            patch.object(trade_monitor, "_mark_pending_trades_filled", marked),
        ):
            yield to_thread, alert, marked

    async def test_missing_user_returns(self):
        db = self._db(None)
        load = MagicMock()
        with patch.object(trade_monitor, "load_wallet_credentials", load):
            await trade_monitor._reconcile_gtc_for_user(db, 1, [], 86400)

        load.assert_not_called()

    async def test_credential_store_error_returns(self):
        db = self._db(SimpleNamespace(wallet_address=WALLET))
        with patch.object(
            trade_monitor,
            "load_wallet_credentials",
            side_effect=CredentialStoreError("down"),
        ):
            await trade_monitor._reconcile_gtc_for_user(
                db, 1, [SimpleNamespace(order_hash="h")], 86400
            )  # no raise

    async def test_missing_credentials_returns(self):
        db = self._db(SimpleNamespace(wallet_address=WALLET))
        with patch.object(trade_monitor, "load_wallet_credentials", return_value=None):
            await trade_monitor._reconcile_gtc_for_user(
                db, 1, [SimpleNamespace(order_hash="h")], 86400
            )

    async def test_open_order_lookup_failure_returns(self):
        db = self._db(SimpleNamespace(wallet_address=WALLET))
        row = SimpleNamespace(order_hash="0xh", status="pending")
        with self._env([RuntimeError("boom")]):
            await trade_monitor._reconcile_gtc_for_user(db, 1, [row], 86400)

        self.assertEqual(row.status, "pending")

    async def test_resting_order_is_cancelled_on_exchange(self):
        db = self._db(SimpleNamespace(wallet_address=WALLET))
        row = SimpleNamespace(order_hash="0xh", status="pending")
        with self._env([[{"id": "0xh"}], None]) as (_to_thread, alert, _marked):
            await trade_monitor._reconcile_gtc_for_user(db, 1, [row], 86400)

        self.assertEqual(row.status, "cancelled")
        self.assertEqual(db.commits, 1)
        alert.assert_awaited_once()
        self.assertEqual(alert.call_args.args[1], "gtc_order_cancelled")

    async def test_cancel_failure_leaves_row_pending(self):
        db = self._db(SimpleNamespace(wallet_address=WALLET))
        row = SimpleNamespace(order_hash="0xh", status="pending")
        with self._env([[{"id": "0xh"}], RuntimeError("boom")]) as (
            _to_thread,
            alert,
            _marked,
        ):
            await trade_monitor._reconcile_gtc_for_user(db, 1, [row], 86400)

        self.assertEqual(row.status, "pending")
        alert.assert_not_awaited()

    async def test_orphan_that_filled_is_marked_executed(self):
        db = self._db(SimpleNamespace(wallet_address=WALLET))
        row = SimpleNamespace(order_hash="0xh", status="pending")
        fill = {"price": "0.5", "size": "1", "fee": "0.0", "match_time": 1700000000}
        with self._env([[], [fill]]) as (_to_thread, alert, marked):
            await trade_monitor._reconcile_gtc_for_user(db, 1, [row], 86400)

        marked.assert_called_once()
        self.assertEqual(marked.call_args.args, (db, "0xh"))
        alert.assert_awaited_once()
        self.assertEqual(alert.call_args.args[1], "gtc_order_filled")

    async def test_orphan_gone_from_exchange_is_cancelled_locally(self):
        db = self._db(SimpleNamespace(wallet_address=WALLET))
        row = SimpleNamespace(order_hash="0xh", status="pending")
        with self._env([[], []]) as (_to_thread, alert, _marked):
            await trade_monitor._reconcile_gtc_for_user(db, 1, [row], 86400)

        self.assertEqual(row.status, "cancelled")
        self.assertEqual(db.commits, 1)
        alert.assert_awaited_once()
        self.assertEqual(alert.call_args.args[1], "gtc_order_cancelled")

    async def test_trade_lookup_failure_skips_row(self):
        db = self._db(SimpleNamespace(wallet_address=WALLET))
        row = SimpleNamespace(order_hash="0xh", status="pending")
        with self._env([[], RuntimeError("boom")]) as (
            _to_thread,
            alert,
            _marked,
        ):
            await trade_monitor._reconcile_gtc_for_user(db, 1, [row], 86400)

        self.assertEqual(row.status, "pending")
        alert.assert_not_awaited()

    async def test_empty_open_order_book_treats_orders_as_gone(self):
        db = self._db(SimpleNamespace(wallet_address=WALLET))
        row = SimpleNamespace(order_hash="0xh", status="pending")
        with self._env([None, []]) as (_to_thread, _alert, _marked):
            await trade_monitor._reconcile_gtc_for_user(db, 1, [row], 86400)

        self.assertEqual(row.status, "cancelled")


# ────────────── Helpers ──────────────


class EnsureMarketTests(_MonitorTestCase):
    def test_existing_market_is_kept(self):
        db = FakeSession()
        db.register(Market, first=SimpleNamespace(id="m"))

        trade_monitor._ensure_market(db, "m", {"question": "q"})

        self.assertEqual(db.added, [])
        self.assertEqual(db.commits, 0)

    def test_new_market_uses_question(self):
        db = FakeSession()
        db.register(Market, first=None)

        trade_monitor._ensure_market(db, "m", {"question": "q"})

        market = db.added[0]
        self.assertEqual(market.id, "m")
        self.assertEqual(market.question, "q")
        self.assertEqual(market.status, "active")
        self.assertEqual(db.commits, 1)

    def test_new_market_falls_back_to_title_then_id(self):
        db = FakeSession()
        db.register(Market, first=None)
        trade_monitor._ensure_market(db, "m", {"title": "T"})
        self.assertEqual(db.added[0].question, "T")

        db = FakeSession()
        db.register(Market, first=None)
        trade_monitor._ensure_market(db, "m", {})
        self.assertEqual(db.added[0].question, "m")

    def test_creation_failure_rolls_back(self):
        db = FakeSession()
        db.register(Market, first=None)
        db.commit_error = RuntimeError("boom")

        trade_monitor._ensure_market(db, "m", {})  # no raise

        self.assertEqual(db.rollbacks, 1)


class ToFloatTests(unittest.TestCase):
    def test_none_returns_default(self):
        self.assertEqual(trade_monitor._to_float(None), 0.0)
        self.assertEqual(trade_monitor._to_float(None, 1.5), 1.5)

    def test_valid_values(self):
        self.assertEqual(trade_monitor._to_float("1.5"), 1.5)
        self.assertEqual(trade_monitor._to_float(3), 3.0)
        self.assertEqual(trade_monitor._to_float(0), 0.0)

    def test_invalid_values_return_default(self):
        self.assertEqual(trade_monitor._to_float("abc"), 0.0)
        self.assertEqual(trade_monitor._to_float("abc", 2.0), 2.0)
        self.assertEqual(trade_monitor._to_float(["x"]), 0.0)


class ParseTimestampTests(unittest.TestCase):
    def test_falsy_returns_none(self):
        self.assertIsNone(trade_monitor._parse_timestamp(None))
        self.assertIsNone(trade_monitor._parse_timestamp(0))
        self.assertIsNone(trade_monitor._parse_timestamp(""))

    def test_epoch_integer(self):
        self.assertEqual(
            trade_monitor._parse_timestamp(1700000000),
            datetime.fromtimestamp(1700000000, tz=UTC),
        )
        self.assertEqual(
            trade_monitor._parse_timestamp("1700000000"),
            datetime.fromtimestamp(1700000000, tz=UTC),
        )

    def test_isoformat_string(self):
        self.assertEqual(
            trade_monitor._parse_timestamp("2026-01-01T00:00:00+00:00"),
            datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.assertEqual(
            trade_monitor._parse_timestamp("2026-01-01T00:00:00Z"),
            datetime(2026, 1, 1, tzinfo=UTC),
        )

    def test_invalid_returns_none(self):
        self.assertIsNone(trade_monitor._parse_timestamp("not-a-date"))
        self.assertIsNone(trade_monitor._parse_timestamp(object()))


# ────────────── Lifecycle ──────────────


async def _block_forever():
    await asyncio.Event().wait()


class LifecycleTests(_MonitorTestCase):
    def setUp(self):
        super().setUp()
        self._saved_tasks = (
            trade_monitor._monitor_task,
            trade_monitor._fill_recon_task,
            trade_monitor._gtc_recon_task,
            trade_monitor._poll_http_client,
        )

    def tearDown(self):
        (
            trade_monitor._monitor_task,
            trade_monitor._fill_recon_task,
            trade_monitor._gtc_recon_task,
            trade_monitor._poll_http_client,
        ) = self._saved_tasks
        super().tearDown()

    async def test_start_creates_tasks_and_http_client(self):
        db = MagicMock()
        refresh = MagicMock()
        http_client = MagicMock()
        task = MagicMock()
        create_task = MagicMock(return_value=task)
        with (
            patch.object(trade_monitor, "SessionLocal", return_value=db),
            patch.object(trade_monitor, "refresh_watched_wallets", refresh),
            patch("httpx.AsyncClient", return_value=http_client),
            patch("asyncio.create_task", create_task),
        ):
            await trade_monitor.start_trade_monitor()

        refresh.assert_called_once_with(db)
        db.close.assert_called_once()
        self.assertEqual(create_task.call_count, 3)
        self.assertIs(trade_monitor._poll_http_client, http_client)
        self.assertIs(trade_monitor._monitor_task, task)
        self.assertIs(trade_monitor._fill_recon_task, task)
        self.assertIs(trade_monitor._gtc_recon_task, task)

    async def test_stop_cancels_tasks_and_closes_client(self):
        done = asyncio.create_task(_block_forever())
        done.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await done
        pending = asyncio.create_task(_block_forever())
        http_client = MagicMock()
        http_client.aclose = AsyncMock()
        trade_monitor._monitor_task = done
        trade_monitor._fill_recon_task = pending
        trade_monitor._gtc_recon_task = None
        trade_monitor._poll_http_client = http_client

        await trade_monitor.stop_trade_monitor()

        self.assertTrue(done.done())
        self.assertTrue(pending.cancelled())
        http_client.aclose.assert_awaited_once()
        self.assertIsNone(trade_monitor._poll_http_client)
        self.assertIsNone(trade_monitor._monitor_task)
        self.assertIsNone(trade_monitor._fill_recon_task)
        self.assertIsNone(trade_monitor._gtc_recon_task)

    async def test_stop_with_no_tasks_or_client(self):
        trade_monitor._monitor_task = None
        trade_monitor._fill_recon_task = None
        trade_monitor._gtc_recon_task = None
        trade_monitor._poll_http_client = None

        await trade_monitor.stop_trade_monitor()  # no raise


class WatchListManagementTests(_MonitorTestCase):
    def test_add_watched_wallet_normalises(self):
        trade_monitor.add_watched_wallet(WALLET_UPPER)

        self.assertIn(WALLET, trade_monitor._watched_wallets)

    def test_remove_watched_wallet_discards(self):
        trade_monitor._watched_wallets.add(WALLET)

        trade_monitor.remove_watched_wallet(WALLET_UPPER)

        self.assertNotIn(WALLET, trade_monitor._watched_wallets)

    def test_remove_unknown_wallet_is_a_noop(self):
        trade_monitor.remove_watched_wallet("0xunknown")  # no raise


class NotificationFeedEventModelSmokeTests(_MonitorTestCase):
    """The feed-event model is imported by the monitor; keep the import honest."""

    def test_models_are_importable(self):
        self.assertTrue(issubclass(NotificationFeedEvent, object))
        self.assertTrue(issubclass(FollowedTrader, object))
        self.assertTrue(issubclass(NotificationFollowedTrader, object))
        self.assertTrue(issubclass(UserTrade, object))
        self.assertTrue(issubclass(UserSettings, object))
        self.assertTrue(issubclass(User, object))
        self.assertTrue(issubclass(TraderPositionState, object))
        self.assertTrue(issubclass(Market, object))


if __name__ == "__main__":
    unittest.main()
