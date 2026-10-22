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
