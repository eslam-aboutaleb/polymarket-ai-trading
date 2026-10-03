"""
CLOB WebSocket connection manager — real-time price ticks.

Maintains a single process-level WebSocket connection to Polymarket's
public market channel (wss://ws-subscriptions-clob.polymarket.com/ws/market)
and fans out price ticks to registered subscribers (the portfolio price
SSE stream).

Design notes:
  * The connection is lazy — it opens when the first token is subscribed
    and closes when the last one is unsubscribed, so an idle process
    holds no WebSocket.
  * Reconnects use exponential backoff and re-send the full subscription
    set, so a dropped connection heals without caller involvement.
  * The market channel expects an application-level heartbeat: a `PING`
    text frame every 10 s (the server replies `PONG`).
  * The subscription set is capped: the exchange silently drops oversized
    subscription sets, so excess tokens are rejected with a warning and
    keep flowing through the polling fallback instead.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

POLYMARKET_CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Wire event types that carry a price for a single asset.
PRICE_EVENT_TYPES = ("price_change", "last_trade_price")
# Event types that carry a full or top-of-book snapshot; the tick price
# is the bid/ask midpoint.
BOOK_EVENT_TYPES = ("book", "best_bid_ask")

INITIAL_RECONNECT_DELAY_SECONDS = 1.0
MAX_RECONNECT_DELAY_SECONDS = 30.0
IDLE_CHECK_INTERVAL_SECONDS = 0.5
HEARTBEAT_INTERVAL_SECONDS = 10.0

# Ticks older than this are considered stale and dropped.
STALE_TICK_SECONDS = 30.0

# A tick handler receives {"token_id", "price", "ts"}.
PriceTickHandler = Callable[[dict], None]

try:
    import websockets

    _WEBSOCKETS_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    websockets = None  # type: ignore[assignment]
    _WEBSOCKETS_AVAILABLE = False

try:
    _settings = get_settings()
    _MAX_SUBSCRIPTIONS = max(1, int(_settings.price_stream_max_subscriptions))
except Exception:
    _MAX_SUBSCRIPTIONS = 50


def _to_float(value, default=0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _event_timestamp_iso(data: dict, fallback: datetime) -> str:
    """Best-effort ISO timestamp from a wire event's epoch-ms timestamp."""
    raw = data.get("timestamp")
    if raw is not None:
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=UTC).isoformat()
        except (ValueError, OSError, TypeError):
            pass
    return fallback.isoformat()


def is_stale_tick(ts: str | None, now: datetime | None = None) -> bool:
    """True when a tick timestamp is older than the staleness window."""
    if not ts:
        return False
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return (reference - parsed).total_seconds() > STALE_TICK_SECONDS


class ClobWsManager:
    """Single-connection fan-out manager for CLOB market price ticks."""

    def __init__(
        self,
        url: str = POLYMARKET_CLOB_WS_URL,
        max_subscriptions: int = _MAX_SUBSCRIPTIONS,
    ):
        self._url = url
        self._max_subscriptions = max_subscriptions
        # token_id → refcount (multiple SSE clients may share a token)
        self._subscriptions: dict[str, int] = {}
        self._subscribers: dict[int, PriceTickHandler] = {}
        self._next_subscriber_id = 0
        self._ws: Any = None
        self._is_connected = False
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._subscribe_churn = 0
        self._unsubscribe_churn = 0

    # ── Lifecycle ───────────────────────────────────────────

    def start(self) -> None:
        """Spawn the connection loop (idles until the first subscription)."""
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run())
            logger.info("CLOB WS manager started (url=%s)", self._url)

    async def stop(self) -> None:
        """Cancel the connection loop and release the WebSocket."""
        self._stop_event.set()
        self._wake_event.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._ws = None
        self._is_connected = False
        logger.info("CLOB WS manager stopped")

    # ── Subscription management ─────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def subscription_count(self) -> int:
        return len(self._subscriptions)

    async def subscribe(self, token_ids: list[str]) -> list[str]:
        """Subscribe to token ids; returns the accepted (deduplicated) ids."""
        accepted: list[str] = []
        async with self._lock:
            for raw in token_ids:
                token_id = str(raw or "").strip()
                if not token_id or token_id in accepted:
                    continue
                if token_id not in self._subscriptions:
                    if len(self._subscriptions) >= self._max_subscriptions:
                        logger.warning(
                            "CLOB WS subscription cap (%d) reached; dropping token %s",
                            self._max_subscriptions,
                            token_id[:16],
                        )
                        continue
                    self._subscriptions[token_id] = 0
                    self._subscribe_churn += 1
                self._subscriptions[token_id] += 1
                accepted.append(token_id)
        if accepted:
            self._wake_event.set()
            if self._is_connected and self._ws is not None:
                await self._send(self._ws, {"assets_ids": accepted, "type": "market"})
        return accepted

    async def unsubscribe(self, token_ids: list[str]) -> None:
        """Drop subscriptions; the connection closes when the last one goes."""
        removed: list[str] = []
        async with self._lock:
            for raw in token_ids:
                token_id = str(raw or "").strip()
                refcount = self._subscriptions.get(token_id, 0)
                if refcount <= 0:
                    continue
                if refcount == 1:
                    del self._subscriptions[token_id]
                    self._unsubscribe_churn += 1
                    removed.append(token_id)
                else:
                    self._subscriptions[token_id] = refcount - 1
        if removed and self._is_connected and self._ws is not None:
            await self._send(
                self._ws,
                {"assets_ids": removed, "operation": "unsubscribe"},
            )
        if not self._subscriptions and self._ws is not None:
            # Closing returns the session loop to its idle wait.
            with contextlib.suppress(Exception):
                await self._ws.close()

    # ── Subscriber fan-out ──────────────────────────────────

    def register_subscriber(self, handler: PriceTickHandler) -> int:
        """Register a tick callback; returns its subscriber id."""
        self._next_subscriber_id += 1
        self._subscribers[self._next_subscriber_id] = handler
        return self._next_subscriber_id

    def unregister_subscriber(self, subscriber_id: int) -> None:
        self._subscribers.pop(subscriber_id, None)

    # ── Connection loop ─────────────────────────────────────

    async def _run(self) -> None:
        if not _WEBSOCKETS_AVAILABLE:
            logger.info("websockets package not installed; CLOB WS disabled")
            return
        delay = INITIAL_RECONNECT_DELAY_SECONDS
        while not self._stop_event.is_set():
            if not self._subscriptions:
                # Clear before the re-check so a subscribe that lands
                # between the check and the wait is not lost.
                self._wake_event.clear()
                if not self._subscriptions:
                    await self._wake_event.wait()
                continue
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("CLOB WS session ended: %s", exc)
            if self._stop_event.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY_SECONDS)

    async def _session(self) -> None:
        if websockets is None:  # pragma: no cover - guarded by _run
            return
        async with websockets.connect(
            self._url,
            ping_interval=25,
            ping_timeout=20,
            close_timeout=5,
            additional_headers={"User-Agent": "polymarket-ai/1.0"},
        ) as ws:
            self._ws = ws
            self._is_connected = True
            logger.info("CLOB WS connected to %s", self._url)
            heartbeat = asyncio.create_task(self._heartbeat(ws))
            try:
                async with self._lock:
                    token_ids = list(self._subscriptions)
                if token_ids:
                    await self._send(ws, {"assets_ids": token_ids, "type": "market"})
                async for message in ws:
                    self._handle_message(message)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                self._ws = None
                self._is_connected = False

    async def _heartbeat(self, ws: Any) -> None:
        """The market channel expects application-level PING frames."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            await ws.send("PING")

    async def _send(self, ws: Any, payload: dict) -> None:
        try:
            await ws.send(json.dumps(payload))
        except Exception as exc:
            # A failed send means the connection dropped; the reconnect
            # loop re-sends the full subscription set anyway.
            logger.debug("CLOB WS send failed (will resubscribe): %s", exc)

    # ── Inbound events ──────────────────────────────────────

    def _handle_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            message = message.decode("utf-8", "replace")
        if message in ("PING", "PONG"):
            return
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        for tick in self._extract_ticks(data):
            self._fan_out(tick)

    def _extract_ticks(self, data: dict) -> list[dict]:
        """Map a wire event to price ticks (usually zero or one)."""
        event_type = data.get("event_type") or data.get("type") or ""
        now = datetime.now(UTC)
        ts = _event_timestamp_iso(data, now)

        if event_type in PRICE_EVENT_TYPES:
            if event_type == "last_trade_price":
                candidates = [data]
            else:
                candidates = data.get("price_changes") or []
            ticks = []
            for entry in candidates:
                token_id = entry.get("asset_id") or entry.get("token_id")
                price = _to_float(entry.get("price"))
                if token_id and 0 < price < 1:
                    ticks.append({"token_id": str(token_id), "price": price, "ts": ts})
            return ticks

        if event_type in BOOK_EVENT_TYPES:
            token_id = data.get("asset_id") or data.get("token_id")
            if not token_id:
                return []
            if event_type == "book":
                bids = data.get("bids") or []
                asks = data.get("asks") or []
                best_bid = max((_to_float(b.get("price")) for b in bids), default=0.0)
                best_ask = min(
                    (_to_float(a.get("price")) for a in asks if _to_float(a.get("price")) > 0),
                    default=0.0,
                )
            else:
                best_bid = _to_float(data.get("best_bid"))
                best_ask = _to_float(data.get("best_ask"))
            if best_bid <= 0 or best_ask <= 0:
                return []
            return [
                {
                    "token_id": str(token_id),
                    "price": (best_bid + best_ask) / 2,
                    "ts": ts,
                }
            ]

        return []

    def _fan_out(self, tick: dict) -> None:
        for handler in list(self._subscribers.values()):
            try:
                handler(tick)
            except Exception:
                logger.exception("CLOB WS tick handler failed")


# ── Process-level singleton ─────────────────────────────────

_manager: ClobWsManager | None = None


def get_clob_ws_manager() -> ClobWsManager:
    """Return the process-level manager, creating it on first use."""
    global _manager
    if _manager is None:
        _manager = ClobWsManager()
    return _manager


def start_clob_ws_manager() -> None:
    """Start the process-level manager's connection loop."""
    get_clob_ws_manager().start()


async def stop_clob_ws_manager() -> None:
    """Stop the process-level manager's connection loop."""
    if _manager is not None:
        await _manager.stop()
