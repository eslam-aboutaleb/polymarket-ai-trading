import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, MarketMakerConfig, PaperMakerIntent, User, UserTrade
from app.services.trading.market_maker_service import _sync_market_maker_paper


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        session.add(User(id=1, wallet_address="0x" + "a" * 40))
        session.add(
            MarketMakerConfig(
                id=1, user_id=1, condition_id="cond", token_id_yes="YES", token_id_no="NO"
            )
        )
        session.commit()
        yield session
    engine.dispose()


def sync(db, expected):
    config = db.get(MarketMakerConfig, 1)
    return asyncio.run(
        _sync_market_maker_paper(
            db, config, 0.5, expected, {"best_bid": 0.49, "best_ask": 0.51, "depth": 10000}
        )
    )


QUOTES = [
    {"token_id": "YES", "side": "BUY", "price": 0.45, "size": 10},
    {"token_id": "YES", "side": "SELL", "price": 0.55, "size": 10},
]


def test_static_book_hundred_cycles_never_fills_or_duplicates_intents(db):
    with (
        patch(
            "app.services.trading.trade_recorder.record_user_trade",
            side_effect=AssertionError("phantom fill"),
        ),
        patch(
            "py_clob_client.client.ClobClient.post_order",
            side_effect=AssertionError("paper cannot post"),
        ),
    ):
        first = sync(db, QUOTES)
        identities = {row.id for row in db.query(PaperMakerIntent)}
        for _ in range(99):
            result = sync(db, QUOTES)
            assert result["placed"] == 0 and result["intents_created"] == 0
    assert first["intents_created"] == 2
    assert {row.id for row in db.query(PaperMakerIntent)} == identities
    assert db.query(UserTrade).count() == 0
    config = db.get(MarketMakerConfig, 1)
    assert config.total_volume_usdc == config.total_orders_placed == config.current_open_orders == 0


def test_reprice_withdraw_and_restart_preserve_definitions_not_fills(db):
    sync(db, QUOTES)
    ids = {row.id for row in db.query(PaperMakerIntent)}
    result = sync(db, [dict(QUOTES[0], price=0.44)])
    assert result["intents_withdrawn"] == 2 and result["intents_created"] == 1
    assert sum(row.state == "UNFILLED_DATA_MISSING" for row in db.query(PaperMakerIntent)) == 1
    db.expire_all()
    sync(db, QUOTES)
    assert ids.issubset({row.id for row in db.query(PaperMakerIntent)})
    result = sync(db, [])
    assert result["intent_count"] == 0
    assert all(row.state == "WITHDRAWN" for row in db.query(PaperMakerIntent))


@pytest.mark.parametrize(
    "bad",
    [
        dict(QUOTES[0], side="BOGUS"),
        dict(QUOTES[0], price=float("nan")),
        dict(QUOTES[0], size=-1),
        dict(QUOTES[0], token_id="other"),
    ],
)
def test_invalid_intent_cannot_overwrite_current_quotes(db, bad):
    sync(db, QUOTES)
    with pytest.raises(ValueError):
        sync(db, [bad])
    db.rollback()
    assert db.query(PaperMakerIntent).count() == 2


def test_intent_api_active_account_and_auth_boundary(monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool

    from app.api import deps
    from app.api.routes.auth.auth import get_current_user_from_token
    from app.api.routes.trading import market_maker
    from app.models import Account
    from app.utils.database import get_db

    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add_all(
            [User(id=1, wallet_address="0x" + "a" * 40), User(id=2, wallet_address="0x" + "b" * 40)]
        )
        session.flush()
        session.add_all(
            [
                Account(id=1, owner_user_id=1, name="Personal", kind="personal", is_default=True),
                Account(id=2, owner_user_id=1, name="Sub", kind="sub"),
                Account(id=3, owner_user_id=2, name="Other", kind="personal", is_default=True),
            ]
        )
        session.add(
            MarketMakerConfig(
                id=1, user_id=1, condition_id="cond", token_id_yes="YES", token_id_no="NO"
            )
        )
        session.commit()
        sync(session, QUOTES)

    def sessions():
        with factory() as session:
            yield session

    app = FastAPI()
    app.include_router(market_maker.router)
    app.dependency_overrides[get_db] = sessions
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(accounts_enabled=True))
    with TestClient(app) as http:
        own = http.get("/api/market-maker/configs/1/paper-intents")
        assert own.status_code == 200 and len(own.json()["intents"]) == 2
        assert (
            http.get(
                "/api/market-maker/configs/1/paper-intents", headers={"X-Account-Id": "2"}
            ).status_code
            == 404
        )
        assert (
            http.get(
                "/api/market-maker/configs/1/paper-intents", headers={"X-Account-Id": "3"}
            ).status_code
            == 403
        )
        app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 2}
        assert http.get("/api/market-maker/configs/1/paper-intents").status_code == 404
        app.dependency_overrides.pop(get_current_user_from_token)
        assert http.get("/api/market-maker/configs/1/paper-intents").status_code == 401
    engine.dispose()


def test_definition_identity_ignores_ambient_decimal_precision(db):
    from decimal import localcontext

    quote = [dict(QUOTES[0], price=0.4567)]
    sync(db, quote)
    identity = db.query(PaperMakerIntent).one().id
    with localcontext() as context:
        context.prec = 2
        result = sync(db, quote)
    assert result["intents_created"] == 0
    assert db.query(PaperMakerIntent).one().id == identity
    assert "0.4567" in db.query(PaperMakerIntent).one().payload_json
