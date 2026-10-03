"""Coverage tests for the Binance WebSocket price feed service.

The feed is pure state machinery plus one websocket loop, so every
test drives the module-level ``_state`` directly or fakes the
``websockets.connect`` factory — no real network I/O happens.

Covered:

* ``Kline`` / ``SymbolState.record_kline`` -- open vs closed klines,
  history rotation at ``KLINE_HISTORY_SIZE``.
* URL builders and the ``_to_float`` / ``_to_int`` coercions
  (None, valid, TypeError and ValueError paths).
* ``handle_message`` -- every malformed-frame branch (bad JSON,
  non-dict payloads, missing/non-dict ``data``/``k``, non-kline
  events, unknown symbols, symbol fallback to ``k.s``) plus the
  happy path for open and closed klines.
* ``_ws_listen`` -- message handling, stop-event break inside the
  message loop, reconnect with exponential backoff, the 60s backoff
  cap, connection-failure logging, ``CancelledError`` propagation
  and the break-after-failure path.
* All public accessors -- unknown symbols, empty history, exact and
  fallback window lookups, feed lag.
* Lifecycle -- idempotent start, stop when not running, stop with
  null internals, cancel of a running task, already-finished task
  and a task that raises on cancellation.
"""

import asyncio
import contextlib
import json
import unittest
from unittest.mock import MagicMock, patch

from app.services import binance_ws_service as bs
from app.services.binance_ws_service import (
    KLINE_HISTORY_SIZE,
    Kline,
    SymbolState,
    combined_streams_url,
    get_current_kline,
    get_feed_lag_ms,
    get_kline_at,
    get_last_price,
    get_recent_closes,
    get_symbol_state,
    get_window_close_price,
    get_window_open_price,
    handle_message,
    start_binance_feed,
    stop_binance_feed,
)


def _kline_message(
    symbol="BTCUSDT",
    open_="100.5",
    high="101.0",
    low="99.0",
    close="100.8",
    t=1700000000000,
    T=1700000060000,
    E=1700000059000,
    closed=False,
):
    return json.dumps(
        {
            "data": {
                "e": "kline",
                "E": E,
                "s": symbol,
                "k": {
                    "t": t,
                    "T": T,
                    "o": open_,
                    "h": high,
                    "l": low,
                    "c": close,
                    "x": closed,
                },
            }
        }
    )


def _closed_kline(start_ms, end_ms, close, open_=None, event_time_ms=0):
    return Kline(
        start_ms=start_ms,
        end_ms=end_ms,
        open=open_ if open_ is not None else close,
        high=close,
        low=close,
        close=close,
        event_time_ms=event_time_ms,
        is_closed=True,
    )


class _FakeWebSocket:
    """Async-iterable websocket double that yields canned frames."""

    def __init__(self, messages, on_exhausted=None):
        self._messages = list(messages)
        self._on_exhausted = on_exhausted

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        if self._on_exhausted is not None:
            self._on_exhausted()
        raise StopAsyncIteration


class _FakeConnect:
    """Async context manager double for ``websockets.connect``."""

    def __init__(self, websocket):
        self._websocket = websocket

    async def __aenter__(self):
        return self._websocket

    async def __aexit__(self, exc_type, exc, tb):
        return False


class BinanceServiceTestCase(unittest.IsolatedAsyncioTestCase):
    """Resets the module-level feed state around every test."""

    def setUp(self):
        for state in bs._state.values():
            state.last_price = None
            state.current_kline = None
            state.closed_klines.clear()
            state.last_event_time_ms = 0
            state.last_message_at = 0.0
        bs._running = False
        bs._feed_task = None
        bs._stop_event = None

    async def asyncTearDown(self):
        if bs._feed_task is not None and not bs._feed_task.done():
            bs._feed_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await bs._feed_task
        bs._running = False
        bs._feed_task = None
        bs._stop_event = None
        for state in bs._state.values():
            state.last_price = None
            state.current_kline = None
            state.closed_klines.clear()
            state.last_event_time_ms = 0
            state.last_message_at = 0.0


class KlineAndSymbolStateTests(BinanceServiceTestCase):
    def test_record_open_kline_sets_current_kline(self):
        state = SymbolState(symbol="BTCUSDT")
        kline = Kline(
            start_ms=1,
            end_ms=2,
            open=10.0,
            high=11.0,
            low=9.0,
            close=10.5,
            event_time_ms=3,
            is_closed=False,
        )
        state.record_kline(kline)

        self.assertEqual(state.last_price, 10.5)
        self.assertEqual(state.last_event_time_ms, 3)
        self.assertGreater(state.last_message_at, 0.0)
        self.assertIs(state.current_kline, kline)
        self.assertEqual(len(state.closed_klines), 0)

    def test_record_closed_kline_rotates_into_history(self):
        state = SymbolState(symbol="BTCUSDT")
        kline = Kline(
            start_ms=1,
            end_ms=2,
            open=10.0,
            high=11.0,
            low=9.0,
            close=10.5,
            event_time_ms=3,
            is_closed=True,
        )
        state.record_kline(kline)

        self.assertEqual(state.last_price, 10.5)
        self.assertIsNone(state.current_kline)
        self.assertEqual(list(state.closed_klines), [kline])

    def test_closed_kline_history_is_capped(self):
        state = SymbolState(symbol="BTCUSDT")
        for i in range(KLINE_HISTORY_SIZE + 5):
            state.record_kline(_closed_kline(i, i + 1, float(i)))

        self.assertEqual(len(state.closed_klines), KLINE_HISTORY_SIZE)
        # Oldest klines were evicted, newest retained.
        self.assertEqual(state.closed_klines[0].start_ms, 5)
        self.assertEqual(state.closed_klines[-1].start_ms, KLINE_HISTORY_SIZE + 4)


class UrlAndConversionTests(BinanceServiceTestCase):
    def test_stream_name_lowercases_symbol(self):
        self.assertEqual(bs._stream_name("BTCUSDT"), "btcusdt@kline_1m")

    def test_combined_streams_url_default_symbols(self):
        url = combined_streams_url()
        self.assertTrue(url.startswith(bs.BINANCE_WS_URL + "?streams="))
        for symbol in bs.SYMBOLS:
            self.assertIn(bs._stream_name(symbol), url)

    def test_combined_streams_url_custom_symbols(self):
        url = combined_streams_url(("ETHUSDT", "SOLUSDT"))
        self.assertIn("ethusdt@kline_1m", url)
        self.assertIn("solusdt@kline_1m", url)
        self.assertNotIn("btcusdt", url)

    def test_combined_streams_url_empty_symbols(self):
        self.assertEqual(combined_streams_url(()), f"{bs.BINANCE_WS_URL}?streams=")

    def test_to_float(self):
        self.assertEqual(bs._to_float(None), 0.0)
        self.assertEqual(bs._to_float(None, 9.5), 9.5)
        self.assertEqual(bs._to_float("1.5"), 1.5)
        self.assertEqual(bs._to_float(3), 3.0)
        self.assertEqual(bs._to_float("abc"), 0.0)
        self.assertEqual(bs._to_float(["x"]), 0.0)  # TypeError path

    def test_to_int(self):
        self.assertEqual(bs._to_int(None), 0)
        self.assertEqual(bs._to_int(None, 7), 7)
        self.assertEqual(bs._to_int("42"), 42)
        self.assertEqual(bs._to_int(7), 7)
        self.assertEqual(bs._to_int("x"), 0)
        self.assertEqual(bs._to_int({}), 0)  # TypeError path


class HandleMessageTests(BinanceServiceTestCase):
    def test_unparseable_frames_return_none(self):
        self.assertIsNone(handle_message("not json"))
        self.assertIsNone(handle_message(""))
        self.assertIsNone(handle_message(None))  # TypeError path
        self.assertIsNone(handle_message(b"\xff\xfe\xfa"))  # UnicodeDecodeError

    def test_non_dict_payloads_return_none(self):
        self.assertIsNone(handle_message(json.dumps([1, 2, 3])))
        self.assertIsNone(handle_message(json.dumps("hello")))
        self.assertIsNone(handle_message(json.dumps(None)))
        self.assertIsNone(handle_message(json.dumps(42)))

    def test_missing_or_invalid_data_returns_none(self):
        self.assertIsNone(handle_message(json.dumps({})))
        self.assertIsNone(handle_message(json.dumps({"data": "not-a-dict"})))
        self.assertIsNone(handle_message(json.dumps({"data": None})))

    def test_non_kline_event_returns_none(self):
        payload = json.dumps({"data": {"e": "depthUpdate", "s": "BTCUSDT"}})
        self.assertIsNone(handle_message(payload))

    def test_missing_or_invalid_kline_returns_none(self):
        self.assertIsNone(handle_message(json.dumps({"data": {"e": "kline", "s": "BTCUSDT"}})))
        self.assertIsNone(
            handle_message(json.dumps({"data": {"e": "kline", "s": "BTCUSDT", "k": "x"}}))
        )

    def test_unknown_symbol_returns_none(self):
        self.assertIsNone(handle_message(_kline_message(symbol="DOGEUSDT")))

    def test_open_kline_updates_state(self):
        state = handle_message(_kline_message())

        self.assertIsNotNone(state)
        self.assertIs(state, bs._state["BTCUSDT"])
        self.assertEqual(state.last_price, 100.8)
        self.assertEqual(state.last_event_time_ms, 1700000059000)
        self.assertGreater(state.last_message_at, 0.0)
        current = state.current_kline
        self.assertIsNotNone(current)
        self.assertFalse(current.is_closed)
        self.assertEqual(current.start_ms, 1700000000000)
        self.assertEqual(current.end_ms, 1700000060000)
        self.assertEqual(current.open, 100.5)
        self.assertEqual(current.high, 101.0)
        self.assertEqual(current.low, 99.0)
        self.assertEqual(current.close, 100.8)

    def test_closed_kline_moves_to_history(self):
        state = handle_message(_kline_message(closed=True))

        self.assertEqual(len(state.closed_klines), 1)
        self.assertIsNone(state.current_kline)
        self.assertEqual(state.closed_klines[0].close, 100.8)

    def test_symbol_falls_back_to_kline_symbol(self):
        payload = json.dumps(
            {
                "data": {
                    "e": "kline",
                    "E": 1,
                    "k": {
                        "s": "ETHUSDT",
                        "t": 1,
                        "T": 2,
                        "o": "1",
                        "h": "2",
                        "l": "1",
                        "c": "2",
                        "x": True,
                    },
                }
            }
        )
        state = handle_message(payload)
        self.assertIs(state, bs._state["ETHUSDT"])
        self.assertEqual(state.last_price, 2.0)

    def test_missing_numeric_fields_default_to_zero(self):
        payload = json.dumps({"data": {"e": "kline", "E": None, "s": "SOLUSDT", "k": {}}})
        state = handle_message(payload)
        self.assertIs(state, bs._state["SOLUSDT"])
        kline = state.current_kline
        self.assertEqual(kline.start_ms, 0)
        self.assertEqual(kline.end_ms, 0)
        self.assertEqual(kline.open, 0.0)
        self.assertEqual(kline.high, 0.0)
        self.assertEqual(kline.low, 0.0)
        self.assertEqual(kline.close, 0.0)
        self.assertEqual(state.last_event_time_ms, 0)

    def test_bytes_frame_is_accepted(self):
        state = handle_message(_kline_message().encode("utf-8"))
        self.assertIsNotNone(state)
        self.assertEqual(state.last_price, 100.8)


class WsListenTests(BinanceServiceTestCase):
    def _patch_websockets(self, websocket=None, connect_error=None):
        mock_ws = MagicMock()
        if connect_error is not None:
            mock_ws.connect.side_effect = connect_error
        else:
            mock_ws.connect.return_value = _FakeConnect(websocket)
        patcher = patch.object(bs, "websockets", mock_ws)
        patcher.start()
        self.addCleanup(patcher.stop)
        return mock_ws

    async def test_listen_handles_messages_then_stops(self):
        stop_event = asyncio.Event()
        ws = _FakeWebSocket(
            [_kline_message(), _kline_message(symbol="ETHUSDT", close="2000.0")],
            on_exhausted=stop_event.set,
        )
        mock_ws = self._patch_websockets(websocket=ws)

        await bs._ws_listen(stop_event)

        mock_ws.connect.assert_called_once_with(
            bs.combined_streams_url(),
            ping_interval=25,
            ping_timeout=20,
            close_timeout=5,
            additional_headers={"User-Agent": "polymarket-ai/1.0"},
        )
        self.assertEqual(bs.get_last_price("BTCUSDT"), 100.8)
        self.assertEqual(bs.get_last_price("ETHUSDT"), 2000.0)

    async def test_listen_breaks_when_stop_set_between_messages(self):
        stop_event = asyncio.Event()

        class _StopAfterFirst(_FakeWebSocket):
            async def __anext__(self):
                if self._messages:
                    stop_event.set()
                    return self._messages.pop(0)
                raise StopAsyncIteration

        ws = _StopAfterFirst([_kline_message()])
        self._patch_websockets(websocket=ws)

        await bs._ws_listen(stop_event)

        # The message was received but never processed.
        self.assertIsNone(bs.get_last_price("BTCUSDT"))

    async def test_listen_reconnects_with_doubling_backoff(self):
        stop_event = asyncio.Event()
        delays = []

        async def fake_sleep(delay):
            delays.append(delay)

        ws = _FakeWebSocket([_kline_message()], on_exhausted=stop_event.set)
        mock_ws = self._patch_websockets(websocket=ws)
        # First two connection attempts fail, third succeeds.
        mock_ws.connect.side_effect = [OSError("drop 1"), OSError("drop 2"), _FakeConnect(ws)]

        sleep_patcher = patch.object(bs.asyncio, "sleep", fake_sleep)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

        await bs._ws_listen(stop_event)

        self.assertEqual(delays, [1.0, 2.0])
        self.assertEqual(mock_ws.connect.call_count, 3)
        self.assertEqual(bs.get_last_price("BTCUSDT"), 100.8)

    async def test_listen_backoff_is_capped(self):
        stop_event = asyncio.Event()
        delays = []

        async def fake_sleep(delay):
            delays.append(delay)
            if len(delays) >= 8:
                stop_event.set()

        self._patch_websockets(connect_error=OSError("always down"))

        sleep_patcher = patch.object(bs.asyncio, "sleep", fake_sleep)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

        await bs._ws_listen(stop_event)

        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0])

    async def test_listen_logs_connection_failure_and_stops(self):
        stop_event = asyncio.Event()

        async def fake_sleep(delay):
            stop_event.set()

        self._patch_websockets(connect_error=OSError("refused"))

        sleep_patcher = patch.object(bs.asyncio, "sleep", fake_sleep)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

        await bs._ws_listen(stop_event)  # must not raise

    async def test_listen_propagates_cancellation(self):
        stop_event = asyncio.Event()
        self._patch_websockets(connect_error=asyncio.CancelledError())

        with self.assertRaises(asyncio.CancelledError):
            await bs._ws_listen(stop_event)

    async def test_listen_returns_immediately_when_already_stopped(self):
        stop_event = asyncio.Event()
        stop_event.set()
        mock_ws = self._patch_websockets(websocket=_FakeWebSocket([]))

        await bs._ws_listen(stop_event)

        mock_ws.connect.assert_not_called()


class AccessorTests(BinanceServiceTestCase):
    def _seed(self):
        state = bs._state["BTCUSDT"]
        state.record_kline(_closed_kline(1000, 2000, 100.0, open_=99.0, event_time_ms=1500))
        state.record_kline(_closed_kline(3000, 4000, 200.0, open_=199.0, event_time_ms=3500))
        state.record_kline(
            Kline(
                start_ms=5000,
                end_ms=6000,
                open=201.0,
                high=202.0,
                low=200.0,
                close=201.5,
                event_time_ms=5500,
                is_closed=False,
            )
        )
        return state

    def test_get_symbol_state_unknown_symbol(self):
        self.assertIsNone(get_symbol_state("DOGEUSDT"))

    def test_get_symbol_state_with_current_kline(self):
        self._seed()
        snapshot = get_symbol_state("btcusdt")  # case-insensitive lookup

        self.assertEqual(snapshot["symbol"], "BTCUSDT")
        self.assertEqual(snapshot["last_price"], 201.5)
        self.assertEqual(snapshot["closed_klines"], 2)
        self.assertEqual(snapshot["last_event_time_ms"], 5500)
        self.assertGreater(snapshot["last_message_at"], 0.0)
        current = snapshot["current_kline"]
        self.assertEqual(
            current,
            {
                "open": 201.0,
                "high": 202.0,
                "low": 200.0,
                "close": 201.5,
                "start_ms": 5000,
                "end_ms": 6000,
                "is_closed": False,
            },
        )

    def test_get_symbol_state_without_current_kline(self):
        state = bs._state["BTCUSDT"]
        state.record_kline(_closed_kline(1000, 2000, 100.0))
        snapshot = get_symbol_state("BTCUSDT")
        self.assertIsNone(snapshot["current_kline"])

    def test_get_last_price(self):
        self.assertIsNone(get_last_price("BTCUSDT"))
        self.assertIsNone(get_last_price("DOGEUSDT"))
        self._seed()
        self.assertEqual(get_last_price("btcusdt"), 201.5)

    def test_get_current_kline(self):
        self.assertIsNone(get_current_kline("BTCUSDT"))
        self.assertIsNone(get_current_kline("DOGEUSDT"))
        self._seed()
        kline = get_current_kline("BTCUSDT")
        self.assertIsNotNone(kline)
        self.assertEqual(kline.close, 201.5)

    def test_get_recent_closes(self):
        self.assertEqual(get_recent_closes("DOGEUSDT"), [])
        self._seed()
        self.assertEqual(get_recent_closes("BTCUSDT"), [100.0, 200.0])
        self.assertEqual(get_recent_closes("BTCUSDT", count=1), [200.0])
        # NOTE: count<=0 yields the full history because
        # [-max(count, 0):] degenerates to [0:] when count==0.
        self.assertEqual(get_recent_closes("BTCUSDT", count=0), [100.0, 200.0])
        self.assertEqual(get_recent_closes("BTCUSDT", count=-3), [100.0, 200.0])
        self.assertEqual(get_recent_closes("BTCUSDT", count=99), [100.0, 200.0])

    def test_get_kline_at(self):
        self.assertIsNone(get_kline_at("DOGEUSDT", 1000))
        self._seed()
        exact = get_kline_at("BTCUSDT", 3000)
        self.assertIsNotNone(exact)
        self.assertEqual(exact.start_ms, 3000)
        self.assertEqual(exact.close, 200.0)
        # No kline starts at 2500; the scan breaks once it passes it.
        self.assertIsNone(get_kline_at("BTCUSDT", 2500))
        self.assertIsNone(get_kline_at("BTCUSDT", 9999))

    def test_get_window_open_price_exact_match(self):
        self._seed()
        # Kline starting at 3000 exists -> its open price.
        self.assertEqual(get_window_open_price("BTCUSDT", 3), 199.0)

    def test_get_window_open_price_falls_back_to_predating_close(self):
        self._seed()
        # No kline starts at 4000; fall back to the close of the
        # latest kline that predates the window (start 3000).
        self.assertEqual(get_window_open_price("BTCUSDT", 4), 200.0)

    def test_get_window_open_price_skips_klines_after_window(self):
        self._seed()
        # Window starts at 2000: the newest closed kline (start
        # 3000) postdates it, so the fallback scan continues past
        # it and lands on the kline starting at 1000.
        self.assertEqual(get_window_open_price("BTCUSDT", 2), 100.0)

    def test_get_window_open_price_without_history(self):
        self.assertIsNone(get_window_open_price("BTCUSDT", 1))
        self.assertIsNone(get_window_open_price("DOGEUSDT", 1))

    def test_get_window_close_price(self):
        self._seed()
        self.assertEqual(get_window_close_price("BTCUSDT", 2), 100.0)  # end_ms 2000
        self.assertEqual(get_window_close_price("BTCUSDT", 4), 200.0)  # end_ms 4000
        # end_ms 5000 is past every closed kline -> scan breaks -> None.
        self.assertIsNone(get_window_close_price("BTCUSDT", 5))
        self.assertIsNone(get_window_close_price("DOGEUSDT", 2))

    def test_get_window_close_price_without_history(self):
        self.assertIsNone(get_window_close_price("BTCUSDT", 1))

    def test_get_feed_lag_ms(self):
        self.assertIsNone(get_feed_lag_ms("DOGEUSDT"))
        # No event time recorded yet.
        self.assertIsNone(get_feed_lag_ms("BTCUSDT"))

        state = bs._state["BTCUSDT"]
        now_ms = bs.datetime.now(bs.UTC).timestamp() * 1000.0
        state.last_event_time_ms = int(now_ms - 5000)
        lag = get_feed_lag_ms("BTCUSDT")
        self.assertIsNotNone(lag)
        self.assertGreaterEqual(lag, 0.0)
        self.assertLess(lag, 10000.0)

        # Future event time clamps to zero.
        state.last_event_time_ms = int(now_ms + 60000)
        self.assertEqual(get_feed_lag_ms("BTCUSDT"), 0.0)


class LifecycleTests(BinanceServiceTestCase):
    async def test_start_is_idempotent(self):
        async def blocker(stop_event):
            await asyncio.sleep(3600)

        with patch.object(bs, "_ws_listen", blocker):
            await start_binance_feed()
            self.assertTrue(bs._running)
            self.assertIsNotNone(bs._feed_task)
            first_task = bs._feed_task

            await asyncio.sleep(0)  # let the task start
            await start_binance_feed()  # second call is a no-op
            self.assertIs(bs._feed_task, first_task)

            await stop_binance_feed()
            self.assertFalse(bs._running)
            self.assertIsNone(bs._feed_task)
            self.assertIsNone(bs._stop_event)

    async def test_stop_when_not_running(self):
        await stop_binance_feed()  # no-op, must not raise
        self.assertFalse(bs._running)

    async def test_stop_with_null_internals(self):
        bs._running = True
        bs._stop_event = None
        bs._feed_task = None
        await stop_binance_feed()
        self.assertFalse(bs._running)
        self.assertIsNone(bs._feed_task)
        self.assertIsNone(bs._stop_event)

    async def test_stop_cancels_running_task(self):
        async def blocker(stop_event):
            await asyncio.sleep(3600)

        with patch.object(bs, "_ws_listen", blocker):
            await start_binance_feed()
            await asyncio.sleep(0)  # let the task enter its sleep
            self.assertFalse(bs._feed_task.done())

            await stop_binance_feed()

            self.assertTrue(bs._feed_task is None)
            self.assertFalse(bs._running)

    async def test_stop_when_task_already_finished(self):
        async def quick(stop_event):
            return None

        with patch.object(bs, "_ws_listen", quick):
            await start_binance_feed()
            await asyncio.sleep(0)  # let the task run to completion
            self.assertTrue(bs._feed_task.done())

            await stop_binance_feed()  # skips the cancel path

            self.assertFalse(bs._running)

    async def test_stop_swallows_task_exception(self):
        async def exploding(stop_event):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise RuntimeError("boom") from None

        with patch.object(bs, "_ws_listen", exploding):
            await start_binance_feed()
            await asyncio.sleep(0)  # let the task start

            await stop_binance_feed()  # must not raise

            self.assertFalse(bs._running)


if __name__ == "__main__":
    unittest.main()
