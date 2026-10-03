"""Coverage tests for app/api/routes/portfolio.py.

Exercises every route handler and the module's helper functions
(_fetch_token_prices, _position_token_ids, _sse_frame,
_attempt_response, _price_stream_frames). The Polymarket service,
the credential store and the CLOB WebSocket manager are mocked;
the SQLite session is real so the redemption history endpoint is
exercised end to end.
"""

import asyncio
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.routes.portfolio as portfolio_module
from app.api.routes.auth import get_current_user_from_token
from app.api.routes.portfolio import (
    RedemptionAttemptResponse,
    _attempt_response,
    _sse_frame,
)
from app.main import app
from app.models.redemption_attempt import RedemptionAttempt
from app.models.user import User
from app.security.credential_store import CredentialStoreError
from app.utils.database import get_db
from app.utils.time import utc_now

WALLET = "0x" + "ab" * 20
PRIVATE_KEY = "0x" + "11" * 32
CLOB_CREDS = {"api_key": "k", "api_secret": "s", "api_passphrase": "p"}


class PortfolioRouteTestBase(unittest.TestCase):
    """Shared fixtures: in-memory DB, auth override, TestClient."""

    def setUp(self):
        # StaticPool + check_same_thread=False: TestClient runs sync
        # dependencies in a threadpool, so the in-memory connection must
        # be shareable across threads.
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        User.__table__.create(self.engine)
        RedemptionAttempt.__table__.create(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, class_=Session, expire_on_commit=False
        )
        self.db = self.session_factory()

        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": 1,
            "wallet_address": WALLET,
            "is_admin": False,
        }
        app.dependency_overrides[get_db] = self._get_db_override

        self.client = TestClient(app)
        self.addCleanup(self._cleanup)

    def _get_db_override(self):
        try:
            yield self.db
        except Exception:
            self.db.rollback()
            raise

    def _cleanup(self):
        app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def _service(self) -> MagicMock:
        service = MagicMock()
        service.get_wallet_balance = AsyncMock()
        service.get_positions = AsyncMock()
        service.get_portfolio_summary = AsyncMock()
        service.get_active_markets = AsyncMock()
        service.get_newest_markets = AsyncMock()
        service.search_all_markets = AsyncMock()
        service._get_clob_client = MagicMock()
        service._to_float = MagicMock(
            side_effect=lambda value, default=0.0: float(value) if value is not None else default
        )
        return service


class BalanceRouteTests(PortfolioRouteTestBase):
    def test_get_balance_returns_balances(self):
        service = self._service()
        service.get_wallet_balance = AsyncMock(
            return_value={
                "wallet_address": WALLET,
                "usdc_balance": 123.45,
                "matic_balance": 0.5,
            }
        )
        with (
            patch.object(portfolio_module, "get_polymarket_service", return_value=service),
            patch.object(
                portfolio_module,
                "load_wallet_credentials",
                return_value={"private_key": PRIVATE_KEY, "clob_creds": CLOB_CREDS},
            ),
        ):
            resp = self.client.get("/api/portfolio/balance")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["wallet_address"], WALLET)
        self.assertEqual(data["usdc_balance"], 123.45)
        self.assertEqual(data["matic_balance"], 0.5)
        self.assertEqual(data["chain"], "polygon")
        service.get_wallet_balance.assert_awaited_once_with(
            WALLET, private_key=PRIVATE_KEY, clob_creds=CLOB_CREDS
        )

    def test_get_balance_without_stored_credentials(self):
        service = self._service()
        service.get_wallet_balance = AsyncMock(
            return_value={"wallet_address": WALLET, "usdc_balance": 0.0, "matic_balance": 0.0}
        )
        with (
            patch.object(portfolio_module, "get_polymarket_service", return_value=service),
            patch.object(portfolio_module, "load_wallet_credentials", return_value=None),
        ):
            resp = self.client.get("/api/portfolio/balance")

        self.assertEqual(resp.status_code, 200)
        service.get_wallet_balance.assert_awaited_once_with(
            WALLET, private_key=None, clob_creds=None
        )

    def test_get_balance_credential_store_error_falls_back_to_none(self):
        service = self._service()
        service.get_wallet_balance = AsyncMock(
            return_value={"wallet_address": WALLET, "usdc_balance": 1.0, "matic_balance": 0.0}
        )
        with (
            patch.object(portfolio_module, "get_polymarket_service", return_value=service),
            patch.object(
                portfolio_module,
                "load_wallet_credentials",
                side_effect=CredentialStoreError("encryption not configured"),
            ),
        ):
            resp = self.client.get("/api/portfolio/balance")

        self.assertEqual(resp.status_code, 200)
        service.get_wallet_balance.assert_awaited_once_with(
            WALLET, private_key=None, clob_creds=None
        )

    def test_get_balance_requires_authentication(self):
        app.dependency_overrides.clear()
        resp = self.client.get("/api/portfolio/balance")
        self.assertEqual(resp.status_code, 401)


class PositionsRouteTests(PortfolioRouteTestBase):
    def test_get_positions_returns_positions(self):
        service = self._service()
        service.get_positions = AsyncMock(
            return_value=[
                {"market": "m1", "size": 10.0},
                {"market": "m2", "size": 20.0},
            ]
        )
        with (
            patch.object(portfolio_module, "get_polymarket_service", return_value=service),
            patch.object(
                portfolio_module,
                "load_wallet_credentials",
                return_value={"private_key": PRIVATE_KEY, "clob_creds": None},
            ),
        ):
            resp = self.client.get("/api/portfolio/positions")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["wallet_address"], WALLET)
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["positions"]), 2)
        service.get_positions.assert_awaited_once_with(
            WALLET, private_key=PRIVATE_KEY, clob_creds=None
        )

    def test_get_positions_credential_store_error_falls_back_to_none(self):
        service = self._service()
        service.get_positions = AsyncMock(return_value=[])
        with (
            patch.object(portfolio_module, "get_polymarket_service", return_value=service),
            patch.object(
                portfolio_module,
                "load_wallet_credentials",
                side_effect=CredentialStoreError("encryption not configured"),
            ),
        ):
            resp = self.client.get("/api/portfolio/positions")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 0)


class PortfolioSummaryRouteTests(PortfolioRouteTestBase):
    def test_get_portfolio_summary(self):
        service = self._service()
        service.get_portfolio_summary = AsyncMock(
            return_value={
                "wallet_address": WALLET,
                "usdc_balance": 10.0,
                "matic_balance": 1.0,
                "active_positions": 2,
                "total_positions": 3,
                "total_invested": 100.0,
                "total_current_value": 110.0,
                "total_pnl": 10.0,
                "pnl_percentage": 10.0,
                "win_rate": 50.0,
                "positions": [{"market": "m1"}],
            }
        )
        with (
            patch.object(portfolio_module, "get_polymarket_service", return_value=service),
            patch.object(
                portfolio_module,
                "load_wallet_credentials",
                return_value={"private_key": PRIVATE_KEY, "clob_creds": CLOB_CREDS},
            ),
        ):
            resp = self.client.get("/api/portfolio/summary")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["wallet_address"], WALLET)
        self.assertEqual(data["active_positions"], 2)
        self.assertEqual(data["total_pnl"], 10.0)
        service.get_portfolio_summary.assert_awaited_once_with(
            WALLET, private_key=PRIVATE_KEY, clob_creds=CLOB_CREDS
        )

    def test_get_portfolio_summary_credential_store_error(self):
        service = self._service()
        service.get_portfolio_summary = AsyncMock(return_value={"wallet_address": WALLET})
        with (
            patch.object(portfolio_module, "get_polymarket_service", return_value=service),
            patch.object(
                portfolio_module,
                "load_wallet_credentials",
                side_effect=CredentialStoreError("encryption not configured"),
            ),
        ):
            resp = self.client.get("/api/portfolio/summary")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["wallet_address"], WALLET)


class MarketsRouteTests(PortfolioRouteTestBase):
    def test_get_active_markets(self):
        service = self._service()
        service.get_active_markets = AsyncMock(
            return_value=[{"id": "1", "question": "Q1?"}, {"id": "2", "question": "Q2?"}]
        )
        with patch.object(portfolio_module, "get_polymarket_service", return_value=service):
            resp = self.client.get("/api/portfolio/markets", params={"limit": 5})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["markets"]), 2)
        service.get_active_markets.assert_awaited_once_with(limit=5)

    def test_get_active_markets_default_limit(self):
        service = self._service()
        service.get_active_markets = AsyncMock(return_value=[])
        with patch.object(portfolio_module, "get_polymarket_service", return_value=service):
            resp = self.client.get("/api/portfolio/markets")

        self.assertEqual(resp.status_code, 200)
        service.get_active_markets.assert_awaited_once_with(limit=10)

    def test_get_newest_markets_is_public(self):
        service = self._service()
        service.get_newest_markets = AsyncMock(
            return_value=[{"id": "n1"}, {"id": "n2"}, {"id": "n3"}]
        )
        app.dependency_overrides.clear()
        with patch.object(portfolio_module, "get_polymarket_service", return_value=service):
            resp = self.client.get("/api/portfolio/markets/newest", params={"limit": 3})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 3)
        service.get_newest_markets.assert_awaited_once_with(limit=3)

    def test_get_combined_markets(self):
        service = self._service()
        service.search_all_markets = AsyncMock(
            return_value={
                "markets": [{"id": "a"}, {"id": "b"}],
                "total": 100,
                "offset": 10,
                "has_more": True,
            }
        )
        app.dependency_overrides.clear()
        with patch.object(portfolio_module, "get_polymarket_service", return_value=service):
            resp = self.client.get(
                "/api/portfolio/markets/combined", params={"limit": 2, "offset": 10}
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["total"], 100)
        self.assertEqual(data["offset"], 10)
        self.assertTrue(data["has_more"])
        # Each market is tagged with its source.
        self.assertEqual(data["markets"][0], {"id": "a", "_source": "all_markets"})
        service.search_all_markets.assert_awaited_once_with(
            query="", tag="", limit=2, offset=10, sort="volume24hr"
        )

    def test_get_combined_markets_defaults_for_missing_keys(self):
        service = self._service()
        service.search_all_markets = AsyncMock(return_value={})
        app.dependency_overrides.clear()
        with patch.object(portfolio_module, "get_polymarket_service", return_value=service):
            resp = self.client.get("/api/portfolio/markets/combined")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["markets"], [])
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["offset"], 0)
        self.assertFalse(data["has_more"])

    def test_get_market_prices_maps_slim_fields(self):
        service = self._service()
        service.get_active_markets = AsyncMock(
            return_value=[
                {
                    "condition_id": "cid-1",
                    "question": "Q1?",
                    "outcomePrices": "0.5,0.5",
                    "bestAsk": "0.5",
                    "bestBid": "0.4",
                    "lastTradePrice": "0.45",
                    "volume24hr": 100.0,
                    "liquidity": 50.0,
                    "slug": "q1",
                },
                # No condition_id: falls back to id.
                {"id": "id-2", "question": "Q2?"},
                # Neither condition_id nor id: falls back to question.
                {"question": "Q3?"},
            ]
        )
        app.dependency_overrides.clear()
        with patch.object(portfolio_module, "get_polymarket_service", return_value=service):
            resp = self.client.get("/api/portfolio/markets/prices", params={"limit": 3})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["markets"]), 3)
        self.assertEqual(data["markets"][0]["id"], "cid-1")
        self.assertEqual(data["markets"][0]["question"], "Q1?")
        self.assertEqual(data["markets"][0]["outcomePrices"], "0.5,0.5")
        self.assertEqual(data["markets"][0]["volume24hr"], 100.0)
        self.assertEqual(data["markets"][0]["liquidity"], 50.0)
        self.assertEqual(data["markets"][0]["slug"], "q1")
        self.assertEqual(data["markets"][1]["id"], "id-2")
        self.assertEqual(data["markets"][2]["id"], "Q3?")
        self.assertIn("ts", data)
        service.get_active_markets.assert_awaited_once_with(limit=3)


class PositionPricesRouteTests(PortfolioRouteTestBase):
    def test_get_position_prices(self):
        with patch.object(
            portfolio_module,
            "_fetch_token_prices",
            new=AsyncMock(return_value={"tok1": 0.42, "tok2": 0.31}),
        ):
            resp = self.client.get("/api/portfolio/positions/prices")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["prices"], {"tok1": 0.42, "tok2": 0.31})
        self.assertIn("ts", data)

    def test_get_position_prices_error_is_reported(self):
        with patch.object(
            portfolio_module,
            "_fetch_token_prices",
            new=AsyncMock(side_effect=RuntimeError("price feed down")),
        ):
            resp = self.client.get("/api/portfolio/positions/prices")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["prices"], {})
        self.assertEqual(data["error"], "price feed down")
        self.assertIn("ts", data)


class PriceStreamRouteTests(PortfolioRouteTestBase):
    def test_stream_prices_returns_sse_response(self):
        async def fake_frames(token_ids, wallet_address, **kwargs):
            yield 'data: {"token_id": "t1", "price": 0.42}\n\n'

        manager = MagicMock()
        with (
            patch.object(portfolio_module, "_price_stream_frames", new=fake_frames),
            patch.object(portfolio_module, "get_clob_ws_manager", return_value=manager),
        ):
            resp = self.client.get("/api/portfolio/prices/stream", params={"token_ids": "t1,t2"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"].split(";")[0], "text/event-stream")
        self.assertEqual(resp.headers["cache-control"], "no-cache")
        self.assertEqual(resp.headers["connection"], "keep-alive")
        self.assertEqual(resp.headers["x-accel-buffering"], "no")
        manager.start.assert_called_once()
        self.assertEqual(resp.text, 'data: {"token_id": "t1", "price": 0.42}\n\n')

    def test_stream_prices_requires_authentication(self):
        app.dependency_overrides.clear()
        resp = self.client.get("/api/portfolio/prices/stream")
        self.assertEqual(resp.status_code, 401)


class RedeemRouteTests(PortfolioRouteTestBase):
    def _attempt(self):
        attempt = MagicMock()
        attempt.id = 1
        attempt.user_id = 1
        attempt.market_id = "market-1"
        attempt.collection_id = "0x" + "ab" * 32
        attempt.tx_hash = "0x" + "cd" * 32
        attempt.status = "confirmed"
        attempt.amount = 5.0
        attempt.error = None
        attempt.created_at = utc_now()
        return attempt

    def test_redeem_success(self):
        attempt = self._attempt()
        redeem_mock = AsyncMock(return_value=attempt)
        with patch("app.services.redemption_service.redeem_market_for_user", new=redeem_mock):
            resp = self.client.post("/api/portfolio/redeem", json={"market_id": "market-1"})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], 1)
        self.assertEqual(data["user_id"], 1)
        self.assertEqual(data["market_id"], "market-1")
        self.assertEqual(data["collection_id"], "0x" + "ab" * 32)
        self.assertEqual(data["tx_hash"], "0x" + "cd" * 32)
        self.assertEqual(data["status"], "confirmed")
        self.assertEqual(data["amount"], 5.0)
        self.assertIsNone(data["error"])
        self.assertIn("created_at", data)
        redeem_mock.assert_awaited_once_with(
            user_id=1,
            wallet_address=WALLET,
            market_id="market-1",
            winner_outcome_index=None,
        )

    def test_redeem_with_winner_outcome_index(self):
        attempt = self._attempt()
        redeem_mock = AsyncMock(return_value=attempt)
        with patch("app.services.redemption_service.redeem_market_for_user", new=redeem_mock):
            resp = self.client.post(
                "/api/portfolio/redeem",
                json={"market_id": "market-1", "winner_outcome_index": 1},
            )

        self.assertEqual(resp.status_code, 200)
        redeem_mock.assert_awaited_once_with(
            user_id=1,
            wallet_address=WALLET,
            market_id="market-1",
            winner_outcome_index=1,
        )

    def test_redeem_no_redeemable_position_is_404(self):
        redeem_mock = AsyncMock(return_value=None)
        with patch("app.services.redemption_service.redeem_market_for_user", new=redeem_mock):
            resp = self.client.post("/api/portfolio/redeem", json={"market_id": "market-1"})

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"], "No redeemable position found for this market")

    def test_redeem_validation_error_is_422(self):
        resp = self.client.post("/api/portfolio/redeem", json={})
        self.assertEqual(resp.status_code, 422)

    def test_redeem_requires_authentication(self):
        app.dependency_overrides.clear()
        resp = self.client.post("/api/portfolio/redeem", json={"market_id": "m"})
        self.assertEqual(resp.status_code, 401)


class RedemptionsRouteTests(PortfolioRouteTestBase):
    def _seed_attempts(self, count: int = 3):
        for i in range(count):
            self.db.add(
                RedemptionAttempt(
                    user_id=1,
                    market_id=f"market-{i}",
                    collection_id="0x" + "ab" * 32,
                    tx_hash="0x" + "cd" * 32,
                    status="confirmed",
                    amount=10.0 + i,
                    error=None,
                    created_at=utc_now() - timedelta(minutes=i),
                )
            )
        self.db.commit()

    def test_get_redemptions_returns_history_newest_first(self):
        self._seed_attempts(3)
        resp = self.client.get("/api/portfolio/redemptions")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 3)
        self.assertEqual(data[0]["market_id"], "market-0")
        self.assertEqual(data[0]["status"], "confirmed")
        self.assertEqual(data[0]["user_id"], 1)

    def test_get_redemptions_scopes_to_current_user(self):
        self._seed_attempts(2)
        self.db.add(
            RedemptionAttempt(
                user_id=999,
                market_id="other-user-market",
                collection_id="0x" + "ab" * 32,
                status="pending",
                amount=1.0,
                created_at=utc_now(),
            )
        )
        self.db.commit()

        resp = self.client.get("/api/portfolio/redemptions")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 2)
        self.assertTrue(all(row["user_id"] == 1 for row in data))

    def test_get_redemptions_limit_is_clamped_to_minimum(self):
        self._seed_attempts(3)
        resp = self.client.get("/api/portfolio/redemptions", params={"limit": 0})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

    def test_get_redemptions_limit_is_clamped_to_maximum(self):
        self._seed_attempts(3)
        resp = self.client.get("/api/portfolio/redemptions", params={"limit": 500})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 3)

    def test_get_redemptions_empty_history(self):
        resp = self.client.get("/api/portfolio/redemptions")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_get_redemptions_requires_authentication(self):
        app.dependency_overrides.clear()
        resp = self.client.get("/api/portfolio/redemptions")
        self.assertEqual(resp.status_code, 401)


class FetchTokenPricesTests(unittest.IsolatedAsyncioTestCase):
    """Direct tests for the _fetch_token_prices helper."""

    def _service(self, positions=None, last_trades=None, midpoints=None, clob_error=False):
        service = MagicMock()
        service.get_positions = AsyncMock(return_value=positions or [])
        service._to_float = MagicMock(
            side_effect=lambda value, default=0.0: float(value) if value is not None else default
        )
        clob = MagicMock()
        if clob_error:
            service._get_clob_client = MagicMock(side_effect=RuntimeError("clob down"))
        else:
            service._get_clob_client = MagicMock(return_value=clob)
        clob.get_last_trades_prices = MagicMock(return_value=last_trades or [])
        clob.get_midpoints = MagicMock(return_value=midpoints or [])
        return service, clob

    def _patches(self, service, stored):
        return (
            patch.object(portfolio_module, "get_polymarket_service", return_value=service),
            patch.object(portfolio_module, "load_wallet_credentials", return_value=stored),
        )

    async def test_returns_empty_without_stored_credentials(self):
        service, clob = self._service()
        with self._patches(service, None)[0], self._patches(service, None)[1]:
            prices = await portfolio_module._fetch_token_prices(WALLET)
        self.assertEqual(prices, {})
        service.get_positions.assert_not_called()
        service._get_clob_client.assert_not_called()

    async def test_positions_failure_is_suppressed(self):
        service, clob = self._service()
        service.get_positions = AsyncMock(side_effect=RuntimeError("positions down"))
        p1, p2 = self._patches(service, {"private_key": PRIVATE_KEY, "clob_creds": None})
        with p1, p2:
            prices = await portfolio_module._fetch_token_prices(WALLET)
        self.assertEqual(prices, {})

    async def test_clob_client_failure_returns_empty(self):
        service, clob = self._service(positions=[{"asset_id": "tok1"}], clob_error=True)
        p1, p2 = self._patches(service, {"private_key": PRIVATE_KEY, "clob_creds": None})
        with p1, p2:
            prices = await portfolio_module._fetch_token_prices(WALLET)
        self.assertEqual(prices, {})

    async def test_last_trade_prices_are_used(self):
        service, clob = self._service(
            positions=[{"asset_id": "tok1"}, {"asset_id": "tok2"}, {"no_id": True}],
            last_trades=[
                {"token_id": "tok1", "price": "0.42"},
                # Zero prices are skipped.
                {"token_id": "tok2", "price": "0"},
            ],
        )
        p1, p2 = self._patches(service, {"private_key": PRIVATE_KEY, "clob_creds": None})
        with p1, p2:
            prices = await portfolio_module._fetch_token_prices(WALLET)
        self.assertEqual(prices, {"tok1": 0.42})
        args = clob.get_last_trades_prices.call_args.args[0]
        self.assertEqual([b.token_id for b in args], ["tok1", "tok2"])

    async def test_midpoints_fill_missing_tokens(self):
        service, clob = self._service(
            positions=[{"asset_id": "tok1"}],
            last_trades=[],
            midpoints=[{"token_id": "tok1", "mid": "0.31"}],
        )
        p1, p2 = self._patches(service, {"private_key": PRIVATE_KEY, "clob_creds": None})
        with p1, p2:
            prices = await portfolio_module._fetch_token_prices(WALLET, extra_token_ids=["tokX"])
        self.assertEqual(prices, {"tok1": 0.31})
        args = clob.get_midpoints.call_args.args[0]
        self.assertEqual([b.token_id for b in args], ["tok1", "tokX"])

    async def test_midpoint_failure_is_suppressed(self):
        service, clob = self._service(
            positions=[{"asset_id": "tok1"}],
            last_trades=[],
        )
        clob.get_midpoints = MagicMock(side_effect=RuntimeError("midpoints down"))
        p1, p2 = self._patches(service, {"private_key": PRIVATE_KEY, "clob_creds": None})
        with p1, p2:
            prices = await portfolio_module._fetch_token_prices(WALLET)
        self.assertEqual(prices, {})

    async def test_extra_token_ids_are_deduplicated_with_position_ids(self):
        service, clob = self._service(
            positions=[{"asset_id": "tok1"}],
            last_trades=[
                {"token_id": "tok1", "price": "0.5"},
                {"token_id": "tok2", "price": "0.6"},
            ],
        )
        p1, p2 = self._patches(service, {"private_key": PRIVATE_KEY, "clob_creds": None})
        with p1, p2:
            prices = await portfolio_module._fetch_token_prices(
                WALLET, extra_token_ids=["tok1", "tok2", ""]
            )
        self.assertEqual(prices, {"tok1": 0.5, "tok2": 0.6})
        args = clob.get_last_trades_prices.call_args.args[0]
        self.assertEqual([b.token_id for b in args], ["tok1", "tok2"])

    async def test_last_trades_failure_is_suppressed(self):
        service, clob = self._service(positions=[{"asset_id": "tok1"}])
        clob.get_last_trades_prices = MagicMock(side_effect=RuntimeError("trades down"))
        clob.get_midpoints = MagicMock(side_effect=RuntimeError("midpoints down"))
        p1, p2 = self._patches(service, {"private_key": PRIVATE_KEY, "clob_creds": None})
        with p1, p2:
            prices = await portfolio_module._fetch_token_prices(WALLET)
        self.assertEqual(prices, {})

    async def test_credential_store_error_is_treated_as_no_credentials(self):
        service, clob = self._service()
        with (
            patch.object(portfolio_module, "get_polymarket_service", return_value=service),
            patch.object(
                portfolio_module,
                "load_wallet_credentials",
                side_effect=CredentialStoreError("encryption not configured"),
            ),
        ):
            prices = await portfolio_module._fetch_token_prices(WALLET)
        self.assertEqual(prices, {})
        service.get_positions.assert_not_called()


class PositionTokenIdsTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_private_key_returns_empty(self):
        service = MagicMock()
        service.get_positions = AsyncMock(return_value=[{"asset_id": "tok1"}])
        with (
            patch.object(portfolio_module, "get_polymarket_service", return_value=service),
            patch.object(portfolio_module, "load_wallet_credentials", return_value=None),
        ):
            self.assertEqual(await portfolio_module._position_token_ids(WALLET), [])
        service.get_positions.assert_not_called()

    async def test_positions_error_returns_empty(self):
        service = MagicMock()
        service.get_positions = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch.object(portfolio_module, "get_polymarket_service", return_value=service),
            patch.object(
                portfolio_module,
                "load_wallet_credentials",
                return_value={"private_key": PRIVATE_KEY, "clob_creds": None},
            ),
        ):
            self.assertEqual(await portfolio_module._position_token_ids(WALLET), [])

    async def test_returns_asset_ids(self):
        service = MagicMock()
        service.get_positions = AsyncMock(
            return_value=[{"asset_id": "a"}, {"asset_id": "b"}, {"no_id": True}]
        )
        with (
            patch.object(portfolio_module, "get_polymarket_service", return_value=service),
            patch.object(
                portfolio_module,
                "load_wallet_credentials",
                return_value={"private_key": PRIVATE_KEY, "clob_creds": None},
            ),
        ):
            result = await portfolio_module._position_token_ids(WALLET)
        self.assertEqual(result, ["a", "b"])

    async def test_credential_store_error_returns_empty(self):
        service = MagicMock()
        service.get_positions = AsyncMock(return_value=[{"asset_id": "a"}])
        with (
            patch.object(portfolio_module, "get_polymarket_service", return_value=service),
            patch.object(
                portfolio_module,
                "load_wallet_credentials",
                side_effect=CredentialStoreError("encryption not configured"),
            ),
        ):
            self.assertEqual(await portfolio_module._position_token_ids(WALLET), [])
        service.get_positions.assert_not_called()


class PriceStreamFramesCoverageTests(unittest.IsolatedAsyncioTestCase):
    """Branches of _price_stream_frames not covered elsewhere."""

    def _mock_manager(self, connected=True):
        manager = MagicMock()
        manager.is_connected = connected
        manager.register_subscriber = MagicMock(return_value=1)
        manager.unregister_subscriber = MagicMock()
        manager.subscribe = AsyncMock(return_value=["tok1"])
        manager.unsubscribe = AsyncMock()
        return manager

    async def test_manager_defaults_to_process_singleton(self):
        # manager=None: the generator falls back to get_clob_ws_manager().
        manager = self._mock_manager(connected=True)
        with (
            patch.object(
                portfolio_module,
                "_position_token_ids",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(portfolio_module, "get_clob_ws_manager", return_value=manager),
        ):
            gen = portfolio_module._price_stream_frames(
                token_ids=["tok1"],
                wallet_address="0xuser",
                heartbeat_seconds=0.05,
            )
            frame = await asyncio.wait_for(gen.__anext__(), timeout=3)
            await gen.aclose()

        self.assertEqual(frame, ": ping\n\n")
        manager.register_subscriber.assert_called_once()
        manager.subscribe.assert_awaited_once_with(["tok1"])
        manager.unregister_subscriber.assert_called_once_with(1)
        manager.unsubscribe.assert_awaited_once_with(["tok1"])

    async def test_cancellation_propagates_and_cleans_up(self):
        manager = self._mock_manager(connected=True)
        with (
            patch.object(
                portfolio_module,
                "_position_token_ids",
                new=AsyncMock(return_value=[]),
            ),
        ):
            gen = portfolio_module._price_stream_frames(
                token_ids=["tok1"],
                wallet_address="0xuser",
                manager=manager,
                heartbeat_seconds=0.05,
            )
            task = asyncio.create_task(gen.__anext__())
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await gen.aclose()

        manager.unregister_subscriber.assert_called_once()
        manager.unsubscribe.assert_awaited_once_with(["tok1"])


class HelperTests(unittest.TestCase):
    def test_sse_frame_format(self):
        self.assertEqual(_sse_frame({"a": 1}), 'data: {"a": 1}\n\n')

    def test_attempt_response_serializes_attempt(self):
        attempt = MagicMock()
        attempt.id = 42
        attempt.user_id = 7
        attempt.market_id = "market-9"
        attempt.collection_id = "0xcoll"
        attempt.tx_hash = "0xtx"
        attempt.status = "submitted"
        attempt.amount = 2.5
        attempt.error = "some error"
        attempt.created_at = utc_now()

        result = _attempt_response(attempt)

        self.assertIsInstance(result, RedemptionAttemptResponse)
        self.assertEqual(result.id, 42)
        self.assertEqual(result.user_id, 7)
        self.assertEqual(result.market_id, "market-9")
        self.assertEqual(result.collection_id, "0xcoll")
        self.assertEqual(result.tx_hash, "0xtx")
        self.assertEqual(result.status, "submitted")
        self.assertEqual(result.amount, 2.5)
        self.assertEqual(result.error, "some error")
        self.assertEqual(result.created_at, attempt.created_at)

    def test_attempt_response_allows_nullable_fields(self):
        attempt = MagicMock()
        attempt.id = 1
        attempt.user_id = 1
        attempt.market_id = "m"
        attempt.collection_id = "c"
        attempt.tx_hash = None
        attempt.status = "failed"
        attempt.amount = 0.0
        attempt.error = None
        attempt.created_at = utc_now()

        result = _attempt_response(attempt)

        self.assertIsNone(result.tx_hash)
        self.assertIsNone(result.error)


if __name__ == "__main__":
    unittest.main()
