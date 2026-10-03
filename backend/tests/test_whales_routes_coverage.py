"""Coverage for the whale-monitoring routes (``/api/whales``).

Exercises the paginated on-chain event feed with every
filter combination, the per-user config read/update
(create and update paths, auto-copy gating, watchlist
normalisation), request validation, and the response
serializer. Events and configs are persisted in an
in-memory SQLite database.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.auth import get_current_user_from_token
from app.api.routes.whales import _event_to_response
from app.main import app
from app.models.base import Base
from app.models.whale_config import WhaleConfig
from app.models.whale_event import WhaleEvent
from app.services.ctf_events_service import invalidate_config_cache
from app.utils.database import get_db

WALLET = "0x" + "ab" * 20


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _event_kwargs(**overrides):
    base = {
        "wallet": "0x" + "aa" * 20,
        "market_id": "m1",
        "token_id": "t1",
        "event_type": "transfer",
        "side": "buy",
        "size": 100.0,
        "price": 0.5,
        "notional": 50.0,
        "tx_hash": "0xhash1",
        "log_index": 1,
        "block_number": 100,
        "detected_at": datetime.now(),
    }
    base.update(overrides)
    return base


class WhaleRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.factory = _session_factory()
        self.session = self.factory()
        app.dependency_overrides[get_db] = lambda: self.session
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": 1,
            "wallet_address": WALLET,
            "is_admin": False,
        }
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)
        invalidate_config_cache()
        self.addCleanup(invalidate_config_cache)

    def _add_event(self, **overrides):
        event = WhaleEvent(**_event_kwargs(**overrides))
        self.session.add(event)
        self.session.commit()
        self.session.refresh(event)
        return event


class ListEventsTests(WhaleRouteTestCase):
    def test_list_requires_authentication(self):
        app.dependency_overrides.clear()
        response = self.client.get("/api/whales/events")
        self.assertEqual(response.status_code, 401)

    def test_list_returns_events_newest_first(self):
        self._add_event(tx_hash="0xolder", detected_at=datetime.now() - timedelta(minutes=5))
        newer = self._add_event(
            tx_hash="0xnewer", detected_at=datetime.now() - timedelta(minutes=1)
        )
        response = self.client.get("/api/whales/events")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["limit"], 50)
        self.assertEqual(body["offset"], 0)
        self.assertEqual(body["events"][0]["tx_hash"], "0xnewer")
        self.assertEqual(body["events"][1]["tx_hash"], "0xolder")
        self.assertEqual(body["events"][0]["id"], newer.id)
        event = body["events"][0]
        self.assertEqual(event["wallet"], "0x" + "aa" * 20)
        self.assertEqual(event["market_id"], "m1")
        self.assertEqual(event["event_type"], "transfer")
        self.assertEqual(event["side"], "buy")
        self.assertEqual(event["size"], 100.0)
        self.assertEqual(event["notional"], 50.0)
        self.assertEqual(event["log_index"], 1)
        self.assertEqual(event["block_number"], 100)
        self.assertIsNone(event["block_ts"])
        self.assertTrue(event["detected_at"])

    def test_list_filters_by_wallet(self):
        self._add_event(wallet="0x" + "aa" * 20)
        self._add_event(wallet="0x" + "bb" * 20)
        response = self.client.get(
            "/api/whales/events",
            params={"wallet": "0x" + "AA" * 20},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["events"][0]["wallet"], "0x" + "aa" * 20)

    def test_list_filters_by_market_id(self):
        self._add_event(market_id="m1")
        self._add_event(market_id="m2")
        response = self.client.get("/api/whales/events", params={"market_id": "m2"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["events"][0]["market_id"], "m2")

    def test_list_filters_by_side(self):
        self._add_event(side="buy")
        self._add_event(side="sell")
        response = self.client.get("/api/whales/events", params={"side": "sell"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["events"][0]["side"], "sell")

    def test_list_filters_by_event_type(self):
        self._add_event(event_type="transfer")
        self._add_event(event_type="position_split")
        response = self.client.get("/api/whales/events", params={"event_type": "position_split"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["events"][0]["event_type"], "position_split")

    def test_list_combines_filters(self):
        self._add_event(wallet="0x" + "aa" * 20, side="buy", market_id="m1")
        self._add_event(wallet="0x" + "aa" * 20, side="sell", market_id="m1")
        self._add_event(wallet="0x" + "bb" * 20, side="buy", market_id="m1")
        response = self.client.get(
            "/api/whales/events",
            params={
                "wallet": "0x" + "aa" * 20,
                "side": "buy",
                "market_id": "m1",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["events"][0]["side"], "buy")

    def test_list_pagination(self):
        for index in range(5):
            self._add_event(
                tx_hash=f"0xhash{index}",
                detected_at=datetime.now() - timedelta(minutes=index),
            )
        response = self.client.get("/api/whales/events", params={"limit": 2, "offset": 1})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 5)
        self.assertEqual(body["limit"], 2)
        self.assertEqual(body["offset"], 1)
        self.assertEqual(len(body["events"]), 2)
        self.assertEqual(body["events"][0]["tx_hash"], "0xhash1")
        self.assertEqual(body["events"][1]["tx_hash"], "0xhash2")

    def test_list_validates_pagination(self):
        for query in ("limit=0", "limit=501", "offset=-1"):
            response = self.client.get(f"/api/whales/events?{query}")
            self.assertEqual(response.status_code, 422, query)

    def test_list_empty_feed(self):
        response = self.client.get("/api/whales/events")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["events"], [])
        self.assertEqual(body["total"], 0)


class GetConfigTests(WhaleRouteTestCase):
    def test_config_requires_authentication(self):
        app.dependency_overrides.clear()
        response = self.client.get("/api/whales/config")
        self.assertEqual(response.status_code, 401)

    def test_config_returns_defaults_when_unset(self):
        response = self.client.get("/api/whales/config")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["min_notional"], 10000.0)
        self.assertFalse(body["auto_copy"])
        self.assertEqual(body["watchlist"], [])
        self.assertEqual(body["whale_set_size"], 50)

    def test_config_returns_stored_values(self):
        config = WhaleConfig(
            user_id=1,
            min_notional=5000.0,
            auto_copy=True,
            watchlist=["0x" + "aa" * 20],
            whale_set_size=100,
        )
        self.session.add(config)
        self.session.commit()
        response = self.client.get("/api/whales/config")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["min_notional"], 5000.0)
        self.assertTrue(body["auto_copy"])
        self.assertEqual(body["watchlist"], ["0x" + "aa" * 20])
        self.assertEqual(body["whale_set_size"], 100)


class UpdateConfigTests(WhaleRouteTestCase):
    def test_update_requires_authentication(self):
        app.dependency_overrides.clear()
        response = self.client.put("/api/whales/config", json={"min_notional": 1.0})
        self.assertEqual(response.status_code, 401)

    def test_update_creates_config_with_defaults(self):
        response = self.client.put("/api/whales/config", json={})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["min_notional"], 10000.0)
        self.assertFalse(body["auto_copy"])
        self.assertEqual(body["watchlist"], [])
        self.assertEqual(body["whale_set_size"], 50)
        config = self.session.query(WhaleConfig).filter(WhaleConfig.user_id == 1).first()
        self.assertIsNotNone(config)

    def test_update_applies_all_fields(self):
        with patch("app.api.routes.whales.WHALE_AUTO_COPY", True):
            response = self.client.put(
                "/api/whales/config",
                json={
                    "min_notional": 5000.0,
                    "auto_copy": True,
                    "watchlist": ["  0xAA ", "", "0xbb"],
                    "whale_set_size": 100,
                },
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["min_notional"], 5000.0)
        self.assertTrue(body["auto_copy"])
        self.assertEqual(body["watchlist"], ["0xaa", "0xbb"])
        self.assertEqual(body["whale_set_size"], 100)

    def test_update_auto_copy_gated_by_platform_flag(self):
        with patch("app.api.routes.whales.WHALE_AUTO_COPY", False):
            response = self.client.put("/api/whales/config", json={"auto_copy": True})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["auto_copy"])

    def test_update_auto_copy_enabled_when_platform_allows(self):
        with patch("app.api.routes.whales.WHALE_AUTO_COPY", True):
            response = self.client.put("/api/whales/config", json={"auto_copy": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["auto_copy"])

    def test_update_invalidates_config_cache(self):
        with patch("app.api.routes.whales.invalidate_config_cache") as invalidate:
            self.client.put("/api/whales/config", json={"min_notional": 1.0})
        invalidate.assert_called_once_with(1)

    def test_update_validates_body(self):
        for body in (
            {"min_notional": 0.0},
            {"min_notional": -1.0},
            {"whale_set_size": 0},
            {"whale_set_size": 501},
        ):
            response = self.client.put("/api/whales/config", json=body)
            self.assertEqual(response.status_code, 422, body)


class EventResponseHelperTests(unittest.TestCase):
    def test_event_to_response(self):
        detected_at = datetime.now()
        block_ts = datetime.now()
        event = WhaleEvent(
            id=7,
            wallet="0x" + "aa" * 20,
            market_id="m1",
            token_id="t1",
            event_type="position_merge",
            side="close",
            size=250.0,
            price=0.75,
            notional=187.5,
            tx_hash="0xhash",
            log_index=3,
            block_number=42,
            block_ts=block_ts,
            detected_at=detected_at,
        )
        response = _event_to_response(event)
        self.assertEqual(response.id, 7)
        self.assertEqual(response.wallet, "0x" + "aa" * 20)
        self.assertEqual(response.event_type, "position_merge")
        self.assertEqual(response.side, "close")
        self.assertEqual(response.size, 250.0)
        self.assertEqual(response.price, 0.75)
        self.assertEqual(response.notional, 187.5)
        self.assertEqual(response.log_index, 3)
        self.assertEqual(response.block_number, 42)
        self.assertEqual(response.block_ts, block_ts)
        self.assertEqual(response.detected_at, detected_at)

    def test_event_to_response_with_null_optionals(self):
        event = WhaleEvent(
            id=1,
            wallet="0x" + "aa" * 20,
            market_id="m1",
            token_id="t1",
            event_type="transfer",
            side="buy",
            size=1.0,
            price=0.5,
            notional=0.5,
            tx_hash="0xhash",
            detected_at=datetime.now(),
        )
        response = _event_to_response(event)
        self.assertIsNone(response.log_index)
        self.assertIsNone(response.block_number)
        self.assertIsNone(response.block_ts)


if __name__ == "__main__":
    unittest.main()
