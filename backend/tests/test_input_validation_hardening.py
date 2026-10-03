import asyncio
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("JWT_SECRET_KEY", "a" * 32)

import app.services.arbitrage_service as arbitrage_service
import app.services.backtesting_service as backtesting_service
import app.services.market_maker_service as market_maker_service
from app.models.user import validate_profile_picture_url


class BacktestDateValidationTests(unittest.TestCase):
    def test_rejects_invalid_start_date_format(self):
        with self.assertRaisesRegex(ValueError, "Invalid start_date. Use YYYY-MM-DD"):
            backtesting_service._validate_backtest_date_range("2026/01/01", "2026-01-31")

    def test_rejects_invalid_end_date_format(self):
        with self.assertRaisesRegex(ValueError, "Invalid end_date. Use YYYY-MM-DD"):
            backtesting_service._validate_backtest_date_range("2026-01-01", "01-31-2026")

    def test_rejects_start_after_end(self):
        with self.assertRaisesRegex(ValueError, "start_date must be on or before end_date"):
            backtesting_service._validate_backtest_date_range("2026-02-01", "2026-01-01")


class MarketMakerBandLimitTests(unittest.TestCase):
    @staticmethod
    def _bands_config(num_bands: int) -> SimpleNamespace:
        return SimpleNamespace(
            num_bands=num_bands,
            min_spread=0.01,
            max_spread=0.05,
            band_order_size=10.0,
            min_price=0.01,
            max_price=0.99,
            max_collateral=1000.0,
            token_id_yes="token-yes",
            amm_liquidity=1000.0,
            min_order_size=0.1,
        )

    def test_bands_strategy_rejects_excessive_num_bands(self):
        config = self._bands_config(num_bands=market_maker_service.MAX_BANDS + 1)
        with self.assertRaisesRegex(ValueError, "Max 20 bands allowed"):
            market_maker_service._compute_bands_orders(0.5, config)

    def test_amm_strategy_rejects_excessive_num_bands(self):
        config = self._bands_config(num_bands=market_maker_service.MAX_BANDS + 1)
        with self.assertRaisesRegex(ValueError, "Max 20 bands allowed"):
            market_maker_service._compute_amm_orders(0.5, config)


class ArbitrageRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig_semaphore = arbitrage_service._arb_semaphore
        arbitrage_service._arb_semaphore = asyncio.Semaphore(3)

    async def asyncTearDown(self):
        arbitrage_service._arb_semaphore = self._orig_semaphore

    async def test_fetch_markets_uses_semaphore_limit(self):
        class _FakeService:
            def __init__(self):
                self.in_flight = 0
                self.max_in_flight = 0

            async def get_active_markets(self, limit: int = 60):
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
                await asyncio.sleep(0.01)
                self.in_flight -= 1
                return [{"condition_id": str(limit)}]

        service = _FakeService()
        await asyncio.gather(
            *[arbitrage_service._fetch_markets_with_rate_limit(service) for _ in range(12)]
        )
        self.assertLessEqual(service.max_in_flight, 3)


class ProfilePictureUrlValidationTests(unittest.TestCase):
    def test_rejects_javascript_scheme(self):
        with self.assertRaisesRegex(ValueError, "must use http, https, or data:image/"):
            validate_profile_picture_url("javascript:alert('xss')")

    def test_allows_https_url(self):
        self.assertEqual(
            validate_profile_picture_url("https://example.com/avatar.png"),
            "https://example.com/avatar.png",
        )

    def test_allows_data_image_url(self):
        value = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA"
        self.assertEqual(validate_profile_picture_url(value), value)

    def test_rejects_data_non_image_url(self):
        with self.assertRaisesRegex(ValueError, "data URL must be image/\\*"):
            validate_profile_picture_url("data:text/html;base64,PHNjcmlwdD4=")


if __name__ == "__main__":
    unittest.main()
