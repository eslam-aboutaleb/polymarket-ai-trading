"""
Market maker service — Bands & AMM strategies.

Adapted from Polymarket/poly-market-maker's strategy lifecycle:
  1. Fetch current midpoint price from CLOB
  2. Compute expected orders based on strategy
  3. Diff expected vs. open orders
  4. Cancel stale orders, place new ones
  5. Repeat every sync_interval seconds

Two strategies:
  - Bands: configurable spread bands around midpoint
  - AMM: constant-product virtual AMM curve
"""

import asyncio
import contextlib
import logging
from typing import Any

from py_clob_client.clob_types import OpenOrderParams
from sqlalchemy.orm import Session

from app.models.trading.market_maker_config import MarketMakerConfig
from app.repositories.trading.bots import TradingBotRepository
from app.services.market_data.polymarket_service import POLYMARKET_CLOB_API
from app.services.trading.execution.async_io import run_blocking_execution
from app.utils.database import SessionLocal
from app.utils.scheduler_lock import (
    acquire_scheduler_lock,
    release_scheduler_lock,
    scheduler_heartbeat,
)
from app.utils.time import utc_now

logger = logging.getLogger(__name__)
MAX_BANDS = 20

# Track running maker tasks per config ID
_running_makers: dict[int, asyncio.Task] = {}


def list_user_market_maker_configs(db: Session, user_id: int) -> list[MarketMakerConfig]:
    """List the current user's saved market-maker configurations."""
    return TradingBotRepository(db).list_market_maker_configs(user_id)


def get_market_maker_config_for_market(
    db: Session,
    user_id: int,
    condition_id: str,
) -> MarketMakerConfig | None:
    """Find the current user's config for a market before updating it."""
    return TradingBotRepository(db).find_market_maker_config(user_id, condition_id)


def get_market_maker_config_for_user(
    db: Session,
    config_id: int,
    user_id: int,
) -> MarketMakerConfig | None:
    """Find a config only when it belongs to the requesting user."""
    return TradingBotRepository(db).find_market_maker_config_for_user(config_id, user_id)


def upsert_market_maker_config(
    db: Session,
    user_id: int,
    values: dict[str, Any],
) -> MarketMakerConfig:
    """Create or update a user's config for one market and commit it."""
    repository = TradingBotRepository(db)
    config = repository.find_market_maker_config(user_id, values["condition_id"])
    is_new = config is None
    if is_new:
        config = MarketMakerConfig(user_id=user_id)
    for field, value in values.items():
        setattr(config, field, value)
    if is_new:
        return repository.save_market_maker_config(config, refresh=True)
    repository.commit()
    return repository.refresh(config)


def set_market_maker_config_state(
    db: Session,
    config: MarketMakerConfig,
    *,
    enabled: bool,
    status: str | None = None,
) -> MarketMakerConfig:
    """Persist an enable/disable state change for a validated config."""
    config.enabled = enabled
    if status is not None:
        config.status = status
    return TradingBotRepository(db).save_market_maker_config(config)


# ─────────────── Strategy helpers ───────────────


def _validate_max_bands(num_bands: int) -> None:
    if num_bands > MAX_BANDS:
        raise ValueError(f"Max {MAX_BANDS} bands allowed")


def _compute_bands_orders(
    midpoint: float,
    config: MarketMakerConfig,
) -> list[dict[str, Any]]:
    """Compute expected orders using the Bands strategy.

    Places symmetric bid/ask orders in bands around the midpoint.
    Each band is further from mid by an equal step between min_spread and max_spread.
    """
    orders: list[dict[str, Any]] = []
    if config.num_bands < 1 or midpoint <= 0:
        return orders
    _validate_max_bands(config.num_bands)

    spread_step = (
        (config.max_spread - config.min_spread) / max(config.num_bands - 1, 1)
        if config.num_bands > 1
        else 0.0
    )

    total_collateral = 0.0

    for i in range(config.num_bands):
        spread = config.min_spread + spread_step * i

        bid_price = round(midpoint - spread, 4)
        ask_price = round(midpoint + spread, 4)
        size = config.band_order_size

        # Price bounds check
        if (
            bid_price >= config.min_price
            and bid_price < midpoint
            and total_collateral + size <= config.max_collateral
        ):
            orders.append(
                {
                    "side": "BUY",
                    "price": bid_price,
                    "size": size,
                    "token_id": config.token_id_yes,
                    "band": i,
                }
            )
            total_collateral += size

        if (
            ask_price <= config.max_price
            and ask_price > midpoint
            and total_collateral + size <= config.max_collateral
        ):
            orders.append(
                {
                    "side": "SELL",
                    "price": ask_price,
                    "size": size,
                    "token_id": config.token_id_yes,
                    "band": i,
                }
            )
            total_collateral += size

    return orders


def _compute_amm_orders(
    midpoint: float,
    config: MarketMakerConfig,
) -> list[dict[str, Any]]:
    """Compute expected orders using a virtual constant-product AMM curve.

    Models a virtual pool with `amm_liquidity` USDC.
    Spreads orders along the curve at discrete price levels.
    """
    orders: list[dict[str, Any]] = []
    if midpoint <= 0 or config.amm_liquidity <= 0:
        return orders

    # Virtual reserves: x * y = k
    # At midpoint p, x = L/2, y = L/(2p)
    L = config.amm_liquidity
    x_reserve = L / 2.0
    y_reserve = L / (2.0 * midpoint) if midpoint > 0 else L / 2.0
    k = x_reserve * y_reserve

    total_collateral = 0.0
    num_levels = config.num_bands or 5
    _validate_max_bands(num_levels)
    spread_step = (config.max_spread - config.min_spread) / max(num_levels - 1, 1)

    for i in range(num_levels):
        offset = config.min_spread + spread_step * i

        # Bid side
        bid_price = round(midpoint - offset, 4)
        if bid_price >= config.min_price and bid_price > 0:
            # How many shares at this price?
            new_y = k / (x_reserve + config.band_order_size)
            share_delta = y_reserve - new_y
            if (
                share_delta > config.min_order_size
                and total_collateral + config.band_order_size <= config.max_collateral
            ):
                orders.append(
                    {
                        "side": "BUY",
                        "price": bid_price,
                        "size": config.band_order_size,
                        "token_id": config.token_id_yes,
                        "band": i,
                    }
                )
                total_collateral += config.band_order_size

        # Ask side
        ask_price = round(midpoint + offset, 4)
        if (
            ask_price <= config.max_price
            and total_collateral + config.band_order_size <= config.max_collateral
        ):
            orders.append(
                {
                    "side": "SELL",
                    "price": ask_price,
                    "size": config.band_order_size,
                    "token_id": config.token_id_yes,
                    "band": i,
                }
            )
            total_collateral += config.band_order_size

    return orders


# ─────────────── Order book helpers ───────────────


async def _fetch_midpoint(token_id: str) -> float | None:
    """Fetch the current midpoint price from the CLOB order book."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{POLYMARKET_CLOB_API}/book",
                params={"token_id": token_id},
            )
            if resp.status_code != 200:
                logger.warning("Failed to fetch order book: %s", resp.status_code)
                return None
            data = resp.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if not bids or not asks:
                return None
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            return round((best_bid + best_ask) / 2.0, 4)
    except Exception as e:
        logger.error("Error fetching midpoint for %s: %s", token_id[:20], e)
        return None


async def _fetch_order_book(token_id: str) -> dict[str, Any] | None:
    """Fetch the full order book for depth-aware paper fills.

    Returns best bid/ask, the midpoint and the total USDC depth
    resting on each side of the book (public, unauthenticated).
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{POLYMARKET_CLOB_API}/book",
                params={"token_id": token_id},
            )
            if resp.status_code != 200:
                logger.warning("Failed to fetch order book: %s", resp.status_code)
                return None
            data = resp.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if not bids or not asks:
                return None
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            bid_depth = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in bids)
            ask_depth = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks)
            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "midpoint": round((best_bid + best_ask) / 2.0, 4),
                "bid_depth": bid_depth,
                "ask_depth": ask_depth,
                "depth": min(bid_depth, ask_depth),
            }
    except Exception as e:
        logger.error("Error fetching order book for %s: %s", token_id[:20], e)
        return None


async def _get_open_orders(
    private_key: str,
    token_id: str,
) -> list[dict[str, Any]]:
    """Fetch open orders for a market from the CLOB."""
    from py_clob_client.client import ClobClient

    try:
        client = ClobClient(
            host=POLYMARKET_CLOB_API,
            chain_id=137,
            key=private_key,
            signature_type=1,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        # get_orders expects an OpenOrderParams object. Passing a dict raised
        # AttributeError inside the library, which the except below swallowed,
        # so _cancel_all_orders always cancelled nothing and every sync cycle
        # layered a fresh set of orders on top of the previous one.
        #
        # OpenOrderParams has no `state` field, so the CLOB returns every order
        # for the asset and live orders are selected here.
        orders = client.get_orders(params=OpenOrderParams(asset_id=token_id))
        if not isinstance(orders, list):
            return []
        return [o for o in orders if isinstance(o, dict) and o.get("status", "live") == "live"]
    except Exception as e:
        logger.error("Error fetching open orders: %s", e)
        return []


async def _cancel_order(private_key: str, order_id: str) -> bool:
    """Cancel a single order by ID."""
    from py_clob_client.client import ClobClient

    try:
        client = ClobClient(
            host=POLYMARKET_CLOB_API,
            chain_id=137,
            key=private_key,
            signature_type=1,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        client.cancel(order_id)
        return True
    except Exception as e:
        logger.error("Error cancelling order %s: %s", order_id, e)
        return False


async def _cancel_all_orders(private_key: str, token_id: str) -> int:
    """Cancel all open orders for a token. Returns count cancelled."""
    open_orders = await _get_open_orders(private_key, token_id)
    cancelled = 0
    for order in open_orders:
        oid = order.get("id") or order.get("order_id")
        if oid and await _cancel_order(private_key, oid):
            cancelled += 1
    return cancelled


async def _place_maker_order(
    private_key: str,
    token_id: str,
    side: str,
    price: float,
    size: float,
) -> str | None:
    """Place a GTC limit order for market making. Returns order ID or None."""
    from app.services.trading.live_trading_policy import (
        LiveTradingBlocked,
        block_legacy_live_execution,
    )

    try:
        block_legacy_live_execution("market_making")
    except LiveTradingBlocked as exc:
        logger.warning("Maker order blocked: %s", exc)
        return None

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL

    try:
        from py_clob_client.signer import Signer

        from app.services.trading.copy_trade_service import _get_poly_proxy_wallet_address

        signer = Signer(private_key, 137)
        eoa = signer.address()
        proxy = _get_poly_proxy_wallet_address(eoa)

        client = ClobClient(
            host=POLYMARKET_CLOB_API,
            chain_id=137,
            key=private_key,
            signature_type=1,
            funder=proxy if proxy else None,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        order_side = BUY if side.upper() == "BUY" else SELL

        shares = (round(size / price, 2) if price > 0 else 0) if order_side == BUY else size

        if shares < 0.01:
            return None

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side=order_side,
        )
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)

        order_id = None
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("id")
        return order_id
    except Exception as e:
        logger.error("Error placing maker order: %s", e)
        return None


# ─────────────── Core sync loop ───────────────


async def _sync_market_maker(config_id: int) -> dict[str, Any]:
    """Execute one sync cycle for a market maker config.

    Returns summary stats for the cycle.
    """
    db: Session = SessionLocal()
    try:
        repository = TradingBotRepository(db)
        config = repository.get_market_maker_config(config_id)
        if not config or not config.enabled:
            return {"skipped": True, "reason": "disabled or not found"}

        user = repository.get_user(config.user_id)
        if not user:
            return {"skipped": True, "reason": "user not found"}

        # Per-user simulation flag: paper mode is decided by the
        # OWNING user's settings, never by a global switch.
        user_settings = repository.get_user_settings(config.user_id)
        simulation_mode = bool(user_settings and getattr(user_settings, "simulation_mode", False))

        # 1. Fetch midpoint (paper mode also needs book depth)
        if simulation_mode:
            book = await _fetch_order_book(config.token_id_yes)
            if book is None:
                config.last_error = "Could not fetch order book"
                db.commit()
                return {"error": "no book"}
            midpoint = book["midpoint"]
        else:
            midpoint = await _fetch_midpoint(config.token_id_yes)
            if midpoint is None:
                config.last_error = "Could not fetch midpoint"
                db.commit()
                return {"error": "no midpoint"}

        # 2. Compute expected orders
        if config.strategy == "amm":
            expected = _compute_amm_orders(midpoint, config)
        else:
            expected = _compute_bands_orders(midpoint, config)

        # Paper mode: simulate fills against book depth and record
        # simulated trades — no CLOB calls are made at all.
        if simulation_mode:
            return await _sync_market_maker_paper(db, config, midpoint, expected, book)

        from decimal import ROUND_DOWN, Decimal
        from uuid import uuid4

        from app.services.trading.execution.live import (
            place_live_order,
            require_enabled,
        )

        require_enabled("market_maker")
        cancelled, unresolved = await run_blocking_execution(_cancel_owned_maker_quotes, db, config)
        if unresolved:
            raise ValueError("Quote replacement requires confirmed cancellation")
        config.total_orders_cancelled += cancelled

        placed = 0
        cycle = uuid4().hex
        for index, order_spec in enumerate(expected):
            shares = Decimal(str(order_spec["size"]))
            if order_spec["side"] == "BUY":
                shares /= Decimal(str(order_spec["price"]))
            shares = shares.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            result = await run_blocking_execution(
                place_live_order,
                db,
                user_id=config.user_id,
                token_id=order_spec["token_id"],
                side=order_spec["side"],
                price=order_spec["price"],
                size=float(shares),
                market_id=config.condition_id,
                strategy="market_maker",
                post_only=True,
                idempotency_key=f"maker:{config.id}:{cycle}:{index}",
            )
            if not result["success"]:
                raise ValueError(result.get("error") or result["status"])
            placed += 1
        # Submitting a quote is not executed volume.
        config.total_orders_placed += placed
        config.current_open_orders = placed
        config.last_sync_at = utc_now()
        config.last_error = None
        config.status = "running"
        db.commit()

        return {
            "midpoint": midpoint,
            "expected_orders": len(expected),
            "cancelled": cancelled,
            "placed": placed,
        }
    except Exception as e:
        logger.error("Market maker sync error (config=%d): %s", config_id, e, exc_info=True)
        try:
            config = TradingBotRepository(db).get_market_maker_config(config_id)
            if config:
                config.status = "error"
                config.last_error = str(e)[:500]
                db.commit()
        except Exception:
            db.rollback()
        return {"error": str(e)}
    finally:
        db.close()


async def _sync_market_maker_paper(
    db: Session,
    config: MarketMakerConfig,
    midpoint: float,
    expected: list[dict[str, Any]],
    book: dict[str, Any],  # noqa: ARG001 - midpoint/depth are not fill evidence
) -> dict[str, Any]:
    """Capture desired paper quotes. Missing flow/queue data never proves a fill."""
    from app.services.trading.paper_maker_intents import sync_intents

    result = sync_intents(db, config, expected)
    config.current_open_orders = 0  # Intents are neither venue nor simulated resting orders.
    config.last_sync_at = utc_now()
    config.last_error = None
    config.status = "running"
    db.commit()
    logger.info("Paper maker intents config=%s count=%s fills=0", config.id, result["intent_count"])
    return {
        "midpoint": midpoint,
        "expected_orders": len(expected),
        "cancelled": 0,
        "placed": 0,
        "simulated": True,
        **result,
    }


async def _maker_loop(config_id: int):
    """Continuous loop for a single market maker config."""
    lock_name = f"market_maker:{config_id}"
    if not acquire_scheduler_lock(lock_name):
        return
    logger.info("Market maker loop started for config_id=%d", config_id)
    try:
        while True:
            try:
                db = SessionLocal()
                config = TradingBotRepository(db).get_market_maker_config(config_id)
                interval = config.sync_interval_seconds if config else 30
                enabled = config.enabled if config else False
                db.close()

                if not enabled:
                    logger.info("Market maker config %d disabled, stopping loop", config_id)
                    break

                result = await _sync_market_maker(config_id)
                logger.info("Market maker sync (config=%d): %s", config_id, result)
                scheduler_heartbeat(lock_name)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Market maker loop error (config=%d): %s", config_id, e)
                interval = 30

            await asyncio.sleep(interval)
    finally:
        # Released on EVERY exit path: a disabled-config break, a cancellation
        # caught inside the loop body, and — the leak this fixes — a
        # cancellation landing during the interval sleep, which sits outside
        # the inner try and used to skip this release, holding the advisory
        # lock until process exit (breaking the single-worker scheduler-lock
        # design for the next worker). release_scheduler_lock is a no-op when
        # the lock is no longer held, and a CancelledError still propagates
        # out through the finally instead of being swallowed.
        try:
            await run_blocking_execution(_cleanup_maker_quotes, config_id)
        finally:
            release_scheduler_lock(lock_name)

    logger.info("Market maker loop ended for config_id=%d", config_id)


def _cancel_owned_maker_quotes(db: Session, config: MarketMakerConfig) -> tuple[int, int]:
    from sqlalchemy import select

    from app.models.trading.live_order import LiveOrder
    from app.services.trading.execution.live import reconcile_order

    resting = db.scalars(
        select(LiveOrder).where(
            LiveOrder.user_id == config.user_id,
            LiveOrder.strategy == "market_maker",
            LiveOrder.idempotency_key.like(f"maker:{config.id}:%"),
            LiveOrder.state.in_(("SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN", "SUBMITTING")),
        )
    ).all()
    cancelled, unresolved = 0, 0
    for order in resting:
        try:
            result = reconcile_order(
                db, user_id=config.user_id, order_hash=order.order_hash, cancel=True
            )
            if result["status"] == "cancelled":
                cancelled += 1
            else:
                unresolved += 1
        except Exception:
            db.rollback()
            unresolved += 1
    return cancelled, unresolved


def _cleanup_maker_quotes(config_id: int) -> None:
    db = None
    try:
        db = SessionLocal()
        config = TradingBotRepository(db).get_market_maker_config(config_id)
        if config is not None:
            # A switch to simulation does not erase real orders already resting.
            _, unresolved = _cancel_owned_maker_quotes(db, config)
            config.current_open_orders = unresolved
            config.status = "error" if unresolved else "idle"
            if unresolved:
                config.last_error = "Live quote cancellation remains unverified"
            db.commit()
    except Exception:
        if db is not None:
            db.rollback()
        logger.warning(
            "Market maker loop cleanup failed for config_id=%d", config_id, exc_info=True
        )
    finally:
        if db is not None:
            db.close()


# ─────────────── Public API ───────────────


async def start_market_maker(config_id: int) -> bool:
    """Start the market maker for a specific config."""
    if config_id in _running_makers and not _running_makers[config_id].done():
        return False  # Already running
    task = asyncio.create_task(_maker_loop(config_id))
    _running_makers[config_id] = task
    return True


async def stop_market_maker(config_id: int) -> bool:
    """Stop the market maker for a specific config."""
    task = _running_makers.pop(config_id, None)
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return True
    return False


async def stop_all_market_makers():
    """Stop all running market makers. Called during app shutdown."""
    for config_id in list(_running_makers.keys()):
        await stop_market_maker(config_id)
    logger.info("All market makers stopped")


async def start_all_enabled_market_makers():
    """Start market makers for all enabled configs. Called during app startup."""
    db = SessionLocal()
    try:
        configs = TradingBotRepository(db).list_enabled_market_maker_configs()
        started = 0
        for config in configs:
            if await start_market_maker(config.id):
                started += 1
        if started:
            logger.info("Started %d market maker(s) on startup", started)
    finally:
        db.close()


def get_running_maker_ids() -> list[int]:
    """Return IDs of currently running market makers."""
    return [cid for cid, t in _running_makers.items() if not t.done()]


async def trigger_single_sync(config_id: int) -> dict[str, Any]:
    """Execute a single sync cycle (for manual trigger from API)."""
    return await _sync_market_maker(config_id)
