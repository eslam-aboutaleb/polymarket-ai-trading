"""
Shared pre-trade correctness primitives (plan 01).

This module owns the money-path guards that must behave identically
for manual and copy-traded orders:

- ``apply_global_safety_caps`` — the multi-layer risk gate extracted
  from ``copy_trade_service`` so the manual execute endpoint runs the
  same halt / loss-limit / drawdown / position-cap checks before
  submitting an order.  Exposure caps also count pending (unfilled)
  notional: a resting GTC buy is committed capital even though it has
  not filled yet, so stacking several max-size pending orders must not
  be allowed to exceed the cap once they all fill.
- Order idempotency — ``(user_id, key) -> result`` storage with a
  24h TTL in the shared cache (Redis when configured) and a DB
  fallback table, so a retried or duplicated request replays the
  original result instead of placing a second order on the exchange.
- CLOB order verification — recomputes a signed order's EIP-712
  struct hash (the exchange's order id) and queries the exchange for
  whether that order already landed, so an ambiguous ``post_order``
  error is never retried blindly.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.user_settings import UserSettings
from app.models.user_trade import UserTrade
from app.services.polymarket_service import POLYMARKET_CLOB_API
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


def _to_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


# ────────────── Risk aggregates ──────────────


def _daily_loss_so_far(db: Session, user_id: int) -> float:
    """Sum of negative PnL from copy trades executed today (UTC)."""
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    result = (
        db.query(func.coalesce(func.sum(UserTrade.pnl), 0.0))
        .filter(
            UserTrade.user_id == user_id,
            UserTrade.copied_from_wallet.isnot(None),
            UserTrade.executed_at >= today_start,
            UserTrade.pnl < 0,
        )
        .scalar()
    )
    return abs(float(result or 0.0))


def _monthly_loss_so_far(db: Session, user_id: int) -> float:
    """Sum of negative PnL from copy trades executed this calendar month (UTC)."""
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    result = (
        db.query(func.coalesce(func.sum(UserTrade.pnl), 0.0))
        .filter(
            UserTrade.user_id == user_id,
            UserTrade.copied_from_wallet.isnot(None),
            UserTrade.executed_at >= month_start,
            UserTrade.pnl < 0,
        )
        .scalar()
    )
    return abs(float(result or 0.0))


def _total_pnl_since_start(db: Session, user_id: int) -> float:
    """Net PnL across all copy trades for a user (positive = profit, negative = loss)."""
    result = (
        db.query(func.coalesce(func.sum(UserTrade.pnl), 0.0))
        .filter(
            UserTrade.user_id == user_id,
            UserTrade.copied_from_wallet.isnot(None),
        )
        .scalar()
    )
    return float(result or 0.0)


def _pending_exposure_so_far(db: Session, user_id: int) -> float:
    """Notional of the user's pending (submitted, unfilled) orders.

    Pending orders are committed capital: a resting GTC buy can fill
    at any time, so exposure caps must count them or a user could
    stack several max-size pending orders and blow through the cap
    once they all fill.
    """
    result = (
        db.query(func.coalesce(func.sum(UserTrade.amount), 0.0))
        .filter(
            UserTrade.user_id == user_id,
            UserTrade.status == "pending",
        )
        .scalar()
    )
    return abs(float(result or 0.0))


# ────────────── Global safety caps ──────────────


def apply_global_safety_caps(
    settings: UserSettings,
    trade_size: float,
    db: Session,
) -> tuple[float, str | None]:
    """
    Apply multi-layer risk protection as the final guardrail,
    regardless of per-trader sizing mode.

    Layers:
      0. Trading halt / cooldown check
      1. Monthly loss limit
      2. Max drawdown from peak capital
      3. Total loss halt (percentage of initial capital)
      4. Max position size cap (counting pending exposure)
      5. Daily loss limit
    """
    size = _to_float(trade_size)
    if size <= 0:
        return 0.0, "Calculated trade size is zero"

    # Layer 0 – Trading halt / cooldown
    if settings.trading_halted:
        reason = settings.halt_reason or "Trading halted by risk system"
        return 0.0, reason
    if settings.cooldown_until:
        now = datetime.now(UTC)
        if now < settings.cooldown_until:
            remaining = int((settings.cooldown_until - now).total_seconds() / 60)
            return 0.0, f"Cooldown active ({remaining}m remaining)"
        # Cooldown expired — clear it
        settings.cooldown_until = None

    # Layer 1 – Monthly loss limit
    monthly_limit = _to_float(settings.monthly_loss_limit)
    if monthly_limit > 0:
        monthly_loss = _monthly_loss_so_far(db, settings.user_id)
        monthly_remaining = monthly_limit - monthly_loss
        if monthly_remaining <= 0:
            return 0.0, "Monthly loss limit reached"
        size = min(size, monthly_remaining)

    # Layer 2 – Max drawdown from peak capital
    max_dd_pct = _to_float(settings.max_drawdown_pct)
    peak = _to_float(settings.peak_capital)
    if max_dd_pct > 0 and peak > 0:
        total_pnl = _total_pnl_since_start(db, settings.user_id)
        initial = _to_float(settings.initial_capital, peak)
        current_capital = initial + total_pnl
        # Update peak if current is higher
        if current_capital > peak:
            settings.peak_capital = current_capital
            peak = current_capital
        drawdown_pct = ((peak - current_capital) / peak) * 100.0 if peak > 0 else 0.0
        if drawdown_pct >= max_dd_pct:
            settings.trading_halted = True
            settings.halt_reason = (
                f"Max drawdown {max_dd_pct}% breached (current: {drawdown_pct:.1f}%)"
            )
            return 0.0, settings.halt_reason

    # Layer 3 – Total loss halt
    total_halt_pct = _to_float(settings.total_loss_halt_pct)
    initial_cap = _to_float(settings.initial_capital)
    if total_halt_pct > 0 and initial_cap > 0:
        total_pnl = _total_pnl_since_start(db, settings.user_id)
        total_loss_pct = (abs(min(0, total_pnl)) / initial_cap) * 100.0
        if total_loss_pct >= total_halt_pct:
            settings.trading_halted = True
            settings.halt_reason = f"Total loss {total_halt_pct}% of initial capital breached"
            return 0.0, settings.halt_reason

    # Layer 4 – Max position size, counting pending exposure.
    # A resting GTC buy is not a fill yet, but its notional is
    # already committed, so the remaining position allowance is
    # the cap minus the user's pending notional.
    max_pos = _to_float(settings.max_position_size)
    if max_pos > 0:
        pending_notional = _pending_exposure_so_far(db, settings.user_id)
        remaining_pos = max_pos - pending_notional
        if remaining_pos <= 0:
            return 0.0, "Max position size reached (pending exposure)"
        size = min(size, remaining_pos)

    # Layer 5 – Daily loss limit
    daily_loss = _daily_loss_so_far(db, settings.user_id)
    remaining = _to_float(settings.daily_loss_limit, 500.0) - daily_loss
    if remaining <= 0:
        return 0.0, "Daily loss limit reached"
    size = min(size, remaining)

    if size <= 0:
        return 0.0, "Calculated trade size is zero after safety caps"

    return round(size, 2), None


# ────────────── Order idempotency ──────────────

IDEMPOTENCY_TTL_SECONDS = 86400  # 24h


class OrderIdempotencyKey(Base):
    """Maps (user_id, idempotency_key) to the stored order result.

    Durable fallback for the shared cache: when Redis is unavailable
    (or lost), a replayed Idempotency-Key still returns the original
    order result instead of placing a second order on the exchange.
    """

    __tablename__ = "order_idempotency_keys"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_order_idempotency_user_key",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    idempotency_key = Column(String(200), nullable=False, index=True)
    result_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def __repr__(self):
        return f"<OrderIdempotencyKey(user={self.user_id}, key={self.idempotency_key[:16]}…)>"


_idempotency_cache = None


def _get_idempotency_cache():
    """Lazy-init the shared cache so importing this module never
    fails when the cache backend is misconfigured."""
    global _idempotency_cache
    if _idempotency_cache is None:
        from app.utils.cache import get_cache

        _idempotency_cache = get_cache("order_idempotency")
    return _idempotency_cache


def _idempotency_cache_key(user_id: int | None, key: str) -> str:
    return f"order:{user_id}:{key}"


def get_idempotent_order_result(
    db: Session,
    user_id: int | None,
    key: str | None,
) -> dict | None:
    """Return the previously stored result for (user_id, key), or None.

    Reads the shared cache first (Redis when configured) and falls
    back to the DB table, so replays work across workers, restarts
    and Redis outages.
    """
    if not key:
        return None
    cache_key = _idempotency_cache_key(user_id, key)
    try:
        cached = _get_idempotency_cache().get(cache_key)
        if cached is not None:
            return cached
    except Exception as e:
        logger.warning("Idempotency cache read failed for user %s: %s", user_id, e)
    try:
        row = (
            db.query(OrderIdempotencyKey)
            .filter(
                OrderIdempotencyKey.user_id == user_id,
                OrderIdempotencyKey.idempotency_key == key,
            )
            .first()
        )
        if row is not None:
            return _json_loads(row.result_json)
    except Exception as e:
        logger.warning("Idempotency DB read failed for user %s: %s", user_id, e)
    return None


def store_idempotent_order_result(
    db: Session,
    user_id: int | None,
    key: str | None,
    result: dict,
) -> None:
    """Persist the order result for (user_id, key) for 24h.

    Writes the shared cache (fast replay) and the DB table (durable
    replay when Redis is unavailable).  Both writes are best-effort:
    an idempotency-store failure must never fail the order itself.
    """
    if not key:
        return
    cache_key = _idempotency_cache_key(user_id, key)
    try:
        _get_idempotency_cache().set(
            cache_key,
            result,
            ttl_seconds=IDEMPOTENCY_TTL_SECONDS,
        )
    except Exception as e:
        logger.warning("Idempotency cache write failed for user %s: %s", user_id, e)
    try:
        existing = (
            db.query(OrderIdempotencyKey)
            .filter(
                OrderIdempotencyKey.user_id == user_id,
                OrderIdempotencyKey.idempotency_key == key,
            )
            .first()
        )
        if existing is not None:
            existing.result_json = _json_dumps(result)
        else:
            db.add(
                OrderIdempotencyKey(
                    user_id=user_id,
                    idempotency_key=key,
                    result_json=_json_dumps(result),
                )
            )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("Idempotency DB write failed for user %s: %s", user_id, e)


# ────────────── CLOB order verification ──────────────

CLOB_TRADES_PATH = "/data/trades"
CLOB_ORDERS_PATH = "/data/orders"


def build_clob_client(
    private_key: str,
    clob_creds: dict | None,
    proxy_address: str | None = None,
):
    """Build an L2-authenticated ClobClient for order queries.

    Uses POLY_PROXY (signature_type=1) with the wallet's proxy
    address as funder when known, mirroring how orders are placed,
    so queries see the same wallet the exchange attributes orders to.
    """
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    client = ClobClient(
        host=POLYMARKET_CLOB_API,
        chain_id=137,
        key=private_key,
        signature_type=1,
        funder=proxy_address,
    )
    if clob_creds:
        client.set_api_creds(
            ApiCreds(
                api_key=clob_creds["api_key"],
                api_secret=clob_creds["api_secret"],
                api_passphrase=clob_creds["api_passphrase"],
            )
        )
    else:
        client.set_api_creds(client.create_or_derive_api_creds())
    return client


def compute_order_hash(client, signed_order, token_id: str) -> str:
    """Recompute the EIP-712 struct hash the exchange uses as order id.

    The exchange identifies an order by the EIP-712 struct hash of
    its order struct (domain: "Polymarket CTF Exchange" v1, verifying
    contract = the chain's CTF exchange address).  Recomputing it
    from a signed order lets us ask the exchange whether an order
    whose ``post_order`` failed ambiguously actually landed.
    """
    from eth_utils import keccak
    from poly_eip712_structs import make_domain
    from py_order_utils.utils import prepend_zx

    neg_risk = bool(client.get_neg_risk(token_id))
    exchange_address = client.get_exchange_address(neg_risk=neg_risk)
    domain = make_domain(
        name="Polymarket CTF Exchange",
        version="1",
        chainId=str(client.chain_id),
        verifyingContract=exchange_address,
    )
    return prepend_zx(keccak(signed_order.order.signable_bytes(domain=domain)).hex())


def clob_trades_for_order(client, order_id: str) -> list[dict]:
    """Return the fills of one order via ``GET /data/trades?order_id=``.

    py-clob-client's ``TradeParams`` does not expose the ``order_id``
    filter, so the query is built directly and signed with the
    client's L2 headers (the HMAC covers the request path only,
    matching the client's own paginated queries).
    """
    from py_clob_client.clob_types import RequestArgs
    from py_clob_client.headers.headers import create_level_2_headers
    from py_clob_client.http_helpers.helpers import get as clob_get

    request_args = RequestArgs(method="GET", request_path=CLOB_TRADES_PATH)
    headers = create_level_2_headers(client.signer, client.creds, request_args)
    url = f"{client.host}{CLOB_TRADES_PATH}?order_id={order_id}"
    resp = clob_get(url, headers=headers)
    if isinstance(resp, dict):
        data = resp.get("data", [])
        return data if isinstance(data, list) else []
    if isinstance(resp, list):
        return resp
    return []


def order_landed_on_clob(client, signed_order, token_id: str) -> str | None:
    """Check whether a signed order already landed on the exchange.

    Returns the order hash when the order is open or already
    filled, an empty string when the exchange confirms it is
    neither, and None when the check itself failed (unknown).
    Callers must not retry on a hash or on None — only an
    empty string is a confirmed safe-to-retry.
    """
    try:
        order_hash = compute_order_hash(client, signed_order, token_id)
    except Exception as e:
        logger.warning("Could not compute order hash for token %s: %s", token_id[:16], e)
        return None
    try:
        open_order = client.get_order(order_hash)
        if open_order:
            return order_hash
    except Exception:
        # Not resting as an open order (404) — check the trade feed.
        logger.debug("Order %s is not resting as an open order", order_hash[:16])
    try:
        trades = clob_trades_for_order(client, order_hash)
        return order_hash if trades else ""
    except Exception as e:
        logger.warning(
            "CLOB trade lookup failed for order %s: %s",
            order_hash[:16],
            e,
        )
        return None
