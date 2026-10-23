from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.models import Account, AccountSettings, Base, User, UserSettings
from app.services.analytics import paper_accounting as paper
from app.services.analytics.replay.follower import Book, Level
from app.services.market_data.replay_capture import append_capture

CID = "0x" + "ab" * 32
RULE = "a" * 64


def evidence(db, clock, *, bid="0.39", ask="0.40", depth="100", token="11", rule=RULE, final=None):
    moment = clock[0]
    clob = {
        "active": final is None,
        "closed": final is not None,
        "accepting_orders": final is None,
        "tokens": [
            {"token_id": "11", "outcome": "Yes", "winner": final == "11"},
            {"token_id": "22", "outcome": "No", "winner": final == "22"},
        ],
    }
    gamma = {
        "active": final is None,
        "closed": final is not None,
        "outcomes": ["Yes", "No"],
        "clobTokenIds": ["11", "22"],
        "outcomePrices": ["1" if final == "11" else "0", "1" if final == "22" else "0"],
        "umaResolutionStatus": "resolved" if final else "proposed",
    }
    for kind, raw in (("clob_market", clob), ("gamma", gamma)):
        append_capture(
            db,
            kind=kind,
            source_key=CID,
            raw=raw,
            observed_at=moment,
            condition_id=CID,
            rule_hash=rule,
        )
    book = Book(
        snapshot_id=str(moment.timestamp()).replace(".", "-"),
        liquidity_id="budget-" + token,
        token_id=token,
        observed_at=moment,
        source_at=moment,
        bids=[Level(price=Decimal(bid), shares=Decimal(depth))] if bid else [],
        asks=[Level(price=Decimal(ask), shares=Decimal(depth))] if ask else [],
    )
    return append_capture(
        db,
        kind="book",
        source_key=book.snapshot_id,
        raw={
            "book": book.model_dump(mode="json"),
            "venue": {
                "asset_id": token,
                "market": CID,
                "tick_size": "0.01",
                "min_order_size": "0.01",
            },
        },
        observed_at=moment,
        source_at=moment,
        condition_id=CID,
        token_id=token,
        rule_hash=rule,
    )


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(get_settings(), "paper_journal_v2_enabled", True)
    clock = [datetime(2026, 10, 23, 12, tzinfo=UTC)]
    monkeypatch.setattr(paper, "utc_now", lambda: clock[0])
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    # Python sqlite3's legacy transaction mode otherwise commits a top-level
    # SAVEPOINT on release; explicit BEGIN matches PostgreSQL transaction semantics.
    @event.listens_for(engine, "connect")
    def connect(connection, _):
        connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    with sessionmaker(engine, expire_on_commit=False)() as session:
        session.add_all(
            [
                User(id=1, wallet_address="0x" + "11" * 20),
                User(id=2, wallet_address="0x" + "22" * 20),
            ]
        )
        session.flush()
        session.add_all(
            [
                Account(id=1, owner_user_id=1, name="Personal", kind="personal", is_default=True),
                Account(id=2, owner_user_id=1, name="Second", kind="sub", is_default=False),
                Account(id=3, owner_user_id=2, name="Personal", kind="personal", is_default=True),
            ]
        )
        session.add(UserSettings(user_id=1, paper_balance=1000, simulation_mode=True))
        session.add(AccountSettings(account_id=2, paper_balance=1000, simulation_mode=True))
        session.commit()
        paper.configure_account(session, 1, 1, fee_bps=0)
        paper.configure_account(session, 1, 2, fee_bps=0)
        evidence(session, clock)
        session.commit()
        yield session, clock
    engine.dispose()


def order(db, *, amount="25", side="BUY", key="entry", account=1, **kwargs):
    return paper.submit_paper_order(
        db,
        user_id=1,
        account_id=account,
        token_id="11",
        side=side,
        amount=Decimal(amount),
        strategy="manual",
        units="SHARES",
        intent_key=key,
        **kwargs,
    )
