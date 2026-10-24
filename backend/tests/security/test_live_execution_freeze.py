"""Exercise the real release barrier without granting test signing authority."""

import ast
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.platform.external import ExternalOrderRequest, external_create_order
from app.api.routes.trading import trades
from app.config import Settings
from app.services.trading.copy_trade_service import (
    _place_order_multi_phase,
    _place_order_on_polymarket,
)
from app.services.trading.execution import (
    ExecutionEngineRegistry,
    ExecutionMode,
    OrderPlacementRequest,
    get_execution_engine,
)
from app.services.trading.latency_arb_service import _place_fok_order
from app.services.trading.live_trading_policy import LiveTradingBlocked, block_legacy_live_execution
from app.services.trading.market_maker_service import _place_maker_order
from app.utils.database import get_db


@pytest.fixture(params=[False, True])
def live_flags(request):
    settings = Settings(
        _env_file=None,
        jwt_secret_key="test-only-key-that-is-long-enough-for-settings",
        live_trading_enabled=request.param,
        live_copy_trading_enabled=request.param,
        live_market_making_enabled=request.param,
        live_latency_arb_enabled=request.param,
        live_inverse_trading_enabled=request.param,
    )
    with (
        patch("app.services.trading.live_trading_policy.get_settings", return_value=settings),
        patch("app.services.trading.execution.live.get_settings", return_value=settings),
    ):
        yield settings


@pytest.mark.parametrize("strategy", ["manual", "external", "copy", "market_making", "latency_arb"])
def test_flags_cannot_release_legacy_orders(live_flags, strategy):
    with pytest.raises(LiveTradingBlocked):
        block_legacy_live_execution(strategy)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("submit", [_place_order_on_polymarket, _place_order_multi_phase])
def test_shared_path_blocks_manual_copy_inverse_and_conditional_exits(live_flags, side, submit):
    # The invalid key also detects any attempt to instantiate a signer first.
    with (
        patch("py_clob_client.client.ClobClient") as client,
        patch("py_clob_client.signer.Signer") as signer,
    ):
        result = submit("not-a-private-key", None, "token", side, 0.5, 10)
    assert result["success"] is False
    client.assert_not_called()
    signer.assert_not_called()


def test_direct_strategy_submission_boundaries(live_flags):
    with (
        patch("py_clob_client.client.ClobClient") as client,
        patch("app.services.trading.latency_arb_service.build_clob_client") as builder,
    ):
        assert asyncio.run(_place_maker_order("invalid", "token", "BUY", 0.5, 10)) is None
        result = asyncio.run(_place_fok_order("invalid", None, None, "token", 0.5, 10))
    assert result["success"] is False
    builder.assert_not_called()
    client.assert_not_called()


def test_registered_adapter_cannot_bypass_release(live_flags):
    factory = MagicMock()
    registry = ExecutionEngineRegistry()
    registry.register(ExecutionMode.LIVE, factory)
    with pytest.raises(LiveTradingBlocked):
        registry.get(ExecutionMode.LIVE)
    factory.assert_not_called()


@pytest.mark.parametrize("mode", [None, False, True, "", "ambiguous"])
def test_ambiguous_engine_modes_are_rejected(mode):
    with pytest.raises(ValueError):
        get_execution_engine(mode)


def test_paper_does_not_sign_or_submit(live_flags):
    with (
        patch("py_clob_client.client.ClobClient") as client,
        patch("app.services.trading.execution.clob_transport.build_client") as live_client,
        patch("app.services.trading.execution.clob_transport.submit") as submit,
    ):
        engine = get_execution_engine(ExecutionMode.PAPER)
        result = asyncio.run(engine.place_order(OrderPlacementRequest(1, "token", 0.5, 2, "BUY")))
    assert result.is_simulated
    client.assert_not_called()
    live_client.assert_not_called()
    submit.assert_not_called()


def test_manual_api_blocks_before_credentials(live_flags):
    app = FastAPI()
    app.include_router(trades.router)
    app.dependency_overrides[get_current_user_from_token] = lambda: {
        "user_id": 1,
        "wallet_address": "0xwallet",
    }
    app.dependency_overrides[get_db] = lambda: None
    client = TestClient(app)
    payload = {"token_id": "token", "side": "BUY", "price": 0.5, "size": 10}
    with patch.object(trades, "load_wallet_credentials") as credentials:
        assert client.post("/api/trades/execute", json=payload).status_code == 422
        for path in ("execute", "cash-out"):
            response = client.post(
                f"/api/trades/{path}", json={**payload, "execution_mode": "live"}
            )
            assert response.status_code == 503
    credentials.assert_not_called()


def test_external_api_blocks_before_credentials(live_flags):
    body = ExternalOrderRequest(execution_mode="live", token_id="t", side="BUY", price=0.5, size=10)
    with patch("app.api.routes.platform.external._load_credentials") as credentials:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            asyncio.run(external_create_order(body, None, None, None))
    assert exc.value.status_code == 503
    credentials.assert_not_called()


def test_submission_inventory_cannot_grow_without_security_review():
    """A new direct CLOB submission must acquire explicit bypass coverage."""
    app_dir = Path(__file__).resolve().parents[2] / "app"
    found = set()
    submit_methods = {"post_order", "post_orders", "create_and_post_order"}
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr in submit_methods:
                found.add(str(path.relative_to(app_dir)))
    assert found == {
        "services/trading/copy_trade_service.py",
        "services/trading/market_maker_service.py",
        "services/trading/latency_arb_service.py",
        # New path has durable reservation/identity and rejection/restart tests
        # in test_live_gateway.py; legacy submission methods remain frozen.
        "services/trading/execution/clob_transport.py",
    }, "A live submission path changed; review its release barrier and add bypass tests"
