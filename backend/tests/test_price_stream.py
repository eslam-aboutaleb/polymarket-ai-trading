"""
Tests for the real-time price streaming pipeline.

Covers the CLOB WebSocket manager (tick extraction, fan-out to
multiple subscribers, subscription cap and refcounting, reconnect
with resubscribe), the SSE price stream generator (tick emission,
250ms coalescing, stale-tick guard, polling fallback when the
WebSocket is down) and the per-user SSE client cap.
"""

import asyncio
import contextlib
import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import app.api.routes.portfolio as portfolio_module
import app.services.clob_ws_manager as clob_ws_manager
from app.services.clob_ws_manager import ClobWsManager, is_stale_tick

TOKEN_A = "107505882767731489358349912513945399560393482969656700824895970500493757150417"
TOKEN_B = "7305630249804085635496399869905769372294302716159034447326228509068694952392"
MARKET = "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75"


def _price_change_event(token_id=TOKEN_A, price="0.42", ts="1782753357257"):
    return {
        "event_type": "price_change",
        "market": MARKET,
        "price_changes": [
            {
                "asset_id": token_id,
                "price": price,
                "size": "33343.4",
                "side": "BUY",
                "best_bid": price,
                "best_ask": "0.43",
            }
        ],
        "timestamp": ts,
    }


def _last_trade_event(token_id=TOKEN_A, price="0.42", ts="1782753357257"):
    return {
        "event_type": "last_trade_price",
        "market": MARKET,
        "asset_id": token_id,
        "price": price,
        "size": "219.217767",
        "side": "SELL",
        "timestamp": ts,
    }


def _book_event(
    token_id=TOKEN_A,
    bids=(("0.41", "10"),),
    asks=(("0.43", "10"),),
    ts="1782753357257",
):
    return {
        "event_type": "book",
        "market": MARKET,
        "asset_id": token_id,
        "timestamp": ts,
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
    }


def _best_bid_ask_event(token_id=TOKEN_A, best_bid="0.41", best_ask="0.43"):
    return {
        "event_type": "best_bid_ask",
        "market": MARKET,
        "asset_id": token_id,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": "0.02",
    }


class TickExtractionTests(unittest.TestCase):
    """Wire events map to price ticks."""

    def setUp(self):
        self.manager = ClobWsManager()

    def test_price_change_event_extracts_tick(self):
        tick = self.manager._extract_ticks(_price_change_event())
        self.assertEqual(len(tick), 1)
        self.assertEqual(tick[0]["token_id"], TOKEN_A)
        self.assertEqual(tick[0]["price"], 0.42)

    def test_last_trade_event_extracts_tick(self):
        tick = self.manager._extract_ticks(_last_trade_event())
        self.assertEqual(len(tick), 1)
        self.assertEqual(tick[0]["token_id"], TOKEN_A)
        self.assertEqual(tick[0]["price"], 0.42)

    def test_book_event_uses_bid_ask_midpoint(self):
        tick = self.manager._extract_ticks(_book_event())
        self.assertEqual(len(tick), 1)
        self.assertAlmostEqual(tick[0]["price"], 0.42)

    def test_best_bid_ask_event_uses_midpoint(self):
        tick = self.manager._extract_ticks(_best_bid_ask_event())
        self.assertEqual(len(tick), 1)
        self.assertAlmostEqual(tick[0]["price"], 0.42)

    def test_non_price_events_are_ignored(self):
        self.assertEqual(
            self.manager._extract_ticks({"event_type": "tick_size_change", "asset_id": TOKEN_A}),
            [],
        )
        self.assertEqual(self.manager._extract_ticks({"foo": "bar"}), [])

    def test_book_event_without_bids_is_ignored(self):
        self.assertEqual(
            self.manager._extract_ticks(_book_event(bids=())),
            [],
        )

    def test_out_of_range_prices_are_ignored(self):
        event = _price_change_event(price="1.5")
        self.assertEqual(self.manager._extract_ticks(event), [])
        event = _price_change_event(price="0")
        self.assertEqual(self.manager._extract_ticks(event), [])

    def test_price_change_with_multiple_assets_extracts_each(self):
        event = _price_change_event()
        event["price_changes"].append(
            {
                "asset_id": TOKEN_B,
                "price": "0.31",
                "size": "10",
                "side": "SELL",
            }
        )
        ticks = self.manager._extract_ticks(event)
        self.assertEqual(len(ticks), 2)
        self.assertEqual(ticks[1]["token_id"], TOKEN_B)
        self.assertEqual(ticks[1]["price"], 0.31)


class StaleTickGuardTests(unittest.TestCase):
    """Ticks older than 30s are stale."""

    def test_recent_tick_is_fresh(self):
        now = datetime.now(UTC)
        self.assertFalse(is_stale_tick(now.isoformat(), now))

    def test_old_tick_is_stale(self):
        now = datetime.now(UTC)
        old = (now - timedelta(seconds=31)).isoformat()
        self.assertTrue(is_stale_tick(old, now))

    def test_epoch_boundary_is_fresh(self):
        now = datetime.now(UTC)
        edge = (now - timedelta(seconds=29)).isoformat()
        self.assertFalse(is_stale_tick(edge, now))

    def test_missing_timestamp_is_not_stale(self):
        self.assertFalse(is_stale_tick(None))

    def test_unparseable_timestamp_is_not_stale(self):
        self.assertFalse(is_stale_tick("not-a-date"))


class FanOutTests(unittest.IsolatedAsyncioTestCase):
    """Ticks fan out to every registered subscriber."""

    async def test_tick_fans_out_to_all_subscribers(self):
        manager = ClobWsManager()
        handler_a = MagicMock()
        handler_b = MagicMock()
        manager.register_subscriber(handler_a)
        manager.register_subscriber(handler_b)

        manager._fan_out({"token_id": TOKEN_A, "price": 0.42, "ts": "x"})

        handler_a.assert_called_once()
        handler_b.assert_called_once()
        self.assertEqual(handler_a.call_args[0][0]["token_id"], TOKEN_A)
        self.assertEqual(handler_b.call_args[0][0]["price"], 0.42)

    async def test_handle_message_routes_wire_events(self):
        manager = ClobWsManager()
        handler = MagicMock()
        manager.register_subscriber(handler)

        manager._handle_message(json.dumps(_price_change_event()))

        handler.assert_called_once()
        self.assertEqual(handler.call_args[0][0]["token_id"], TOKEN_A)

    async def test_pong_heartbeat_frames_are_ignored(self):
        manager = ClobWsManager()
        handler = MagicMock()
        manager.register_subscriber(handler)

        manager._handle_message("PONG")
        manager._handle_message("PING")

        handler.assert_not_called()

    async def test_malformed_messages_are_ignored(self):
        manager = ClobWsManager()
        handler = MagicMock()
        manager.register_subscriber(handler)

        manager._handle_message("not json")
        manager._handle_message(json.dumps([1, 2, 3]))

        handler.assert_not_called()

    async def test_subscriber_errors_do_not_break_fan_out(self):
        manager = ClobWsManager()
        bad = MagicMock(side_effect=RuntimeError("boom"))
        good = MagicMock()
        manager.register_subscriber(bad)
        manager.register_subscriber(good)

        manager._fan_out({"token_id": TOKEN_A, "price": 0.42, "ts": "x"})

        good.assert_called_once()

    async def test_unregister_stops_delivery(self):
        manager = ClobWsManager()
        handler = MagicMock()
        subscriber_id = manager.register_subscriber(handler)

        manager.unregister_subscriber(subscriber_id)
        manager._fan_out({"token_id": TOKEN_A, "price": 0.42, "ts": "x"})

        handler.assert_not_called()


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    """Subscription bookkeeping: caps and refcounts."""

    async def test_subscribe_returns_accepted_tokens(self):
        manager = ClobWsManager(max_subscriptions=10)
        accepted = await manager.subscribe(["a", "b", "a"])
        self.assertEqual(accepted, ["a", "b"])
        self.assertEqual(manager.subscription_count, 2)

    async def test_subscription_cap_drops_excess_tokens(self):
        manager = ClobWsManager(max_subscriptions=2)
        accepted = await manager.subscribe(["a", "b", "c"])
        self.assertEqual(accepted, ["a", "b"])
        self.assertEqual(manager.subscription_count, 2)

    async def test_refcounts_shared_tokens(self):
        manager = ClobWsManager()
        await manager.subscribe(["a"])
        await manager.subscribe(["a"])
        self.assertEqual(manager.subscription_count, 1)

        await manager.unsubscribe(["a"])
        self.assertEqual(manager.subscription_count, 1)

        await manager.unsubscribe(["a"])
        self.assertEqual(manager.subscription_count, 0)

    async def test_unsubscribe_of_unknown_token_is_noop(self):
        manager = ClobWsManager()
        await manager.unsubscribe(["unknown"])
        self.assertEqual(manager.subscription_count, 0)


class _FakeWs:
    """Minimal async-context-manager WebSocket double."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []
        self.closed = False

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    """The session resubscribes and the loop retries with backoff."""

    async def test_session_resubscribes_full_set_on_connect(self):
        manager = ClobWsManager()
        handler = MagicMock()
        manager.register_subscriber(handler)
        await manager.subscribe(["tok1"])

        fake_ws = _FakeWs([json.dumps(_price_change_event())])
        with patch.object(
            clob_ws_manager.websockets,
            "connect",
            MagicMock(return_value=fake_ws),
        ):
            await manager._session()

        subscribe_frames = [json.loads(f) for f in fake_ws.sent if "assets_ids" in f]
        self.assertEqual(len(subscribe_frames), 1)
        self.assertEqual(subscribe_frames[0]["type"], "market")
        self.assertEqual(subscribe_frames[0]["assets_ids"], ["tok1"])
        # The price_change event fanned out to the subscriber.
        handler.assert_called_once()
        self.assertEqual(handler.call_args[0][0]["token_id"], TOKEN_A)
        self.assertFalse(manager.is_connected)

    async def test_run_retries_with_backoff_after_failure(self):
        manager = ClobWsManager()
        await manager.subscribe(["tok1"])
        attempts = []

        async def session():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("connection dropped")

        with (
            patch.object(manager, "_session", new=session),
            patch.object(clob_ws_manager, "INITIAL_RECONNECT_DELAY_SECONDS", 0.01),
            patch.object(clob_ws_manager, "MAX_RECONNECT_DELAY_SECONDS", 0.02),
        ):
            task = asyncio.create_task(manager._run())
            try:
                for _ in range(200):
                    if len(attempts) >= 3:
                        break
                    await asyncio.sleep(0.01)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        self.assertGreaterEqual(len(attempts), 3)

    async def test_stop_cancels_the_loop(self):
        manager = ClobWsManager()
        manager.start()
        self.assertIsNotNone(manager._task)
        await manager.stop()
        self.assertIsNone(manager._task)
        self.assertFalse(manager.is_connected)


class SseClientTrackerTests(unittest.IsolatedAsyncioTestCase):
    """Concurrent SSE clients are capped per user."""

    async def test_cap_enforced_per_user(self):
        tracker = portfolio_module.SseClientTracker(max_per_user=2)
        self.assertTrue(await tracker.acquire("user1"))
        self.assertTrue(await tracker.acquire("user1"))
        self.assertFalse(await tracker.acquire("user1"))

    async def test_users_are_capped_independently(self):
        tracker = portfolio_module.SseClientTracker(max_per_user=1)
        self.assertTrue(await tracker.acquire("user1"))
        self.assertFalse(await tracker.acquire("user1"))
        self.assertTrue(await tracker.acquire("user2"))

    async def test_release_frees_a_slot(self):
        tracker = portfolio_module.SseClientTracker(max_per_user=1)
        self.assertTrue(await tracker.acquire("user1"))
        self.assertFalse(await tracker.acquire("user1"))
        await tracker.release("user1")
        self.assertTrue(await tracker.acquire("user1"))


class PriceStreamGeneratorTests(unittest.IsolatedAsyncioTestCase):
    """The SSE frame generator emits ticks and falls back to polling."""

    def _mock_manager(self, connected=True):
        manager = MagicMock()
        manager.is_connected = connected
        manager.register_subscriber = MagicMock(return_value=1)
        manager.unregister_subscriber = MagicMock()
        manager.subscribe = AsyncMock(return_value=["tok1"])
        manager.unsubscribe = AsyncMock()
        return manager

    async def _wait_for(self, predicate, timeout=3.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while not predicate():
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError("condition not met in time")
            await asyncio.sleep(0.01)

    async def test_stream_emits_ticks(self):
        manager = self._mock_manager(connected=True)
        captured = {}

        def register(handler):
            captured["handler"] = handler
            return 1

        manager.register_subscriber.side_effect = register

        with patch.object(
            portfolio_module,
            "_position_token_ids",
            new=AsyncMock(return_value=[]),
        ):
            gen = portfolio_module._price_stream_frames(
                token_ids=["tok1"],
                wallet_address="0xuser",
                manager=manager,
            )
            collected = []

            async def consume():
                async for frame in gen:
                    collected.append(frame)
                    if len(collected) >= 1:
                        break

            task = asyncio.create_task(consume())
            await self._wait_for(lambda: "handler" in captured)
            captured["handler"](
                {
                    "token_id": "tok1",
                    "price": 0.42,
                    "ts": datetime.now(UTC).isoformat(),
                }
            )
            await asyncio.wait_for(task, timeout=3)
            await gen.aclose()

        self.assertEqual(len(collected), 1)
        payload = json.loads(collected[0].removeprefix("data: ").strip())
        self.assertEqual(payload["token_id"], "tok1")
        self.assertEqual(payload["price"], 0.42)
        self.assertIn("ts", payload)
        manager.unsubscribe.assert_awaited_once_with(["tok1"])

    async def test_stream_coalesces_rapid_ticks_per_token(self):
        manager = self._mock_manager(connected=True)
        captured = {}

        def register(handler):
            captured["handler"] = handler
            return 1

        manager.register_subscriber.side_effect = register

        with patch.object(
            portfolio_module,
            "_position_token_ids",
            new=AsyncMock(return_value=[]),
        ):
            gen = portfolio_module._price_stream_frames(
                token_ids=["tok1"],
                wallet_address="0xuser",
                manager=manager,
            )
            collected = []

            async def consume():
                async for frame in gen:
                    collected.append(frame)
                    if len(collected) >= 2:
                        break

            task = asyncio.create_task(consume())
            await self._wait_for(lambda: "handler" in captured)
            now = datetime.now(UTC).isoformat()
            captured["handler"]({"token_id": "tok1", "price": 0.4, "ts": now})
            await self._wait_for(lambda: len(collected) >= 1)
            # Two rapid ticks for the same token coalesce into one
            # frame carrying the latest price.
            captured["handler"]({"token_id": "tok1", "price": 0.41, "ts": now})
            captured["handler"]({"token_id": "tok1", "price": 0.42, "ts": now})
            await asyncio.wait_for(task, timeout=3)
            await gen.aclose()

        self.assertEqual(len(collected), 2)
        second = json.loads(collected[1].removeprefix("data: ").strip())
        self.assertEqual(second["price"], 0.42)

    async def test_stream_drops_stale_ticks(self):
        manager = self._mock_manager(connected=True)
        captured = {}

        def register(handler):
            captured["handler"] = handler
            return 1

        manager.register_subscriber.side_effect = register

        with patch.object(
            portfolio_module,
            "_position_token_ids",
            new=AsyncMock(return_value=[]),
        ):
            gen = portfolio_module._price_stream_frames(
                token_ids=["tok1", "tok2"],
                wallet_address="0xuser",
                manager=manager,
            )
            collected = []

            async def consume():
                async for frame in gen:
                    collected.append(frame)
                    if len(collected) >= 1:
                        break

            task = asyncio.create_task(consume())
            await self._wait_for(lambda: "handler" in captured)
            stale_ts = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
            fresh_ts = datetime.now(UTC).isoformat()
            captured["handler"]({"token_id": "tok1", "price": 0.42, "ts": stale_ts})
            captured["handler"]({"token_id": "tok2", "price": 0.5, "ts": fresh_ts})
            await asyncio.wait_for(task, timeout=3)
            await gen.aclose()

        self.assertEqual(len(collected), 1)
        payload = json.loads(collected[0].removeprefix("data: ").strip())
        self.assertEqual(payload["token_id"], "tok2")

    async def test_stream_falls_back_to_poll_snapshot_when_ws_down(self):
        manager = self._mock_manager(connected=False)

        with (
            patch.object(
                portfolio_module,
                "_position_token_ids",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                portfolio_module,
                "_fetch_token_prices",
                new=AsyncMock(return_value={"tok1": 0.42}),
            ),
        ):
            gen = portfolio_module._price_stream_frames(
                token_ids=["tok1"],
                wallet_address="0xuser",
                manager=manager,
                poll_interval_seconds=0.05,
            )
            collected = []

            async def consume():
                async for frame in gen:
                    collected.append(frame)
                    if len(collected) >= 1:
                        break

            await asyncio.wait_for(consume(), timeout=3)
            await gen.aclose()

        self.assertEqual(len(collected), 1)
        payload = json.loads(collected[0].removeprefix("data: ").strip())
        self.assertEqual(payload["token_id"], "tok1")
        self.assertEqual(payload["price"], 0.42)
        self.assertIn("ts", payload)

    async def test_stream_includes_position_tokens(self):
        manager = self._mock_manager(connected=True)
        manager.subscribe = AsyncMock(return_value=["tok1", "tokPos"])

        with patch.object(
            portfolio_module,
            "_position_token_ids",
            new=AsyncMock(return_value=["tokPos"]),
        ):
            gen = portfolio_module._price_stream_frames(
                token_ids=["tok1"],
                wallet_address="0xuser",
                manager=manager,
                heartbeat_seconds=0.05,
            )
            # Start the generator so it subscribes, then close it.
            await gen.__anext__()
            await gen.aclose()

        manager.subscribe.assert_awaited_once_with(["tok1", "tokPos"])


class PriceStreamEndpointTests(unittest.TestCase):
    """Auth and per-user scale guard on the SSE endpoint."""

    def _client(self):
        from fastapi.testclient import TestClient

        from app.main import app

        return TestClient(app), app

    def test_requires_auth(self):
        client, _ = self._client()
        resp = client.get("/api/portfolio/prices/stream")
        self.assertEqual(resp.status_code, 401)

    def test_per_user_client_cap_returns_429(self):
        from app.api.routes.auth import get_current_user_from_token

        client, app = self._client()
        tracker = portfolio_module.SseClientTracker(max_per_user=0)
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": 1,
            "wallet_address": "0xuser",
            "is_admin": False,
        }
        try:
            with patch.object(portfolio_module, "_sse_clients", tracker):
                resp = client.get("/api/portfolio/prices/stream")
        finally:
            app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 429)

    def test_rejects_too_many_token_ids(self):
        from app.api.routes.auth import get_current_user_from_token

        client, app = self._client()
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": 1,
            "wallet_address": "0xuser",
            "is_admin": False,
        }
        try:
            resp = client.get(
                "/api/portfolio/prices/stream",
                params={"token_ids": ",".join(f"t{i}" for i in range(201))},
            )
        finally:
            app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
