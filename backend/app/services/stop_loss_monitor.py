"""
Stop-loss & take-profit monitor – background task that periodically checks
prices against active stop-loss and take-profit orders.

- Stop-loss: triggers SELL when price <= stop_price
- Take-profit: triggers SELL when price >= take_profit_price

Runs inside the FastAPI backend process using asyncio tasks.
"""
import asyncio
import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import Dict, List, Any

from sqlalchemy.orm import Session, joinedload

from py_clob_client.clob_types import BookParams

from app.models.stop_loss import StopLossOrder
from app.models.take_profit import TakeProfitOrder
from app.models.user import User
from app.config import get_settings
from app.security.credential_store import CredentialStoreError, load_wallet_credentials
from app.utils.database import SessionLocal
from app.services.copy_trade_service import _place_order_on_polymarket, _get_poly_proxy_wallet_address
from app.services.polymarket_service import get_polymarket_service, POLYMARKET_CLOB_API

logger = logging.getLogger(__name__)

try:
    _settings = get_settings()
    # Check interval in seconds (defaults to 10s for responsiveness).
    STOP_LOSS_CHECK_INTERVAL = int(_settings.stop_loss_check_interval_seconds)
except Exception:
    STOP_LOSS_CHECK_INTERVAL = 10

_monitor_task: asyncio.Task | None = None


async def start_stop_loss_monitor():
    """Start the background stop-loss monitoring loop."""
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        logger.info("Stop-loss monitor already running")
        return
    _monitor_task = asyncio.create_task(_monitor_loop())
    logger.info("Stop-loss monitor started (interval=%ds)", STOP_LOSS_CHECK_INTERVAL)


async def stop_stop_loss_monitor():
    """Cancel the background monitor."""
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        try:
            await _monitor_task
        except asyncio.CancelledError:
            pass
    _monitor_task = None
    logger.info("Stop-loss monitor stopped")


async def _monitor_loop():
    """Main monitoring loop – runs until cancelled."""
    while True:
        try:
            await _check_stop_losses()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Stop-loss check error: %s", e, exc_info=True)
        await asyncio.sleep(STOP_LOSS_CHECK_INTERVAL)


async def _check_stop_losses():
    """
    1. Load all active stop-loss AND take-profit orders grouped by user.
    2. For each user, fetch live prices for their token_ids.
    3. Stop-loss: if price <= stop_price, trigger a SELL order.
    4. Take-profit: if price >= take_profit_price, trigger a SELL order.
    """
    started = perf_counter()
    db: Session = SessionLocal()
    try:
        # Eager-load user relationship to avoid N+1 wallet lookups.
        # 2 queries total (SL + TP) instead of 2 + 1 per user.
        active_sl_orders: List[StopLossOrder] = (
            db.query(StopLossOrder)
            .options(joinedload(StopLossOrder.user))
            .filter(StopLossOrder.status == "active")
            .all()
        )
        active_tp_orders: List[TakeProfitOrder] = (
            db.query(TakeProfitOrder)
            .options(joinedload(TakeProfitOrder.user))
            .filter(TakeProfitOrder.status == "active")
            .all()
        )

        if not active_sl_orders and not active_tp_orders:
            return

        # Group orders by user and collect wallet addresses from the
        # eagerly-loaded relationship — no extra queries needed.
        all_user_ids: set[int] = set()
        wallet_by_user: Dict[int, str] = {}
        user_sl_orders: Dict[int, List[StopLossOrder]] = {}
        for order in active_sl_orders:
            user_sl_orders.setdefault(order.user_id, []).append(order)
            all_user_ids.add(order.user_id)
            if order.user and order.user.wallet_address:
                wallet_by_user[order.user_id] = order.user.wallet_address

        user_tp_orders: Dict[int, List[TakeProfitOrder]] = {}
        for order in active_tp_orders:
            user_tp_orders.setdefault(order.user_id, []).append(order)
            all_user_ids.add(order.user_id)
            if order.user and order.user.wallet_address:
                wallet_by_user[order.user_id] = order.user.wallet_address

        if not all_user_ids:
            return

        triggered_total = 0
        for user_id in all_user_ids:
            wallet_address = wallet_by_user.get(user_id)
            if not wallet_address:
                continue
            sl_orders = user_sl_orders.get(user_id, [])
            tp_orders = user_tp_orders.get(user_id, [])
            triggered_total += await _check_user_price_orders(
                db=db,
                user_id=user_id,
                wallet_address=wallet_address,
                sl_orders=sl_orders,
                tp_orders=tp_orders,
            )

        elapsed_ms = (perf_counter() - started) * 1000
        logger.info(
            "Price-order cycle complete: sl_orders=%d tp_orders=%d users=%d triggered=%d elapsed_ms=%.1f",
            len(active_sl_orders),
            len(active_tp_orders),
            len(all_user_ids),
            triggered_total,
            elapsed_ms,
        )

    finally:
        db.close()


async def _check_user_price_orders(
    db: Session,
    user_id: int,
    wallet_address: str,
    sl_orders: List[StopLossOrder],
    tp_orders: List[TakeProfitOrder],
) -> int:
    """Check and potentially trigger stop-losses and take-profits for a single user."""
    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError as exc:
        logger.warning("Price-order credentials unavailable for %s: %s", wallet_address, exc)
        return 0
    if not stored:
        return 0

    pk = stored["private_key"]
    creds = stored.get("clob_creds")

    # Fetch live prices for all tokens across both order types
    token_ids = list(
        {o.token_id for o in sl_orders} | {o.token_id for o in tp_orders}
    )
    prices = await _fetch_live_prices(pk, creds, token_ids)

    triggered = 0

    # Check stop-losses: trigger SELL when price <= stop_price
    for order in sl_orders:
        live_price = prices.get(order.token_id)
        if live_price is None:
            continue
        if live_price <= order.stop_price:
            logger.info(
                "Stop-loss TRIGGERED: order=%d token=%s price=%.4f <= stop=%.4f",
                order.id, order.token_id[:20], live_price, order.stop_price,
            )
            await _execute_stop_loss(db, order, pk, creds, live_price)
            triggered += 1

    # Check take-profits: trigger SELL when price >= take_profit_price
    for order in tp_orders:
        live_price = prices.get(order.token_id)
        if live_price is None:
            continue
        if live_price >= order.take_profit_price:
            logger.info(
                "Take-profit TRIGGERED: order=%d token=%s price=%.4f >= tp=%.4f",
                order.id, order.token_id[:20], live_price, order.take_profit_price,
            )
            await _execute_take_profit(db, order, pk, creds, live_price)
            triggered += 1

    return triggered


async def _fetch_live_prices(
    private_key: str,
    clob_creds: dict | None,
    token_ids: List[str],
) -> Dict[str, float]:
    """Fetch current prices for a list of token IDs via CLOB client.

    Resolves the proxy wallet address (same as the cash-out fix) and tries
    multiple signature types so that L2 auth / cred derivation works
    regardless of wallet configuration.
    """
    prices: Dict[str, float] = {}
    if not token_ids:
        return prices

    try:
        from py_clob_client.signer import Signer

        service = get_polymarket_service()

        # Resolve proxy address (mirrors _place_order_on_polymarket logic)
        signer = Signer(private_key, 137)
        eoa_address = signer.address()
        proxy_address = await asyncio.to_thread(_get_poly_proxy_wallet_address, eoa_address)

        # Try multiple signature types: POLY_PROXY first, then EOA, then gnosis
        # This mirrors the retry pattern from _place_order_on_polymarket.
        sig_types = [1, 0, 2]
        clob = None

        for sig_type in sig_types:
            try:
                funder = proxy_address if sig_type == 1 and proxy_address else None
                clob = await asyncio.to_thread(
                    service._get_clob_client,
                    private_key,
                    clob_creds,
                    signature_type=sig_type,
                    funder=funder,
                )
                # Quick test: fetch prices for the first token to verify auth works
                await asyncio.to_thread(
                    clob.get_last_trades_prices,
                    [BookParams(token_id=token_ids[0])],
                )
                # If we got here without error, auth is working
                break
            except Exception as auth_err:
                logger.debug(
                    "Price fetch auth failed with sig_type=%d: %s", sig_type, auth_err
                )
                clob = None
                continue

        if not clob:
            logger.warning("Could not create authenticated CLOB client for price fetching")
            return prices

        prices_resp = await asyncio.to_thread(
            clob.get_last_trades_prices,
            [BookParams(token_id=tid) for tid in token_ids],
        )
        for entry in prices_resp:
            tid = entry.get("token_id") or entry.get("asset_id")
            price = service._to_float(entry.get("price"), 0.0)
            if tid and price > 0:
                prices[tid] = price

        # Fill missing with midpoints
        missing = [tid for tid in token_ids if tid not in prices or prices[tid] == 0.0]
        if missing:
            mid_resp = await asyncio.to_thread(
                clob.get_midpoints,
                [BookParams(token_id=tid) for tid in missing],
            )
            for entry in mid_resp:
                tid = entry.get("token_id") or entry.get("asset_id")
                mid = service._to_float(entry.get("mid"), 0.0)
                if tid and mid > 0:
                    prices[tid] = mid
    except Exception as e:
        logger.warning("Failed to fetch live prices for stop-loss: %s", e)

    return prices


async def _execute_stop_loss(
    db: Session,
    order: StopLossOrder,
    private_key: str,
    clob_creds: dict | None,
    live_price: float,
):
    """Execute the sell order and update the stop-loss record."""
    try:
        result = await asyncio.to_thread(
            _place_order_on_polymarket,
            private_key=private_key,
            clob_creds=clob_creds,
            token_id=order.token_id,
            side="SELL",
            price=live_price,
            size=order.size,
        )

        now = datetime.now(timezone.utc)
        if result.get("success"):
            order.status = "triggered"
            order.order_hash = result.get("order_hash")
            order.executed_price = live_price
            order.triggered_at = now
            logger.info("Stop-loss executed: order=%d hash=%s", order.id, order.order_hash)
        else:
            order.status = "failed"
            order.triggered_at = now
            logger.error("Stop-loss execution failed: order=%d error=%s", order.id, result.get("error"))

        order.updated_at = now
        db.commit()

    except Exception as e:
        logger.error("Stop-loss execution exception: order=%d error=%s", order.id, e)
        try:
            order.status = "failed"
            order.triggered_at = datetime.now(timezone.utc)
            order.updated_at = datetime.now(timezone.utc)
            db.commit()
        except Exception:
            db.rollback()


async def _execute_take_profit(
    db: Session,
    order: TakeProfitOrder,
    private_key: str,
    clob_creds: dict | None,
    live_price: float,
):
    """Execute the sell order for a take-profit and update the record."""
    try:
        result = await asyncio.to_thread(
            _place_order_on_polymarket,
            private_key=private_key,
            clob_creds=clob_creds,
            token_id=order.token_id,
            side="SELL",
            price=live_price,
            size=order.size,
        )

        now = datetime.now(timezone.utc)
        if result.get("success"):
            order.status = "triggered"
            order.order_hash = result.get("order_hash")
            order.executed_price = live_price
            order.triggered_at = now
            logger.info("Take-profit executed: order=%d hash=%s", order.id, order.order_hash)
        else:
            order.status = "failed"
            order.triggered_at = now
            logger.error("Take-profit execution failed: order=%d error=%s", order.id, result.get("error"))

        order.updated_at = now
        db.commit()

    except Exception as e:
        logger.error("Take-profit execution exception: order=%d error=%s", order.id, e)
        try:
            order.status = "failed"
            order.triggered_at = datetime.now(timezone.utc)
            order.updated_at = datetime.now(timezone.utc)
            db.commit()
        except Exception:
            db.rollback()
