"""External WebSocket API ``/ws/v1`` (plan 06 Phase B).

A browser client already gets SSE (``/api/portfolio/prices/stream``);
this is the machine channel, and it authenticates like the REST external
API — an ``X-API-Key`` header on the handshake, or an ``api_key`` query
parameter for clients that cannot set handshake headers. Both carry the
same key, and neither is a cookie or a bearer token: the external
namespace never accepts a browser session.

Protocol (one JSON frame per message, in both directions):

* inbound ``{"action": "subscribe"|"unsubscribe", "channel": ..., "token_ids": [...]}``
  where the channel is one of ``positions`` / ``prices`` / ``fills``.
* outbound ``{"channel": ..., "data": ...}`` plus ``{"channel": "heartbeat"}``
  every ``HEARTBEAT_SECONDS`` so an idle socket is provably alive.

Channels:

* ``prices`` — reuses the process-level CLOB WebSocket manager
  (``clob_ws_manager``) that already feeds the SSE stream, so this
  channel opens no second connection to the exchange and inherits its
  reconnect/backoff behaviour.
* ``positions`` / ``fills`` — poll snapshots. They read the local
  account records (the same rows ``/positions``, ``/fills`` and the
  audit trail read) rather than the exchange, so a fill event is
  consistent with what the audit export reports.

Scope enforcement is the same as REST: a key without ``read`` is
rejected at the handshake with an explicit close code, so a client that
only holds ``trade`` cannot subscribe to a read channel.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.api.deps import API_KEY_HEADER
from app.security.credential_store import CredentialStoreError, load_wallet_credentials
from app.services.api_key_service import (
    SCOPE_READ,
    AuthContext,
    authenticate_api_key,
)
from app.services.clob_ws_manager import get_clob_ws_manager, is_stale_tick
from app.services.execution_analytics import _FILLED_STATUSES
from app.services.polymarket_service import get_polymarket_service
from app.utils.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

#: Query-parameter fallback for handshakes that cannot carry headers.
API_KEY_QUERY_PARAM = "api_key"

CHANNEL_POSITIONS = "positions"
CHANNEL_PRICES = "prices"
CHANNEL_FILLS = "fills"
CHANNELS = (CHANNEL_POSITIONS, CHANNEL_PRICES, CHANNEL_FILLS)

HEARTBEAT_SECONDS = 20.0
POSITIONS_POLL_SECONDS = 5.0
FILLS_POLL_SECONDS = 3.0

#: Close codes: 4401 unauthenticated, 4403 wrong scope. Non-standard but
#: in the 4xxx application range, so a client can tell "bad credential"
#: from "server closed the socket".
WS_CLOSE_UNAUTHENTICATED = 4401
WS_CLOSE_FORBIDDEN_SCOPE = 4403

#: Cap on token ids a single connection may subscribe to, matching the
#: CLOB manager's own subscription cap.
MAX_SUBSCRIBED_TOKENS = 50


def _resolve_handshake_key(websocket: WebSocket, api_key: str | None) -> str | None:
    """Read the API key from the handshake header or the query string.

    Only the prefix is ever logged; the query-string path is the
    fallback for clients that cannot set handshake headers and is
    documented as the less private of the two, because a URL can end up
    in a proxy log.
    """
    raw_header = (websocket.headers.get(API_KEY_HEADER) or "").strip()
    if raw_header:
        return raw_header
    raw_query = (api_key or "").strip()
    if raw_query:
        logger.info("WebSocket handshake used the %s query fallback", API_KEY_QUERY_PARAM)
    return raw_query or None


async def _authenticate_websocket(
    websocket: WebSocket,
    db: Session,
    api_key: str | None,
) -> AuthContext | None:
    """Authenticate a handshake, closing the socket on any failure."""
    raw_key = _resolve_handshake_key(websocket, api_key)
    if not raw_key:
        await websocket.close(code=WS_CLOSE_UNAUTHENTICATED, reason="Missing API key")
        return None
    try:
        context = authenticate_api_key(db, raw_key)
    except ValueError as exc:
        logger.error("WebSocket API key authentication unavailable: %s", exc)
        await websocket.close(code=WS_CLOSE_UNAUTHENTICATED, reason="Authentication unavailable")
        return None
    if context is None:
        await websocket.close(code=WS_CLOSE_UNAUTHENTICATED, reason="Invalid API key")
        return None
    if not context.has_scope(SCOPE_READ):
        await websocket.close(
            code=WS_CLOSE_FORBIDDEN_SCOPE,
            reason="API key requires the read scope",
        )
        return None
    return context


def _parse_frame(raw: str | bytes) -> dict | None:
    """Decode an inbound frame, or return None when it is not actionable.

    A malformed frame is ignored rather than fatal: one bad client must
    not cost the socket, but it also must not be able to crash the
    handler.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not raw or not raw.strip():
        return None
    with contextlib.suppress(json.JSONDecodeError):
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    return None


def _frame(channel: str, data: Any) -> str:
    return json.dumps({"channel": channel, "data": data}, default=str)


async def _send_json(websocket: WebSocket, payload: str) -> bool:
    """Send one frame; False once the socket is gone."""
    try:
        await websocket.send_text(payload)
        return True
    except (RuntimeError, WebSocketDisconnect):
        # Starlette raises RuntimeError once the connection is closed.
        return False


async def _heartbeat_loop(websocket: WebSocket) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        if not await _send_json(websocket, _frame("heartbeat", {"ts": True})):
            return


async def _positions_loop(websocket: WebSocket, context: AuthContext, db: Session) -> None:
    """Poll the account's open positions on a fixed cadence."""
    service = get_polymarket_service()
    while True:
        await asyncio.sleep(POSITIONS_POLL_SECONDS)
        try:
            stored = load_wallet_credentials(context.wallet_address)
        except CredentialStoreError:
            stored = None
        if not stored:
            continue
        try:
            positions = await service.get_positions(
                context.wallet_address,
                private_key=stored["private_key"],
                clob_creds=stored.get("clob_creds"),
            )
        except Exception as exc:
            logger.debug("WS positions snapshot failed: %s", exc)
            continue
        if not await _send_json(
            websocket,
            _frame(CHANNEL_POSITIONS, {"positions": positions, "account_id": context.account_id}),
        ):
            return


async def _fills_loop(websocket: WebSocket, context: AuthContext, db: Session) -> None:
    """Poll newly observed fills and emit each one once."""
    from app.models.user_trade import UserTrade

    last_seen_id = _latest_trade_id(db, context)
    while True:
        await asyncio.sleep(FILLS_POLL_SECONDS)
        query = db.query(UserTrade).filter(
            UserTrade.user_id == context.user_id,
            UserTrade.status.in_(_FILLED_STATUSES),
        )
        if context.account_id is not None:
            query = query.filter(UserTrade.account_id == context.account_id)
        if last_seen_id is not None:
            query = query.filter(UserTrade.id > last_seen_id)
        rows = query.order_by(UserTrade.id.asc()).limit(50).all()
        for row in rows:
            last_seen_id = max(last_seen_id or 0, row.id)
            if not await _send_json(
                websocket,
                _frame(
                    CHANNEL_FILLS,
                    {
                        "trade_id": row.id,
                        "market_id": row.market_id,
                        "token_id": row.token_id,
                        "action": row.action,
                        "filled_price": row.filled_price,
                        "filled_size": row.filled_size,
                        "fee_paid": row.fee_paid,
                        "slippage_bps": row.slippage_bps,
                        "order_hash": row.order_hash,
                        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
                    },
                ),
            ):
                return


def _latest_trade_id(db: Session, context: AuthContext) -> int | None:
    """Baseline id so a subscriber gets new fills, not the whole history."""
    from app.models.user_trade import UserTrade

    query = db.query(UserTrade.id).filter(UserTrade.user_id == context.user_id)
    if context.account_id is not None:
        query = query.filter(UserTrade.account_id == context.account_id)
    latest = query.order_by(UserTrade.id.desc()).first()
    return int(latest[0]) if latest else None


async def _prices_loop(
    websocket: WebSocket,
    context: AuthContext,
    token_ids: list[str],
    stop: asyncio.Event,
) -> None:
    """Fan CLOB ticks out to this connection.

    Ticks arrive on the shared manager's callback thread (the event loop
    the manager's own connection runs on) and are queued, so the socket
    never blocks the exchange feed. Stale ticks are dropped exactly as
    the SSE stream drops them.
    """
    manager = get_clob_ws_manager()
    queue: asyncio.Queue = asyncio.Queue()
    subscriber_id = manager.register_subscriber(queue.put_nowait)
    subscribed: list[str] = []
    try:
        subscribed = await manager.subscribe(token_ids)
        while not stop.is_set():
            try:
                tick = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                continue
            if is_stale_tick(tick.get("ts")):
                continue
            if not await _send_json(websocket, _frame(CHANNEL_PRICES, tick)):
                return
    finally:
        manager.unregister_subscriber(subscriber_id)
        if subscribed:
            with contextlib.suppress(Exception):
                await manager.unsubscribe(subscribed)


@router.websocket("/ws/v1")
async def external_websocket(
    websocket: WebSocket,
    api_key: str | None = Query(default=None, alias=API_KEY_QUERY_PARAM),
    db: Session = Depends(get_db),
) -> None:
    """Multiplexed external feed: positions, prices and fills."""
    await websocket.accept()
    context = await _authenticate_websocket(websocket, db, api_key)
    if context is None:
        return

    logger.info(
        "External WebSocket connected (user_id=%s key=%s)",
        context.user_id,
        context.key_prefix,
    )

    tasks: dict[str, asyncio.Task] = {
        "heartbeat": asyncio.create_task(_heartbeat_loop(websocket)),
    }
    subscribed_tokens: list[str] = []
    prices_stop = asyncio.Event()

    async def _stop_prices() -> None:
        prices_stop.set()
        task = tasks.pop(CHANNEL_PRICES, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _start_prices(token_ids: list[str]) -> None:
        await _stop_prices()
        prices_stop.clear()
        if not token_ids:
            return
        tasks[CHANNEL_PRICES] = asyncio.create_task(
            _prices_loop(websocket, context, token_ids, prices_stop)
        )

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            frame = _parse_frame(message.get("text") or message.get("bytes") or "")
            if frame is None:
                continue

            action = str(frame.get("action") or "").lower()
            channel = str(frame.get("channel") or "").lower()
            if action not in ("subscribe", "unsubscribe"):
                await _send_json(
                    websocket,
                    _frame("error", {"detail": "action must be subscribe or unsubscribe"}),
                )
                continue

            if channel == CHANNEL_PRICES:
                if action == "subscribe":
                    requested = [
                        str(token).strip()
                        for token in (frame.get("token_ids") or [])
                        if str(token).strip()
                    ]
                    # Deduplicated here so the ack and the manager's
                    # own subscription agree on what was accepted.
                    requested = list(dict.fromkeys(requested))
                    if len(requested) > MAX_SUBSCRIBED_TOKENS:
                        await _send_json(
                            websocket,
                            _frame(
                                "error",
                                {"detail": f"Too many token_ids (max {MAX_SUBSCRIBED_TOKENS})"},
                            ),
                        )
                        continue
                    subscribed_tokens = requested
                    await _start_prices(requested)
                else:
                    subscribed_tokens = []
                    await _stop_prices()
                await _send_json(
                    websocket,
                    _frame(
                        channel,
                        {"subscribed": action == "subscribe", "token_ids": subscribed_tokens},
                    ),
                )
                continue

            if channel not in (CHANNEL_POSITIONS, CHANNEL_FILLS):
                await _send_json(
                    websocket,
                    _frame("error", {"detail": f"Unknown channel: {channel or '(missing)'}"}),
                )
                continue

            if action == "subscribe":
                if channel in tasks:
                    continue
                loop = _positions_loop if channel == CHANNEL_POSITIONS else _fills_loop
                tasks[channel] = asyncio.create_task(loop(websocket, context, db))
            else:
                task = tasks.pop(channel, None)
                if task is not None:
                    task.cancel()
            await _send_json(
                websocket,
                _frame(channel, {"subscribed": action == "subscribe"}),
            )
    except WebSocketDisconnect:
        logger.info("External WebSocket disconnected (user_id=%s)", context.user_id)
    finally:
        prices_stop.set()
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
