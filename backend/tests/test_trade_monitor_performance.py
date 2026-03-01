import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import app.services.trade_monitor as trade_monitor


class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self):
        self.calls = 0

    async def get(self, *_args, **_kwargs):
        self.calls += 1
        return _FakeResponse([])


class TradeMonitorPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        trade_monitor._watched_wallets = set()
        trade_monitor._last_seen_trades = {}
        trade_monitor._poll_http_client = None
        trade_monitor._poll_semaphore = asyncio.Semaphore(50)

    async def test_http_poll_cycle_honors_semaphore_limit(self):
        max_parallel = 0
        in_flight = 0

        async def fake_poll(_wallet: str):
            nonlocal in_flight, max_parallel
            in_flight += 1
            max_parallel = max(max_parallel, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return []

        trade_monitor._watched_wallets = {f"wallet-{i}" for i in range(15)}
        trade_monitor._poll_semaphore = asyncio.Semaphore(3)

        with patch.object(trade_monitor, "_poll_trader_trades", side_effect=fake_poll), patch.object(
            trade_monitor, "_detect_new_trades", new=AsyncMock(return_value=[])
        ), patch.object(trade_monitor, "_process_new_trade", new=AsyncMock()):
            await trade_monitor._http_poll_cycle()

        self.assertLessEqual(max_parallel, 3)

    async def test_poll_trader_trades_reuses_shared_http_client(self):
        fake_client = _FakeClient()
        trade_monitor._poll_http_client = fake_client

        with patch("app.services.trade_monitor.httpx.AsyncClient", side_effect=AssertionError("unexpected new client")):
            result_one = await trade_monitor._poll_trader_trades("0xabc")
            result_two = await trade_monitor._poll_trader_trades("0xdef")

        self.assertEqual(result_one, [])
        self.assertEqual(result_two, [])
        self.assertEqual(fake_client.calls, 2)


if __name__ == "__main__":
    unittest.main()
