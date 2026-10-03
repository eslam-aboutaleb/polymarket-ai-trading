"""
Copy-trade execution service.

Responsible for:
- Calculating trade sizes based on the user's risk mode
- Placing orders on Polymarket via py-clob-client
- Recording trades in the user_trades table
- Enforcing daily loss limits
- Optionally running LLM analysis before execution
"""

import json
import logging
import math
from collections import OrderedDict
from contextlib import suppress
from datetime import UTC, datetime
from time import time
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.assessment import Assessment
from app.models.followed_trader import FollowedTrader
from app.models.trade_history import TradeHistory
from app.models.user_settings import RiskMode, UserSettings
from app.models.user_trade import UserTrade
from app.security.credential_store import CredentialStoreError, load_wallet_credentials
from app.services import kelly_service
from app.services.polymarket_service import (
    POLYMARKET_CLOB_API,
    get_polymarket_service,
)
from app.services.pre_trade_gate import (
    _to_float,
    apply_global_safety_caps,
    get_idempotent_order_result,
    order_landed_on_clob,
    store_idempotent_order_result,
)
from app.utils.database import SessionLocal
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

# Try to use orjson for hot-path serialization while keeping stdlib fallback.
try:
    import orjson as _orjson

    ORJSON_AVAILABLE = True
except ImportError:
    _orjson = None
    ORJSON_AVAILABLE = False


def _json_dumps(value: Any) -> str:
    if ORJSON_AVAILABLE and _orjson is not None:
        return _orjson.dumps(value).decode("utf-8")
    return json.dumps(value)


def _json_loads(raw: str) -> Any:
    if ORJSON_AVAILABLE and _orjson is not None:
        return _orjson.loads(raw)
    return json.loads(raw)


SIZING_INHERIT_GLOBAL = "inherit_global"
SIZING_FIXED_AMOUNT = "fixed_amount"
SIZING_TRADER_WALLET_RATIO = "trader_wallet_ratio"
SIZING_KELLY = "kelly"

COPY_WALLET_DYNAMIC_PCT = "dynamic_main_wallet_percentage"
COPY_WALLET_FIXED_SNAPSHOT = "fixed_snapshot_amount"

# SELL orders are placed as FOK marketable limit orders with a floor of
# price × (1 − SELL_SLIPPAGE_GUARD), tick-aligned down: a copy or
# stop-loss sell never fills more than 5% below the reference price.
# price <= 0 means unrestricted (emergency stop).
SELL_SLIPPAGE_GUARD = 0.05  # 5%

# Polygon RPC endpoints (for on-chain proxy address lookup)
_POLYGON_RPCS = [
    "https://polygon.drpc.org",
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
]

# CTF Exchange address on Polygon (non-neg-risk)
_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

try:
    _perf_settings = get_settings()
    _PROXY_CACHE_TTL_SECONDS = max(1, int(_perf_settings.proxy_cache_ttl_seconds))
    _PROXY_CACHE_MAX_ENTRIES = max(1, int(_perf_settings.proxy_cache_max_entries))
except Exception:
    _PROXY_CACHE_TTL_SECONDS = 86400
    _PROXY_CACHE_MAX_ENTRIES = 10000

# In-memory cache for proxy wallet addresses (EOA → (proxy, cached_at_ts))
_proxy_address_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()

# Rejection keywords indicating a credential / signature-type mismatch rather
# than a genuine order failure. Only these are safe to retry with a different
# signature type: they are produced by the exchange's own validation and carry
# no ambiguity about whether an order landed.
_RETRYABLE_REJECT_KEYWORDS = ("signature", "balance", "allowance", "not enough")


class OrderRejectedError(RuntimeError):
    """The exchange accepted the request but refused the order.

    The CLOB answers a rejected order with HTTP 200 and a body of
    ``{"success": false, "errorMsg": ...}``, so py-clob-client does not raise.
    Callers must translate that payload into a failure; treating it as a fill
    marks stop-losses as executed and records trades that never happened.
    """


def _cleanup_proxy_address_cache(now_ts: float | None = None) -> None:
    if now_ts is None:
        now_ts = time()
    expired = [
        eoa
        for eoa, (_proxy, cached_at) in _proxy_address_cache.items()
        if (now_ts - cached_at) > _PROXY_CACHE_TTL_SECONDS
    ]
    for eoa in expired:
        _proxy_address_cache.pop(eoa, None)
    while len(_proxy_address_cache) > _PROXY_CACHE_MAX_ENTRIES:
        _proxy_address_cache.popitem(last=False)


def _get_cached_proxy(eoa_lower: str, now_ts: float | None = None) -> str | None:
    if now_ts is None:
        now_ts = time()
    _cleanup_proxy_address_cache(now_ts)
    entry = _proxy_address_cache.get(eoa_lower)
    if not entry:
        return None
    proxy, cached_at = entry
    if (now_ts - cached_at) > _PROXY_CACHE_TTL_SECONDS:
        _proxy_address_cache.pop(eoa_lower, None)
        return None
    _proxy_address_cache.move_to_end(eoa_lower)
    return proxy


def _set_cached_proxy(eoa_lower: str, proxy: str, now_ts: float | None = None) -> None:
    if now_ts is None:
        now_ts = time()
    _cleanup_proxy_address_cache(now_ts)
    _proxy_address_cache[eoa_lower] = (proxy, now_ts)
    _proxy_address_cache.move_to_end(eoa_lower)
    _cleanup_proxy_address_cache(now_ts)


def _get_poly_proxy_wallet_address(eoa_address: str) -> str | None:
    """
    Resolve the Polymarket proxy wallet address for a given EOA by calling
    Exchange.getPolyProxyWalletAddress(eoa) on-chain.

    The proxy address is needed as the `funder` parameter when creating
    ClobClient with signature_type=1 (POLY_PROXY), so that orders use
    maker=proxy instead of maker=EOA.

    Results are cached in-memory since proxy addresses are deterministic.
    """
    eoa_lower = eoa_address.lower()
    cached_proxy = _get_cached_proxy(eoa_lower)
    if cached_proxy:
        return cached_proxy

    try:
        from web3 import Web3

        w3 = None
        for rpc in _POLYGON_RPCS:
            # Unreachable/broken RPCs are expected; fall through to the next.
            with suppress(Exception):
                _w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
                if _w3.is_connected():
                    w3 = _w3
                    break

        if not w3:
            logger.warning("Could not connect to any Polygon RPC for proxy lookup")
            return None

        exchange_addr = Web3.to_checksum_address(_CTF_EXCHANGE)
        eoa_cs = Web3.to_checksum_address(eoa_address)

        # Minimal ABI for getPolyProxyWalletAddress
        abi = [
            {
                "inputs": [{"name": "_addr", "type": "address"}],
                "name": "getPolyProxyWalletAddress",
                "outputs": [{"name": "", "type": "address"}],
                "stateMutability": "view",
                "type": "function",
            }
        ]

        exchange = w3.eth.contract(address=exchange_addr, abi=abi)
        proxy = exchange.functions.getPolyProxyWalletAddress(eoa_cs).call()

        if proxy and proxy != "0x0000000000000000000000000000000000000000":
            _set_cached_proxy(eoa_lower, proxy)
            logger.info("Resolved proxy wallet for %s → %s", eoa_address, proxy)
            return proxy
        logger.warning("No proxy wallet found for %s", eoa_address)
        return None

    except Exception as e:
        logger.warning("Failed to resolve proxy wallet for %s: %s", eoa_address, e)
        return None


# ────────────── Order placement (real) ──────────────


def _place_order_on_polymarket(
    private_key: str,
    clob_creds: dict | None,
    token_id: str,
    side: str,
    price: float,
    size: float,
) -> dict[str, Any]:
    """
    Place an order on Polymarket via py-clob-client.

    For SELL orders we use a FOK (Fill-or-Kill) marketable limit order
    so that even positions smaller than the CLOB's minimum limit-order
    size can be sold instantly against the current order-book.  The
    limit price is floored at ``price × (1 − SELL_SLIPPAGE_GUARD)``,
    rounded down to a tick multiple, so a sell never fills more than
    5% below the reference price.  ``price <= 0`` means unrestricted
    (emergency stop) and only enforces the minimum tick.

    For BUY orders, `size` is interpreted as USDC notional and converted to
    shares at the provided price. We round shares up to 2 decimals so that a
    "$1.00" buy does not get rounded down below Polymarket's $1 marketable-buy
    minimum.

    Tries multiple (signature_type, creds_source) combinations to handle
    different wallet configurations (proxy, EOA, gnosis safe).
    """
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds,
        AssetType,
        BalanceAllowanceParams,
        MarketOrderArgs,
        OrderArgs,
        OrderType,
    )
    from py_clob_client.order_builder.constants import BUY, SELL
    from py_clob_client.signer import Signer

    order_side = BUY if side.upper() == "BUY" else SELL

    # Resolve the proxy wallet address for this key.
    # POLY_PROXY orders MUST set maker=proxy; without funder the client
    # defaults to maker=EOA which always fails signature verification.
    signer = Signer(private_key, 137)
    eoa_address = signer.address()
    proxy_address = _get_poly_proxy_wallet_address(eoa_address)
    if proxy_address:
        logger.info("Using proxy wallet %s as funder for %s", proxy_address, eoa_address)
    else:
        logger.warning("No proxy wallet found; POLY_PROXY orders will use EOA as maker")

    # Build a list of (signature_type, creds_source_label) attempts.
    # We try POLY_PROXY first (stored creds, then fresh), since most
    # Polymarket wallets use the proxy.  Then fall back to EOA / gnosis.
    attempts: list[tuple[int, str]] = [
        (1, "stored"),  # POLY_PROXY with stored creds (fast)
        (1, "fresh"),  # POLY_PROXY with freshly derived creds
        (0, "fresh"),  # EOA
        (2, "fresh"),  # POLY_GNOSIS_SAFE
    ]

    last_error: Exception | None = None
    for sig_type, creds_source in attempts:
        signed_order = None
        try:
            # For POLY_PROXY (sig_type=1), pass the proxy wallet as funder
            # so that order.maker = proxy (not the EOA).
            funder = proxy_address if sig_type == 1 and proxy_address else None

            client = ClobClient(
                host=POLYMARKET_CLOB_API,
                chain_id=137,
                key=private_key,
                signature_type=sig_type,
                funder=funder,
            )

            if creds_source == "stored" and clob_creds:
                creds = ApiCreds(
                    api_key=clob_creds["api_key"],
                    api_secret=clob_creds["api_secret"],
                    api_passphrase=clob_creds["api_passphrase"],
                )
                client.set_api_creds(creds)
            elif creds_source == "stored" and not clob_creds:
                # No stored creds available — skip to the fresh attempt
                continue
            else:
                creds = client.create_or_derive_api_creds()
                client.set_api_creds(creds)

            # Ensure token approval for SELL orders
            if order_side == SELL:
                try:
                    client.update_balance_allowance(
                        BalanceAllowanceParams(
                            asset_type=AssetType.CONDITIONAL,
                            token_id=token_id,
                            signature_type=sig_type,
                        )
                    )
                except Exception as approval_err:
                    logger.debug(
                        "update_balance_allowance() with sig_type=%d: %s (continuing anyway)",
                        sig_type,
                        approval_err,
                    )

            # ── Build & post the order ──
            if order_side == SELL:
                # FOK marketable limit order: executes immediately
                # against existing bids and works for ANY position
                # size (no minimum), but never fills more than
                # SELL_SLIPPAGE_GUARD below the reference price.
                # get_tick_size is cached per client instance (and
                # create_market_order fetches it anyway), so this
                # adds no extra HTTP requests.
                tick = float(client.get_tick_size(token_id))
                if price > 0:
                    raw_floor = price * (1.0 - SELL_SLIPPAGE_GUARD)
                    sell_floor = math.floor(raw_floor / tick) * tick
                    sell_floor = round(max(tick, min(1.0 - tick, sell_floor)), 6)
                else:
                    # Emergency liquidation (price=0): unrestricted,
                    # minimum tick only.
                    sell_floor = tick
                market_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=size,  # shares to sell
                    side=SELL,
                    price=sell_floor,
                )
                signed_order = client.create_market_order(market_args)
                resp = client.post_order(signed_order, OrderType.FOK)
            else:
                # GTC limit order for buys.
                # Incoming `size` is USDC notional; convert to shares.
                # Round UP to 2 decimals to avoid rounding below $1 min notional.
                buy_notional_usdc = float(size)
                if buy_notional_usdc <= 0:
                    raise ValueError("BUY size must be > 0 USDC")
                if price <= 0:
                    raise ValueError("BUY price must be > 0")
                buy_shares = math.ceil((buy_notional_usdc / price) * 100) / 100
                order_args = OrderArgs(
                    price=price,
                    size=buy_shares,
                    side=order_side,
                    token_id=token_id,
                )
                signed_order = client.create_order(order_args)
                resp = client.post_order(signed_order, OrderType.GTC)

            order_hash = resp.get("orderID") or resp.get("order_id") or ""

            # The CLOB answers a rejected order with HTTP 200 and
            # {"success": false, "errorMsg": "..."} — py-clob-client only
            # raises on a non-200 status. Without this check every rejection
            # (FOK not filled, invalid tick, balance moved) was reported to
            # callers as a completed fill, so stop-losses were marked executed
            # and trades recorded as filled while no shares ever moved.
            if resp.get("success") is False or resp.get("errorMsg"):
                reason = resp.get("errorMsg") or resp.get("error") or "order rejected by exchange"
                logger.warning(
                    "Order rejected (signature_type=%d creds=%s funder=%s): %s",
                    sig_type,
                    creds_source,
                    funder,
                    reason,
                )
                last_error = OrderRejectedError(str(reason))
                err_str = str(reason).lower()
                if any(kw in err_str for kw in _RETRYABLE_REJECT_KEYWORDS):
                    continue
                raise last_error

            status = resp.get("status", "submitted")
            logger.info(
                "Order placed successfully with signature_type=%d creds=%s "
                "funder=%s: %s (status=%s)",
                sig_type,
                creds_source,
                funder,
                order_hash,
                status,
            )
            return {
                "success": True,
                "order_hash": order_hash,
                "status": status,
                "response": resp,
            }
        except Exception as e:
            last_error = e
            err_str = str(e).lower()
            logger.warning(
                "Order failed sig_type=%d creds=%s funder=%s: %s",
                sig_type,
                creds_source,
                funder if sig_type == 1 else "N/A",
                e,
            )

            # Ambiguous failure: the order was signed but
            # post_order may have reached the exchange (timeout,
            # 5xx, connection reset).  Ask the exchange — by the
            # order's hash — whether it landed before any retry;
            # retrying a live order would submit it twice.
            if signed_order is not None and not any(
                kw in err_str for kw in _RETRYABLE_REJECT_KEYWORDS
            ):
                landed_hash = order_landed_on_clob(client, signed_order, token_id)
                if landed_hash:
                    logger.info(
                        "Order landed despite post_order error: %s",
                        landed_hash,
                    )
                    return {
                        "success": True,
                        "order_hash": landed_hash,
                        "status": "submitted",
                        "response": {},
                        "verified_after_error": True,
                    }
                if landed_hash is None:
                    # Outcome unknown — retrying could double-submit.
                    logger.error(
                        "Ambiguous order outcome; not retrying: %s",
                        e,
                    )
                    return {
                        "success": False,
                        "order_hash": None,
                        "status": "failed",
                        "error": f"Ambiguous order outcome (not retried): {e}",
                    }

            # Retry with next attempt for signature / balance / allowance errors
            if any(kw in err_str for kw in _RETRYABLE_REJECT_KEYWORDS):
                continue
            # For other errors stop immediately
            logger.error("Order placement failed (non-retryable): %s", e)
            return {
                "success": False,
                "order_hash": None,
                "status": "failed",
                "error": str(e),
            }

    # All attempts exhausted
    logger.error("Order placement failed with all attempts: %s", last_error)
    return {
        "success": False,
        "order_hash": None,
        "status": "failed",
        "error": str(last_error) if last_error else "All signature types failed",
    }


# ────────────── Multi-Phase Order Execution ──────────────


def _place_order_multi_phase(
    private_key: str,
    clob_creds: dict | None,
    token_id: str,
    side: str,
    price: float,
    size: float,
    enable_phases: bool = True,
) -> dict[str, Any]:
    """Multi-phase order execution with automatic price adjustment.

    Inspired by zydomus219/Polymarket-betting-bot's 3-tiered approach:
      Phase 1: Place at original price (GTC limit)
      Phase 2: Adjust price by +1-3 cents toward market for better fills
      Phase 3: Aggressive — reduced size at more aggressive price

    Falls back to single-phase when enable_phases=False.
    """
    if not enable_phases:
        return _place_order_on_polymarket(
            private_key,
            clob_creds,
            token_id,
            side,
            price,
            size,
        )

    # Phase 1: Original price
    logger.info("Multi-phase order: Phase 1 — original price=%.4f size=%.2f", price, size)
    result = _place_order_on_polymarket(
        private_key,
        clob_creds,
        token_id,
        side,
        price,
        size,
    )
    if result.get("success"):
        result["phase"] = 1
        return result

    # Phase 2: Adjust price by 1-3 cents toward market
    for price_adjust in [0.01, 0.02, 0.03]:
        adjusted_price = price + price_adjust if side.upper() == "BUY" else price - price_adjust
        adjusted_price = round(max(0.01, min(0.99, adjusted_price)), 4)
        logger.info(
            "Multi-phase order: Phase 2 — adjusted price=%.4f (offset=%.2f)",
            adjusted_price,
            price_adjust,
        )
        result = _place_order_on_polymarket(
            private_key,
            clob_creds,
            token_id,
            side,
            adjusted_price,
            size,
        )
        if result.get("success"):
            result["phase"] = 2
            result["price_adjustment"] = price_adjust
            return result

    # Phase 3: Aggressive — smaller size, wider spread
    aggressive_size = round(size * 0.5, 2)
    aggressive_price = round(
        price + 0.05 if side.upper() == "BUY" else price - 0.05,
        4,
    )
    aggressive_price = max(0.01, min(0.99, aggressive_price))
    if aggressive_size >= 1.0:
        logger.info(
            "Multi-phase order: Phase 3 — aggressive price=%.4f size=%.2f",
            aggressive_price,
            aggressive_size,
        )
        result = _place_order_on_polymarket(
            private_key,
            clob_creds,
            token_id,
            side,
            aggressive_price,
            aggressive_size,
        )
        if result.get("success"):
            result["phase"] = 3
            result["original_size"] = size
            return result

    logger.error("Multi-phase order exhausted all phases for %s %s", side, token_id[:20])
    return {
        "success": False,
        "order_hash": None,
        "status": "all_phases_failed",
        "error": "All execution phases failed",
    }


# ────────────── Risk calculations ──────────────


# Backwards-compatible alias: the global safety gate moved to
# the shared pre_trade_gate module (plan 01) so the manual
# execute endpoint runs the identical caps; the historical
# private name keeps working for existing callers.
_apply_global_safety_caps = apply_global_safety_caps


def _fixed_amount_for_trade(
    settings: UserSettings,
    followed_trader: FollowedTrader | None,
) -> float:
    if followed_trader and _to_float(followed_trader.fixed_trade_amount_override) > 0:
        return _to_float(followed_trader.fixed_trade_amount_override)
    return _to_float(settings.fixed_trade_amount, 50.0) or 50.0


def _streak_sizing_multiplier(settings: UserSettings) -> float:
    """
    Calculate a dynamic sizing multiplier based on consecutive win/loss streaks.
    - Wins streak: gradually increase up to 1.5x
    - Loss streak: taper down to 0.25x
    """
    if not settings.dynamic_sizing_enabled:
        return 1.0

    wins = settings.consecutive_wins or 0
    losses = settings.consecutive_losses or 0

    if losses >= 5:
        return 0.25
    if losses >= 3:
        return 0.5
    if losses >= 2:
        return 0.75
    if wins >= 5:
        return 1.5
    if wins >= 3:
        return 1.25
    if wins >= 2:
        return 1.1
    return 1.0


def calculate_trade_size(
    settings: UserSettings,
    trader_trade_amount: float,
    db: Session,
) -> tuple[float, str | None]:
    """
    Determine base USDC trade size based on the user's global risk mode.
    Applies dynamic streak-based sizing multiplier when enabled.
    Safety caps are applied separately by _apply_global_safety_caps().

    Returns (trade_size, rejection_reason).
    rejection_reason is None when the trade is allowed.
    """
    risk_mode = settings.risk_mode

    if risk_mode == RiskMode.PERCENTAGE_MIRROR.value:
        pct = max(0, min(100, settings.mirror_percentage or 10.0))
        trade_size = trader_trade_amount * (pct / 100.0)

    elif risk_mode == RiskMode.FIXED_AMOUNT.value:
        trade_size = settings.fixed_trade_amount or 50.0

    else:
        # MAX_POSITION_DAILY_LOSS (default): base sizing by source trade notional.
        # Final max-position + daily-loss safety caps are applied later.
        trade_size = min(trader_trade_amount, settings.max_position_size or 100.0)

    if trade_size <= 0:
        return 0.0, "Calculated trade size is zero"

    # Apply dynamic streak-based multiplier
    multiplier = _streak_sizing_multiplier(settings)
    if multiplier != 1.0:
        trade_size = trade_size * multiplier
        logger.info(
            "Streak sizing: user %s multiplier=%.2f (wins=%d, losses=%d)",
            settings.user_id,
            multiplier,
            settings.consecutive_wins or 0,
            settings.consecutive_losses or 0,
        )

    return round(trade_size, 2), None


async def _plan_copy_trade_size(
    db: Session,
    settings: UserSettings,
    followed_trader: FollowedTrader | None,
    trader_wallet: str,
    trader_trade_notional: float,
    user_wallet_address: str,
    private_key: str | None,
    clob_creds: dict | None,
    market_id: str = "",
    side: str = "BUY",
    price: float = 0.0,
) -> dict[str, Any]:
    """
    Build a decision-complete sizing plan for one copy trade.
    """
    source_notional = _to_float(trader_trade_notional)
    fixed_amount = _fixed_amount_for_trade(settings, followed_trader)

    configured_sizing_mode = (
        followed_trader.sizing_mode
        if followed_trader and followed_trader.sizing_mode
        else SIZING_INHERIT_GLOBAL
    )
    configured_copy_wallet_mode = (
        followed_trader.copy_wallet_mode
        if followed_trader and followed_trader.copy_wallet_mode
        else COPY_WALLET_DYNAMIC_PCT
    )
    configured_copy_wallet_pct = (
        _to_float(followed_trader.copy_wallet_percentage, 100.0) if followed_trader else 100.0
    )
    configured_copy_wallet_fixed = (
        _to_float(followed_trader.copy_wallet_fixed_amount) if followed_trader else 0.0
    )

    plan: dict[str, Any] = {
        "configured_sizing_mode": configured_sizing_mode,
        "configured_copy_wallet_mode": configured_copy_wallet_mode,
        "trade_size_pre_safety": 0.0,
        "sizing_mode_applied": configured_sizing_mode,
        "copy_wallet_mode_applied": configured_copy_wallet_mode,
        "trader_trade_notional": source_notional,
        "trader_wallet_balance": None,
        "copy_wallet_base": None,
        "ratio": None,
        "warning": None,
        "rejection": None,
        "details": {},
    }

    if source_notional <= 0:
        plan["warning"] = "Source trade notional missing; fell back to fixed amount."
        plan["trade_size_pre_safety"] = fixed_amount
        plan["sizing_mode_applied"] = "fixed_amount_fallback"
        plan["copy_wallet_mode_applied"] = COPY_WALLET_FIXED_SNAPSHOT
        plan["copy_wallet_base"] = fixed_amount
        return plan

    if configured_sizing_mode == SIZING_FIXED_AMOUNT:
        plan["trade_size_pre_safety"] = fixed_amount
        plan["copy_wallet_mode_applied"] = COPY_WALLET_FIXED_SNAPSHOT
        plan["copy_wallet_base"] = fixed_amount
        return plan

    if configured_sizing_mode == SIZING_KELLY:
        # Edge-based sizing: fractional Kelly (user-configured
        # multiplier, quarter Kelly by default) of the bankroll
        # (USDC balance + unrealized PnL), using the followed
        # trader's evaluated edge for this market.  The edge
        # source is recorded in calculation_details and logged
        # for audit on every Kelly-sized order.
        p_market, edge_source = kelly_service.resolve_edge_probability(
            db,
            settings.user_id,
            market_id,
            trader_wallet=trader_wallet,
        )
        multiplier = _to_float(
            settings.kelly_fraction,
            kelly_service.DEFAULT_KELLY_MULTIPLIER,
        )
        bankroll = await kelly_service.compute_bankroll(
            get_polymarket_service(),
            user_wallet_address,
            private_key=private_key,
            clob_creds=clob_creds,
        )
        max_pos = _to_float(followed_trader.max_position_size) if followed_trader else 0.0
        if max_pos <= 0:
            max_pos = _to_float(settings.max_position_size)
        p, kelly_price = kelly_service.effective_trade_params(
            p_market or 0.0,
            side,
            price,
        )
        f_star = kelly_service.kelly_fraction(p, kelly_price)
        kelly_size = kelly_service.size(
            bankroll,
            p,
            kelly_price,
            multiplier,
            max_position_size=max_pos,
        )
        plan["trade_size_pre_safety"] = kelly_size
        plan["sizing_mode_applied"] = SIZING_KELLY
        plan["copy_wallet_mode_applied"] = None
        plan["details"] = {
            "kelly_p": p,
            "kelly_price": kelly_price,
            "kelly_f_star": round(f_star, 6),
            "kelly_multiplier": multiplier,
            "kelly_bankroll": bankroll,
            "kelly_edge_source": edge_source,
            "kelly_max_position_size": max_pos,
        }
        if p_market is None:
            plan["warning"] = "No edge estimate available; Kelly sizing skipped."
        logger.info(
            "Kelly copy-trade sizing: user=%d market=%s edge_source=%s "
            "p=%.4f price=%.4f f*=%.4f multiplier=%.2f bankroll=%.2f "
            "size=%.2f",
            settings.user_id,
            market_id,
            edge_source,
            p,
            kelly_price,
            f_star,
            multiplier,
            bankroll,
            kelly_size,
        )
        return plan

    if configured_sizing_mode == SIZING_INHERIT_GLOBAL:
        base_size, rejection = calculate_trade_size(settings, source_notional, db)
        plan["trade_size_pre_safety"] = base_size
        plan["rejection"] = rejection
        plan["sizing_mode_applied"] = f"{SIZING_INHERIT_GLOBAL}:{settings.risk_mode}"
        plan["copy_wallet_mode_applied"] = None
        return plan

    if configured_sizing_mode != SIZING_TRADER_WALLET_RATIO:
        plan["warning"] = (
            f"Unknown sizing mode '{configured_sizing_mode}'; fell back to fixed amount."
        )
        plan["trade_size_pre_safety"] = fixed_amount
        plan["sizing_mode_applied"] = "fixed_amount_fallback"
        plan["copy_wallet_mode_applied"] = COPY_WALLET_FIXED_SNAPSHOT
        plan["copy_wallet_base"] = fixed_amount
        return plan

    fallback_reason: str | None = None
    service = get_polymarket_service()

    try:
        trader_balance = await service.get_wallet_balance(trader_wallet)
        trader_wallet_balance = _to_float(trader_balance.get("usdc_balance"))
    except Exception as e:
        logger.warning("Trader wallet balance fetch failed for %s: %s", trader_wallet[:10], e)
        trader_wallet_balance = 0.0

    if trader_wallet_balance <= 0:
        fallback_reason = "trader wallet balance unavailable"
    else:
        ratio = source_notional / trader_wallet_balance
        plan["trader_wallet_balance"] = round(trader_wallet_balance, 6)
        plan["ratio"] = ratio

    copy_wallet_base = 0.0
    if not fallback_reason:
        if configured_copy_wallet_mode == COPY_WALLET_FIXED_SNAPSHOT:
            copy_wallet_base = configured_copy_wallet_fixed
            if copy_wallet_base <= 0:
                fallback_reason = "copy wallet fixed snapshot amount is not set"
        else:
            pct = max(0.0, min(100.0, configured_copy_wallet_pct))
            plan["details"]["copy_wallet_percentage"] = pct
            try:
                main_balance = await service.get_wallet_balance(
                    user_wallet_address,
                    private_key=private_key,
                    clob_creds=clob_creds,
                )
                main_wallet_usdc = _to_float(main_balance.get("usdc_balance"))
            except Exception as e:
                logger.warning(
                    "Main wallet balance fetch failed for user %s: %s", user_wallet_address[:10], e
                )
                main_wallet_usdc = 0.0

            if main_wallet_usdc <= 0:
                fallback_reason = "main wallet balance unavailable"
            else:
                copy_wallet_base = main_wallet_usdc * (pct / 100.0)

    if not fallback_reason:
        ratio = _to_float(plan.get("ratio"))
        if ratio <= 0:
            fallback_reason = "invalid trader trade ratio"
        elif copy_wallet_base <= 0:
            fallback_reason = "copy wallet base is zero"

    if fallback_reason:
        plan["warning"] = (
            f"Ratio sizing unavailable ({fallback_reason}); fell back to fixed amount."
        )
        plan["trade_size_pre_safety"] = fixed_amount
        plan["sizing_mode_applied"] = "fixed_amount_fallback"
        plan["copy_wallet_mode_applied"] = COPY_WALLET_FIXED_SNAPSHOT
        plan["copy_wallet_base"] = fixed_amount
        return plan

    ratio = _to_float(plan.get("ratio"))
    computed_size = ratio * copy_wallet_base
    plan["trade_size_pre_safety"] = round(computed_size, 6)
    plan["copy_wallet_base"] = round(copy_wallet_base, 6)
    plan["sizing_mode_applied"] = SIZING_TRADER_WALLET_RATIO
    plan["copy_wallet_mode_applied"] = configured_copy_wallet_mode
    return plan


def _record_copy_trade_attempt(
    db: Session,
    *,
    user_id: int,
    market_id: str,
    side: str,
    trade_size: float,
    price: float,
    status: str,
    trader_wallet: str,
    order_hash: str | None,
    trade_history_id: int | None,
    plan: dict[str, Any],
    reason: str | None = None,
    token_id: str = "",
) -> UserTrade:
    warning = plan.get("warning")
    if reason:
        warning = f"{warning} {reason}".strip() if warning else reason

    details = dict(plan.get("details") or {})
    details.update(
        {
            "configured_sizing_mode": plan.get("configured_sizing_mode"),
            "configured_copy_wallet_mode": plan.get("configured_copy_wallet_mode"),
            "ratio": plan.get("ratio"),
            "trade_size_pre_safety": plan.get("trade_size_pre_safety"),
            "trade_size_final": trade_size,
        }
    )

    user_trade = UserTrade(
        user_id=user_id,
        market_id=market_id,
        token_id=token_id or None,
        action=side.lower(),
        amount=trade_size,
        price=price,
        status=status,
        order_hash=order_hash,
        copied_from_wallet=trader_wallet,
        source_trade_history_id=trade_history_id,
        trader_trade_notional=plan.get("trader_trade_notional"),
        trader_wallet_balance=plan.get("trader_wallet_balance"),
        copy_wallet_base=plan.get("copy_wallet_base"),
        sizing_mode_applied=plan.get("sizing_mode_applied"),
        copy_wallet_mode_applied=plan.get("copy_wallet_mode_applied"),
        calculation_warning=warning,
        calculation_details=_json_dumps(details),
        expected_price=price,
        expected_size=trade_size,
        strategy_source="copy",
        executed_at=utc_now() if status == "executed" else None,
    )
    db.add(user_trade)
    db.commit()
    db.refresh(user_trade)
    return user_trade


def _update_streak(settings: UserSettings, won: bool) -> None:
    """Update consecutive win/loss counters for dynamic sizing."""
    if won:
        settings.consecutive_wins = (settings.consecutive_wins or 0) + 1
        settings.consecutive_losses = 0
    else:
        settings.consecutive_losses = (settings.consecutive_losses or 0) + 1
        settings.consecutive_wins = 0


# ────────────── Main execution flow ──────────────


async def execute_copy_trade(
    db: Session,
    user_id: int,
    trader_wallet: str,
    market_id: str,
    token_id: str,
    side: str,
    price: float,
    trader_amount: float,
    trade_history_id: int | None = None,
    assessment: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """
    Execute (or reject) a copy trade for a user.

    Steps:
      1. Load user settings + verify copy-trading is enabled.
      2. Calculate trade size based on risk mode.
      3. If require_ai_approval and assessment says "avoid", skip.
      4. Place order on Polymarket (idempotent on the key).
      5. Record in user_trades table.

    `idempotency_key` defaults to the source trade identity, so
    replaying the same source trade (restart, redelivery) replays
    the stored result instead of placing a second order.

    Returns a summary dict.
    """
    # 1 – Load settings
    user_settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if not user_settings or not user_settings.copy_trading_enabled:
        return {"executed": False, "reason": "Copy-trading disabled"}

    # 2 – Load user + credentials (also needed for balance-aware sizing)
    from app.models.user import User

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"executed": False, "reason": "User not found"}
    try:
        stored = load_wallet_credentials(user.wallet_address)
    except CredentialStoreError as exc:
        return {"executed": False, "reason": f"Credential store unavailable: {exc}"}
    pk = stored["private_key"] if stored else None
    creds = stored.get("clob_creds") if stored else None

    # Per-trader overrides
    followed = (
        db.query(FollowedTrader)
        .filter(
            FollowedTrader.user_id == user_id,
            FollowedTrader.trader_wallet == trader_wallet.lower(),
            FollowedTrader.is_active,
        )
        .first()
    )

    # 3 – Build sizing plan and apply global safety caps
    plan = await _plan_copy_trade_size(
        db=db,
        settings=user_settings,
        followed_trader=followed,
        trader_wallet=trader_wallet,
        trader_trade_notional=trader_amount,
        user_wallet_address=user.wallet_address,
        private_key=pk,
        clob_creds=creds,
        market_id=market_id,
        side=side,
        price=price,
    )

    if plan.get("rejection"):
        return {"executed": False, "reason": plan["rejection"]}

    size_before_safety = _to_float(plan.get("trade_size_pre_safety"))
    trade_size, rejection = _apply_global_safety_caps(
        user_settings,
        size_before_safety,
        db,
    )
    if rejection:
        rejected = _record_copy_trade_attempt(
            db=db,
            user_id=user_id,
            market_id=market_id,
            side=side,
            trade_size=0.0,
            price=price,
            status="rejected",
            trader_wallet=trader_wallet,
            order_hash=None,
            trade_history_id=trade_history_id,
            plan=plan,
            reason=rejection,
        )
        return {
            "executed": False,
            "reason": rejection,
            "trade_id": rejected.id,
            "trade_size": 0.0,
            "status": "rejected",
        }

    # 4 – AI gate
    if user_settings.require_ai_approval and assessment:
        recommendation = assessment.get("recommendation", "").lower()
        if recommendation in ("avoid", "skip"):
            logger.info("AI gate rejected copy trade for user %d: %s", user_id, recommendation)
            if trade_history_id:
                _save_assessment(db, trade_history_id, assessment)
            rejected = _record_copy_trade_attempt(
                db=db,
                user_id=user_id,
                market_id=market_id,
                side=side,
                trade_size=trade_size,
                price=price,
                status="rejected",
                trader_wallet=trader_wallet,
                order_hash=None,
                trade_history_id=trade_history_id,
                plan=plan,
                reason=f"AI recommends: {recommendation}",
            )
            return {
                "executed": False,
                "reason": f"AI recommends: {recommendation}",
                "trade_id": rejected.id,
                "trade_size": trade_size,
                "status": "rejected",
            }

    # Save assessment regardless
    if trade_history_id and assessment:
        _save_assessment(db, trade_history_id, assessment)

    # 5 – Ensure credentials exist before placing order
    if not stored and not user_settings.simulation_mode:
        rejected = _record_copy_trade_attempt(
            db=db,
            user_id=user_id,
            market_id=market_id,
            side=side,
            trade_size=trade_size,
            price=price,
            status="rejected",
            trader_wallet=trader_wallet,
            order_hash=None,
            trade_history_id=trade_history_id,
            plan=plan,
            reason="No trading credentials (re-login required)",
        )
        return {
            "executed": False,
            "reason": "No trading credentials (re-login required)",
            "trade_id": rejected.id,
            "trade_size": trade_size,
            "status": "rejected",
        }

    # 6 – Simulation mode: record trade without placing a real order
    if user_settings.simulation_mode:
        logger.info(
            "SIMULATION: user %d would trade %s %s @ %.4f size=%.2f on %s",
            user_id,
            side,
            token_id,
            price,
            trade_size,
            market_id,
        )
        sim_trade = _record_copy_trade_attempt(
            db=db,
            user_id=user_id,
            market_id=market_id,
            side=side,
            trade_size=trade_size,
            price=price,
            status="simulated",
            trader_wallet=trader_wallet,
            order_hash=None,
            trade_history_id=trade_history_id,
            plan=plan,
            reason=None,
        )
        # Update streak counters (simulate success)
        _update_streak(user_settings, won=True)
        db.commit()
        return {
            "executed": True,
            "simulated": True,
            "trade_id": sim_trade.id,
            "order_hash": None,
            "trade_size": trade_size,
            "status": "simulated",
        }

    # 7 – Place order (idempotent: a replayed key returns the
    # stored result so the same source trade can never place
    # a second order, even across restarts or Redis loss).
    idem_key = idempotency_key or (
        f"copy-trade:{trade_history_id}" if trade_history_id is not None else None
    )
    if idem_key:
        prior = get_idempotent_order_result(db, user_id, idem_key)
        if prior is not None:
            logger.info(
                "Replaying idempotent copy-trade result for user %d key %s",
                user_id,
                str(idem_key)[:32],
            )
            return prior

    result = _place_order_on_polymarket(
        private_key=pk,
        clob_creds=creds,
        token_id=token_id,
        side=side,
        price=price,
        size=trade_size,
    )

    # 8 – Record trade and update streak.  A successfully
    # submitted order is pending until a fill is observed
    # (trade_monitor WS events or the fill reconciliation
    # job); executed_at is set only on an observed fill.
    status = "pending" if result["success"] else "failed"
    user_trade = _record_copy_trade_attempt(
        db=db,
        user_id=user_id,
        market_id=market_id,
        side=side,
        trade_size=trade_size,
        price=price,
        status=status,
        trader_wallet=trader_wallet,
        order_hash=result.get("order_hash"),
        trade_history_id=trade_history_id,
        token_id=token_id,
        plan=plan,
        reason=result.get("error"),
    )

    # Update consecutive win/loss streak
    if result["success"]:
        _update_streak(user_settings, won=True)
    else:
        _update_streak(user_settings, won=False)
    db.commit()

    response = {
        "executed": result["success"],
        "trade_id": user_trade.id,
        "order_hash": result.get("order_hash"),
        "trade_size": trade_size,
        "status": status,
        "error": result.get("error"),
    }
    if idem_key:
        store_idempotent_order_result(db, user_id, idem_key, response)
    return response


def _save_assessment(db: Session, trade_history_id: int, assessment: dict[str, Any]):
    """Persist an AI assessment linked to a trade_history record."""
    try:
        record = Assessment(
            trade_history_id=trade_history_id,
            ai_score=float(assessment.get("ai_score", assessment.get("confidence", 50))),
            reasoning=assessment.get("reasoning", assessment.get("analysis", "")),
            recommendation=assessment.get("recommendation", "hold"),
            risk_level=assessment.get("risk_level", "medium"),
            market_sentiment=assessment.get("market_sentiment"),
            confidence=float(assessment.get("confidence", 50)),
        )
        db.add(record)
        db.commit()
    except Exception as e:
        logger.error("Failed to save assessment: %s", e)
        db.rollback()


async def get_copy_trade_history(
    db: Session,
    user_id: int,
    limit: int = 50,
) -> list:
    """Return recent copy trades for a user (with assessment data)."""
    trades = (
        db.query(UserTrade)
        .filter(
            UserTrade.user_id == user_id,
            UserTrade.copied_from_wallet.isnot(None),
        )
        .order_by(UserTrade.created_at.desc())
        .limit(limit)
        .all()
    )
    result = []
    for t in trades:
        row = {
            "id": t.id,
            "market_id": t.market_id,
            "action": t.action,
            "amount": t.amount,
            "price": t.price,
            "status": t.status,
            "order_hash": t.order_hash,
            "copied_from_wallet": t.copied_from_wallet,
            "source_trade_history_id": t.source_trade_history_id,
            "trader_trade_notional": t.trader_trade_notional,
            "trader_wallet_balance": t.trader_wallet_balance,
            "copy_wallet_base": t.copy_wallet_base,
            "sizing_mode_applied": t.sizing_mode_applied,
            "copy_wallet_mode_applied": t.copy_wallet_mode_applied,
            "calculation_warning": t.calculation_warning,
            "calculation_details": t.calculation_details,
            "pnl": t.pnl,
            "executed_at": t.executed_at.isoformat() if t.executed_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        result.append(row)
    return result


async def get_copy_trade_evaluation(
    db: Session,
    user_id: int,
    trader_wallet: str,
    limit: int = 50,
) -> list:
    """
    Return side-by-side source trades and copied outcomes for a trader.
    """
    wallet = trader_wallet.lower()
    source_trades = (
        db.query(TradeHistory)
        .filter(TradeHistory.wallet_address == wallet)
        .order_by(TradeHistory.timestamp.desc())
        .limit(limit)
        .all()
    )
    source_ids = [s.id for s in source_trades]

    copy_by_source: dict[int, UserTrade] = {}
    if source_ids:
        copied = (
            db.query(UserTrade)
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.copied_from_wallet == wallet,
                UserTrade.source_trade_history_id.in_(source_ids),
            )
            .order_by(UserTrade.created_at.desc())
            .all()
        )
        for row in copied:
            sid = row.source_trade_history_id
            if sid is not None and sid not in copy_by_source:
                copy_by_source[sid] = row

    rows = []
    for src in source_trades:
        cp = copy_by_source.get(src.id)
        details: dict[str, Any] = {}
        if cp and cp.calculation_details:
            try:
                details = _json_loads(cp.calculation_details)
            except Exception:
                details = {}

        ratio = details.get("ratio")
        if ratio is None and cp and cp.trader_trade_notional and cp.trader_wallet_balance:
            denom = _to_float(cp.trader_wallet_balance)
            if denom > 0:
                ratio = _to_float(cp.trader_trade_notional) / denom

        source_notional = (
            _to_float(src.notional_usdc)
            if src.notional_usdc is not None
            else _to_float(src.amount) * _to_float(src.price)
        )

        rows.append(
            {
                "source_trade_id": src.id,
                "source_trade_id_ext": src.source_trade_id_ext,
                "source_timestamp": src.timestamp.isoformat() if src.timestamp else None,
                "market_id": src.market_id,
                "side": (src.order_type or "").upper(),
                "price": _to_float(src.price),
                "source_trade_notional": round(source_notional, 6),
                "copy_trade_id": cp.id if cp else None,
                "copy_timestamp": (
                    cp.executed_at.isoformat()
                    if cp and cp.executed_at
                    else cp.created_at.isoformat()
                    if cp and cp.created_at
                    else None
                ),
                "copied_size": _to_float(cp.amount) if cp else None,
                "copy_status": cp.status if cp else "not_copied",
                "order_hash": cp.order_hash if cp else None,
                "trader_wallet_balance": _to_float(cp.trader_wallet_balance) if cp else None,
                "copy_wallet_base": _to_float(cp.copy_wallet_base) if cp else None,
                "ratio": _to_float(ratio) if ratio is not None else None,
                "sizing_mode_applied": cp.sizing_mode_applied if cp else None,
                "copy_wallet_mode_applied": cp.copy_wallet_mode_applied if cp else None,
                "warning": cp.calculation_warning if cp else None,
            }
        )

    return rows


async def get_daily_copy_pnl(db: Session, user_id: int) -> float:
    """Sum of PnL from today's copy trades."""
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    result = (
        db.query(func.coalesce(func.sum(UserTrade.pnl), 0.0))
        .filter(
            UserTrade.user_id == user_id,
            UserTrade.copied_from_wallet.isnot(None),
            UserTrade.executed_at >= today_start,
        )
        .scalar()
    )
    return float(result or 0.0)


async def execute_aggregated_trade(aggregated: dict) -> dict:
    """Execute a VWAP-aggregated trade as a single order.

    Called by the trade_aggregation_service when a buffer window closes.
    Uses multi-phase execution for better fill rates.
    """
    user_id = aggregated.get("user_id")
    token_id = aggregated.get("token_id", "")
    side = aggregated.get("side", "BUY")
    size = aggregated.get("aggregated_size", 0)
    price = aggregated.get("vwap_price", 0)

    if not user_id or size <= 0 or price <= 0:
        logger.warning("Invalid aggregated trade params: %s", aggregated)
        return {"success": False, "error": "Invalid aggregated trade parameters"}

    db = SessionLocal()
    try:
        from app.models.user import User

        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return {"success": False, "error": f"User {user_id} not found"}

        from app.security.credential_store import get_decrypted_private_key

        private_key = get_decrypted_private_key(db, user.id)
        if not private_key:
            return {"success": False, "error": "No private key available"}

        result = _place_order_multi_phase(
            private_key=private_key,
            clob_creds=None,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            enable_phases=True,
        )

        logger.info(
            "Aggregated trade executed: user=%d token=%s side=%s size=%.2f vwap=%.4f success=%s",
            user_id,
            token_id[:20],
            side,
            size,
            price,
            result.get("success"),
        )
        return result
    except Exception as e:
        logger.error("Failed to execute aggregated trade: %s", e)
        return {"success": False, "error": str(e)}
    finally:
        db.close()
