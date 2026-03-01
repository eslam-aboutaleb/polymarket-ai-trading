import importlib
import os
import unittest
from unittest.mock import AsyncMock, patch

from app.config import get_settings


class AnalysisClientPoolingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {
                "JWT_SECRET_KEY": "a" * 32,
            },
            clear=False,
        )
        self.env_patcher.start()
        get_settings.cache_clear()
        import app.grpc_clients.analysis_client as analysis_client_module

        self.analysis_client = importlib.reload(analysis_client_module)
        await self.analysis_client.close_shared_channels()

    async def asyncTearDown(self):
        await self.analysis_client.close_shared_channels()
        self.env_patcher.stop()
        get_settings.cache_clear()

    async def test_shared_channel_reused_for_same_service_address(self):
        fake_channel = AsyncMock()
        fake_channel.close = AsyncMock()

        with patch.object(
            self.analysis_client.grpc.aio,
            "insecure_channel",
            return_value=fake_channel,
        ) as channel_factory, patch(
            "app.grpc_clients.analysis_pb2_grpc.AnalysisServiceStub",
            side_effect=lambda channel: {"channel": channel},
        ) as stub_factory:
            client_one = self.analysis_client.AnalysisClient()
            client_two = self.analysis_client.AnalysisClient()

            stub_one = await client_one._get_stub()
            stub_two = await client_two._get_stub()

            self.assertIs(stub_one, stub_two)
            self.assertEqual(channel_factory.call_count, 1)
            self.assertEqual(stub_factory.call_count, 1)

        await self.analysis_client.close_shared_channels()
        fake_channel.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
