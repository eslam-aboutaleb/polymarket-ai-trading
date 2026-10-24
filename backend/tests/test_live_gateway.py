"""Exercise the real gateway and database, replacing only remote exchange I/O."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.trading import risk, trades
from app.models import Base
from app.models.trading.followed_trader import FollowedTrader
from app.models.trading.live_order import LiveOrder
from app.models.trading.trading_control import TradingControlState
from app.models.trading.user_trade import UserTrade
from app.models.users.user import User
from app.models.users.user_settings import UserSettings
from app.services.trading.execution import clob_transport, live
from app.services.trading.live_trading_policy import LiveTradingBlocked
from app.utils.database import get_db
from app.utils.time import utc_now


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'orders.db'}")
    Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    db.add(User(id=1, wallet_address="0x" + "1" * 40))
    db.flush()
    db.add(
        UserSettings(
            user_id=1,
            simulation_mode=False,
            copy_trading_enabled=True,
            require_ai_approval=False,
            max_position_size=100,
            daily_loss_limit=50,
            initial_capital=1000,
        )
    )
    for scope in ("global", "user:1"):
        db.add(TradingControlState(scope=scope, state="RUNNING", reason="test", version=1))
    db.add(FollowedTrader(user_id=1, trader_wallet="0x" + "2" * 40, is_active=True))
    db.commit()
    flags = SimpleNamespace(
        live_trading_enabled=True,
        live_copy_trading_enabled=True,
        live_market_making_enabled=True,
        live_latency_arb_enabled=True,
        live_inverse_trading_enabled=True,
    )
    monkeypatch.setattr(live, "get_settings", lambda: flags)
    monkeypatch.setattr(live, "load_wallet_credentials", lambda _: {"private_key": "test-only"})
    client = MagicMock()
    monkeypatch.setattr(clob_transport, "build_client", lambda *_: client)
    monkeypatch.setattr(clob_transport, "balance", lambda *_: Decimal(1000))

    def prepare(_client, token, side, price, shares):
        identity = "0x" + str(
            db.scalar(select(LiveOrder.id).order_by(LiveOrder.id.desc())) or 0
        ).zfill(64)
        return object(), identity, shares, shares * price

    monkeypatch.setattr(clob_transport, "prepare", prepare)

    def submit(_client, _signed, _type, _post_only):
        # The money path MUST see the committed reservation from another session.
        with Session(engine) as observer:
            row = observer.scalar(select(LiveOrder).order_by(LiveOrder.id.desc()))
            assert row.state == "SUBMITTING"
            assert observer.get(UserTrade, row.trade_id).status == "unknown"
            return {"success": True, "orderID": row.order_hash, "status": "live"}

    submit_mock = MagicMock(side_effect=submit)
    monkeypatch.setattr(clob_transport, "submit", submit_mock)
    yield db, client, flags, submit_mock
    db.close()
    engine.dispose()


def place(db, **changes):
    args = {
        "user_id": 1,
        "token_id": "123",
        "side": "BUY",
        "price": 0.5,
        "size": 10,
        "idempotency_key": "source:1",
    }
    args.update(changes)
    return live.place_live_order(db, **args)


def test_actual_submission_is_persisted_before_send_and_never_fabricates_fill(gateway):
    db, _, _, submit = gateway
    result = place(db)
    assert result["success"] and not result["simulated"]
    trade = db.get(UserTrade, result["trade_id"])
    assert trade.status == "pending" and trade.filled_size is None and trade.executed_at is None
    assert place(db) == result
    submit.assert_called_once()


def test_replay_survives_new_session(gateway):
    db, _, _, submit = gateway
    first = place(db)
    with Session(db.bind, expire_on_commit=False) as restarted:
        assert place(restarted) == first
    submit.assert_called_once()


def test_same_key_with_different_economics_is_rejected(gateway):
    db, _, _, submit = gateway
    place(db)
    with pytest.raises(ValueError, match="different order"):
        place(db, size=12)
    submit.assert_called_once()


@pytest.mark.parametrize(
    "response", [None, {}, {"success": True}, {"success": True, "orderID": "other"}]
)
def test_invalid_ack_retains_reservation_and_blocks_new_economic_orders(gateway, response):
    db, _, _, submit = gateway
    submit.side_effect = None
    submit.return_value = response
    assert place(db)["status"] == "unknown"
    assert db.scalar(select(LiveOrder)).reserved == 5
    with pytest.raises(LiveTradingBlocked, match="UNRESOLVED"):
        place(db, idempotency_key="source:2")
    assert place(db)["status"] == "unknown"
    submit.assert_called_once()


def test_timeout_is_durable_and_not_retried(gateway):
    db, _, _, submit = gateway
    submit.side_effect = TimeoutError("remote response lost")
    result = place(db)
    assert result["status"] == "unknown" and result["order_hash"]
    with Session(db.bind, expire_on_commit=False) as restarted:
        assert place(restarted)["status"] == "unknown"
    submit.assert_called_once()


def test_exchange_rejection_releases_reservation(gateway):
    db, _, _, submit = gateway
    submit.side_effect = None
    submit.return_value = {"success": False, "errorMsg": "insufficient allowance"}
    result = place(db)
    assert result["status"] == "rejected" and not result["success"]
    assert db.scalar(select(LiveOrder)).reserved == 0
    assert db.get(UserTrade, result["trade_id"]).status == "rejected"


@pytest.mark.parametrize("enabled", [False, True], ids=["live-disabled", "live-enabled"])
@pytest.mark.parametrize("strategy", list(live.STRATEGY_FLAGS))
@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_master_live_switch_controls_every_strategy(gateway, monkeypatch, enabled, strategy, side):
    db, client, flags, submit = gateway
    flags.live_trading_enabled = enabled
    build = MagicMock(return_value=client)
    monkeypatch.setattr(clob_transport, "build_client", build)
    kwargs = {
        "strategy": strategy,
        "side": side,
        "copied_from_wallet": "0x" + "2" * 40 if strategy == "copy" else None,
    }
    if enabled:
        result = place(db, **kwargs)
        assert result["success"] and not result["simulated"]
        row = db.scalar(select(LiveOrder))
        assert row.strategy == strategy and row.side == side and row.state == "SUBMITTED"
        assert row.reserved == (Decimal(5) if side == "BUY" else 0)
        assert db.get(UserTrade, result["trade_id"]).filled_size is None
        build.assert_called_once()
        submit.assert_called_once()
    else:
        with pytest.raises(LiveTradingBlocked, match="^LIVE_TRADING_DISABLED$"):
            place(db, **kwargs)
        assert db.scalar(select(LiveOrder.id)) is None
        assert db.scalar(select(UserTrade.id)) is None
        build.assert_not_called()
        submit.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True], ids=["strategy-disabled", "strategy-enabled"])
@pytest.mark.parametrize(
    "strategy,field", [(s, flag) for s, flag in live.STRATEGY_FLAGS.items() if flag]
)
def test_strategy_switch_is_independent_of_master_and_manual(
    gateway, monkeypatch, enabled, strategy, field
):
    db, client, flags, submit = gateway
    setattr(flags, field, enabled)
    build = MagicMock(return_value=client)
    monkeypatch.setattr(clob_transport, "build_client", build)
    kwargs = {"strategy": strategy}
    if strategy == "copy":
        kwargs["copied_from_wallet"] = "0x" + "2" * 40
    if enabled:
        assert place(db, **kwargs)["success"]
        submit.assert_called_once()
    else:
        with pytest.raises(LiveTradingBlocked, match="^LIVE_STRATEGY_DISABLED$"):
            place(db, **kwargs)
        build.assert_not_called()
        submit.assert_not_called()
        # A disabled automated strategy must not disable authorized manual trading.
        assert place(db, strategy="manual")["success"]
        submit.assert_called_once()


@pytest.mark.parametrize("enabled", [False, True], ids=["live-disabled", "live-enabled"])
@pytest.mark.parametrize("path", ["execute", "cash-out"])
def test_manual_http_order_switch(gateway, monkeypatch, enabled, path):
    db, client, flags, submit = gateway
    flags.live_trading_enabled = enabled
    build = MagicMock(return_value=client)
    monkeypatch.setattr(clob_transport, "build_client", build)
    app = FastAPI()
    app.include_router(trades.router)
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as http:
        response = http.post(
            f"/api/trades/{path}",
            json={
                "execution_mode": "live",
                "token_id": "123",
                "side": "BUY",
                "price": 0.5,
                "size": 5,
            },
            headers={"Idempotency-Key": "http-switch"},
        )
    if enabled:
        assert response.status_code == 200
        assert response.json()["success"]
        row = db.scalar(select(LiveOrder))
        assert row.side == ("SELL" if path == "cash-out" else "BUY")
        assert row.shares == (5 if path == "cash-out" else 10)
        submit.assert_called_once()
    else:
        assert response.status_code == 503
        assert response.json()["detail"] == "LIVE_TRADING_DISABLED"
        assert db.scalar(select(LiveOrder.id)) is None
        build.assert_not_called()
        submit.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True], ids=["live-disabled", "live-enabled"])
@pytest.mark.parametrize("scope", ["global", "user"])
def test_http_activation_respects_live_switch(gateway, monkeypatch, enabled, scope):
    db, client, flags, submit = gateway
    flags.live_trading_enabled = enabled
    db.get(User, 1).is_admin = True
    control_scope = "global" if scope == "global" else "user:1"
    db.get(TradingControlState, control_scope).state = "HALTED"
    db.commit()
    client.get_open_orders.return_value = []
    credentials = MagicMock(return_value={"private_key": "test-only"})
    monkeypatch.setattr("app.security.credential_store.load_wallet_credentials", credentials)
    app = FastAPI()
    app.include_router(risk.router)
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as http:
        response = http.post(
            "/api/v1/risk/activate", json={"scope": scope, "initial_capital": 1000}
        )
    if enabled:
        assert response.status_code == 200
        assert response.json()["state"] == "RUNNING"
        assert db.get(TradingControlState, control_scope).state == "RUNNING"
        if scope == "user":
            credentials.assert_called_once()
    else:
        assert response.status_code == 409
        assert response.json()["detail"] == "LIVE_TRADING_DISABLED"
        assert db.get(TradingControlState, control_scope).state == "HALTED"
        credentials.assert_not_called()
    submit.assert_not_called()  # Activation itself never places an order.


@pytest.mark.parametrize("scope", ["global", "user:1"])
def test_durable_halts_prevent_submission(gateway, scope):
    db, _, _, submit = gateway
    db.get(TradingControlState, scope).state = "HALTED"
    db.commit()
    with pytest.raises(LiveTradingBlocked, match="HALTED"):
        place(db)
    submit.assert_not_called()


def test_budget_counts_prior_submissions_across_strategies(gateway):
    db, _, _, submit = gateway
    place(db, size=80)
    with pytest.raises(LiveTradingBlocked, match="DAILY_BUDGET"):
        place(db, size=30, idempotency_key="second", strategy="market_maker")
    submit.assert_called_once()


def test_copy_requires_active_follow_and_explicit_ai_approval(gateway):
    db, _, _, submit = gateway
    settings = db.scalar(select(UserSettings))
    settings.require_ai_approval = True
    db.commit()
    trader = "0x" + "2" * 40
    with pytest.raises(LiveTradingBlocked, match="AI_APPROVAL"):
        place(db, strategy="copy", copied_from_wallet=trader)
    result = place(db, strategy="copy", copied_from_wallet=trader, ai_approved=True)
    assert result["success"]
    submit.assert_called_once()


@pytest.mark.parametrize("enabled", [False, True], ids=["live-disabled", "live-enabled"])
def test_simulation_cannot_authorize_real_order(gateway, enabled):
    db, _, flags, submit = gateway
    flags.live_trading_enabled = enabled
    db.scalar(select(UserSettings)).simulation_mode = True
    db.commit()
    reason = "NOT_IN_LIVE" if enabled else "LIVE_TRADING_DISABLED"
    with pytest.raises(LiveTradingBlocked, match=reason):
        place(db)
    submit.assert_not_called()


def test_untracked_legacy_exposure_blocks_live_spending(gateway):
    db, _, _, submit = gateway
    db.add(
        UserTrade(user_id=1, market_id="m", action="buy", amount=10, price=0.5, status="pending")
    )
    db.commit()
    with pytest.raises(LiveTradingBlocked, match="LEGACY_EXPOSURE"):
        place(db)
    submit.assert_not_called()


def test_partial_cancel_only_releases_unfilled_principal(gateway, monkeypatch):
    db, client, _, _ = gateway
    result = place(db)
    client.get_order.return_value = {
        "id": result["order_hash"],
        "status": "CANCELED",
        "size_matched": "4",
        "original_size": "10",
    }
    monkeypatch.setattr(clob_transport, "cancel", lambda *_: True)
    result = live.reconcile_order(db, user_id=1, order_hash=result["order_hash"], cancel=True)
    assert result["status"] == "cancelled"
    assert db.scalar(select(LiveOrder)).reserved == 2
    assert db.get(UserTrade, result["trade_id"]).status == "partially_filled"


def test_missing_order_never_proves_cancelled(gateway):
    db, client, _, submit = gateway
    submit.side_effect = TimeoutError()
    result = place(db)
    client.get_order.return_value = {}
    with pytest.raises(LiveTradingBlocked, match="UNVERIFIED"):
        live.reconcile_order(db, user_id=1, order_hash=result["order_hash"])
    assert db.scalar(select(LiveOrder)).reserved == 5


@pytest.mark.parametrize(
    "changes",
    [
        {"size": float("nan")},
        {"price": float("inf")},
        {"side": "invalid"},
        {"size": 1.001},
        {"idempotency_key": ""},
    ],
)
def test_invalid_money_input_never_reaches_exchange(gateway, changes):
    db, _, _, submit = gateway
    with pytest.raises(ValueError):
        place(db, **changes)
    submit.assert_not_called()


def test_subaccount_enforces_its_own_budget_and_persists_scope(gateway):
    from app.models.users.account import Account
    from app.models.users.account_settings import AccountSettings

    db, _, _, submit = gateway
    account = Account(owner_user_id=1, name="Scoped", kind="sub", is_default=False)
    db.add(account)
    db.flush()
    db.add(
        AccountSettings(
            account_id=account.id,
            simulation_mode=False,
            max_position_size=6,
            daily_loss_limit=6,
            initial_capital=1000,
        )
    )
    db.commit()
    result = place(db, account_id=account.id)
    row = db.scalar(select(LiveOrder))
    assert row.account_id == account.id
    assert db.get(UserTrade, result["trade_id"]).account_id == account.id
    with pytest.raises(LiveTradingBlocked, match="BUDGET_EXCEEDED"):
        place(db, account_id=account.id, idempotency_key="second")
    submit.assert_called_once()


def test_subaccount_cannot_widen_wallet_budget_or_ownership(gateway):
    from app.models.users.account import Account
    from app.models.users.account_settings import AccountSettings

    db, _, _, submit = gateway
    db.add(User(id=2, wallet_address="0x" + "3" * 40))
    db.flush()
    foreign = Account(owner_user_id=2, name="Foreign", kind="sub", is_default=False)
    db.add(foreign)
    db.flush()
    db.add(AccountSettings(account_id=foreign.id, initial_capital=1000))
    db.commit()
    with pytest.raises(LiveTradingBlocked, match="OWNERSHIP"):
        place(db, account_id=foreign.id)
    submit.assert_not_called()


def test_missing_loss_budget_basis_is_explicit_not_silently_skipped(gateway):
    db, _, _, submit = gateway
    db.scalar(select(UserSettings)).initial_capital = None
    db.commit()
    with pytest.raises(LiveTradingBlocked, match="INITIAL_CAPITAL_REQUIRED"):
        place(db)
    submit.assert_not_called()


def test_halt_during_preparation_never_calls_exchange_and_releases_unsent_intent(
    gateway, monkeypatch
):
    db, _, _, submit = gateway
    original_prepare = clob_transport.prepare

    def prepare(*args):
        result = original_prepare(*args)
        db.get(TradingControlState, "user:1").state = "HALTED"
        return result

    monkeypatch.setattr(clob_transport, "prepare", prepare)
    assert place(db)["status"] == "rejected"
    assert db.scalar(select(LiveOrder)).reserved == 0
    submit.assert_not_called()


def test_maker_cancellation_is_scoped_and_works_after_switch_to_simulation(gateway):
    from app.services.trading.market_maker_service import _cancel_owned_maker_quotes

    db, client, flags, _ = gateway
    owned = place(db, strategy="market_maker", idempotency_key="maker:42:first")
    other = place(db, token_id="456", idempotency_key="manual")
    db.scalar(select(UserSettings)).simulation_mode = True
    db.commit()
    flags.live_trading_enabled = False
    client.get_order.return_value = {
        "id": owned["order_hash"],
        "status": "CANCELED",
        "size_matched": "0",
        "original_size": "10",
    }
    assert _cancel_owned_maker_quotes(db, SimpleNamespace(id=42, user_id=1)) == (1, 0)
    manual = db.scalar(select(LiveOrder).where(LiveOrder.order_hash == other["order_hash"]))
    assert manual.state == "SUBMITTED"
    client.get_order.assert_called_once_with(owned["order_hash"])


def test_maker_does_not_claim_unknown_cancellation_succeeded(gateway):
    from app.services.trading.market_maker_service import _cancel_owned_maker_quotes

    db, client, _, _ = gateway
    place(db, strategy="market_maker", idempotency_key="maker:42:first")
    client.get_order.return_value = {}
    assert _cancel_owned_maker_quotes(db, SimpleNamespace(id=42, user_id=1)) == (0, 1)
    assert db.scalar(select(LiveOrder)).reserved == 5


def test_reconciliation_failures_do_not_starve_orders_after_first_batch(gateway):
    db, client, _, _ = gateway
    old = utc_now() - timedelta(days=1)
    for number in range(101):
        trade = UserTrade(
            user_id=1,
            market_id="m",
            token_id="123",
            action="buy",
            amount=5,
            price=0.5,
            status="unknown" if number < 100 else "pending",
        )
        db.add(trade)
        db.flush()
        db.add(
            LiveOrder(
                user_id=1,
                trade_id=trade.id,
                idempotency_key=f"recovery:{number}",
                fingerprint="a" * 64,
                strategy="manual",
                token_id="123",
                side="BUY",
                price=0.5,
                shares=10,
                notional=5,
                reserved=5,
                state="UNKNOWN" if number < 100 else "SUBMITTED",
                order_hash=f"0x{number:064x}",
                response={},
                created_at=old,
                updated_at=old,
            )
        )
    db.commit()
    final_hash = f"0x{100:064x}"

    def observation(order_hash):
        if order_hash != final_hash:
            raise LookupError("Exchange cannot resolve this order")
        return {
            "id": final_hash,
            "status": "CANCELED",
            "size_matched": "0",
            "original_size": "10",
        }

    client.get_order.side_effect = observation
    live.reconcile_pending_live_orders(db)
    rows = db.scalars(select(LiveOrder).order_by(LiveOrder.id)).all()
    assert all(row.state == "UNKNOWN" and row.reserved == 5 for row in rows[:100])
    assert all(row.updated_at > rows[-1].updated_at for row in rows[:100])
    assert rows[-1].state == "SUBMITTED"

    live.reconcile_pending_live_orders(db)
    db.refresh(rows[-1])
    assert rows[-1].state == "CANCELLED" and rows[-1].reserved == 0
    assert client.get_order.call_args_list[100].args == (final_hash,)
