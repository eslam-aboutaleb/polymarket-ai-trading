"""Binance WebSocket price feed for the latency-arbitrage engine (plan 06).

Subscribes to Binance's combined 1m kline stream for the BTC, ETH and
SOL USDT pairs and maintains, per symbol:

* the last price (close of the most recent kline update),
* the current (still-forming) 1m kline — open/high/low/close,
* a rolling history of closed 1m klines, used for realized volatility
  and for looking up the open/close price of an arbitrary window.

The feed is the fast leg of the latency-arbitrage premise: Binance
prices move seconds before Polymarket's 5m/15m/1h crypto odds adjust,
so the engine's edge only exists while this feed is fresher than
Polymarket's. Every kline update therefore records Binance's event
time (milliseconds) so the engine can measure the feed→order latency
distribution — the edge is only real when Polymarket's lag dominates.

Failure modes are deliberately non-fatal: connection drops reconnect
with exponential backoff (capped at 60s), and a stale or missing feed
is reported through the accessors as ``None`` rather than raising.

The scheduler lock ("latency_arb") is owned by
``latency_arb_service.start_latency_arb_engine``, which starts this
feed together with the market-discovery refresh and the engine loop;
``start_binance_feed``/``stop_binance_feed`` are plain task
lifecycle helpers and do not acquire locks themselves.
"""

import asyncio
import json
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import websockets

logger = logging.getLogger(__name__)

# Combined-streams endpoint: /stream?streams=a/b/c multiplexes several
# streams over one socket (the /ws endpoint accepts a single stream).
BINANCE_WS_URL = os.environ.get("BINANCE_WS_URL", "wss://stream.binance.com:9443/stream")

# Symbols tracked by the latency-arbitrage engine (USDT perpetuals).
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

KLINE_INTERVAL = "1m"

# Closed klines retained per symbol: 120 minutes covers the longest
# engine window (1h) plus margin for realized-volatility estimation.
KLINE_HISTORY_SIZE = 120

MAX_BACKOFF_SECONDS = 60.0


@dataclass
class Kline:
    """One closed or forming 1m kline."""

    start_ms: int
    end_ms: int
    open: float
    high: float
    low: float
    close: float
    event_time_ms: int
    is_closed: bool = False


@dataclass
class SymbolState:
    """Per-symbol feed state (single asyncio loop — no lock needed)."""

    symbol: str
    last_price: float | None = None
    current_kline: Kline | None = None
    closed_klines: deque[Kline] = field(default_factory=deque)
    last_event_time_ms: int = 0
    last_message_at: float = 0.0

    def record_kline(self, kline: Kline) -> None:
        """Apply a kline update, rotating closed klines into history."""
        self.last_price = kline.close
        self.last_event_time_ms = kline.event_time_ms
        self.last_message_at = datetime.now(UTC).timestamp()
        if kline.is_closed:
            self.closed_klines.append(kline)
            while len(self.closed_klines) > KLINE_HISTORY_SIZE:
                self.closed_klines.popleft()
            self.current_kline = None
        else:
            self.current_kline = kline


_state: dict[str, SymbolState] = {symbol: SymbolState(symbol=symbol) for symbol in SYMBOLS}

_feed_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_running = False


def _stream_name(symbol: str) -> str:
    return f"{symbol.lower()}@kline_{KLINE_INTERVAL}"


def combined_streams_url(symbols: tuple[str, ...] = SYMBOLS) -> str:
    """Combined-streams URL for the given symbols."""
    streams = "/".join(_stream_name(symbol) for symbol in symbols)
    return f"{BINANCE_WS_URL}?streams={streams}"


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def handle_message(raw: str | bytes) -> SymbolState | None:
    """Parse one websocket frame and update per-symbol state.

    Returns the updated ``SymbolState`` or ``None`` for non-kline
    or malformed frames (logged at debug, never raised).
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        logger.debug("Unparseable Binance frame")
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("e") != "kline":
        return None
    kline = data.get("k")
    if not isinstance(kline, dict):
        return None
    symbol = str(data.get("s") or kline.get("s") or "")
    state = _state.get(symbol)
    if state is None:
        return None
    state.record_kline(
        Kline(
            start_ms=_to_int(kline.get("t")),
            end_ms=_to_int(kline.get("T")),
            open=_to_float(kline.get("o")),
            high=_to_float(kline.get("h")),
            low=_to_float(kline.get("l")),
            close=_to_float(kline.get("c")),
            event_time_ms=_to_int(data.get("E")),
            is_closed=bool(kline.get("x")),
        )
    )
    return state


async def _ws_listen(stop_event: asyncio.Event) -> None:
    """Subscribe to the combined kline stream, reconnecting with backoff."""
    url = combined_streams_url()
    backoff = 1.0
    while not stop_event.is_set():
        try:
            async with websockets.connect(
                url,
                ping_interval=25,
                ping_timeout=20,
                close_timeout=5,
                additional_headers={"User-Agent": "polymarket-ai/1.0"},
            ) as ws:
                logger.info("Connected to Binance combined stream: %s", url)
                backoff = 1.0
                async for message in ws:
                    if stop_event.is_set():
                        break
                    handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Binance WS connection failed: %s", exc)
        if stop_event.is_set():
            break
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)


# ── Public accessors ──────────────────────────────────────────────


def get_symbol_state(symbol: str) -> dict[str, Any] | None:
    """Snapshot of one symbol's feed state (or ``None`` when unknown)."""
    state = _state.get(symbol.upper())
    if state is None:
        return None
    current = state.current_kline
    return {
        "symbol": state.symbol,
        "last_price": state.last_price,
        "current_kline": None
        if current is None
        else {
            "open": current.open,
            "high": current.high,
            "low": current.low,
            "close": current.close,
            "start_ms": current.start_ms,
            "end_ms": current.end_ms,
            "is_closed": current.is_closed,
        },
        "closed_klines": len(state.closed_klines),
        "last_event_time_ms": state.last_event_time_ms,
        "last_message_at": state.last_message_at,
    }


def get_last_price(symbol: str) -> float | None:
    """Last price for a symbol (``None`` until the first kline arrives)."""
    state = _state.get(symbol.upper())
    return state.last_price if state is not None else None


def get_current_kline(symbol: str) -> Kline | None:
    """The still-forming 1m kline for a symbol."""
    state = _state.get(symbol.upper())
    return state.current_kline if state is not None else None


def get_recent_closes(symbol: str, count: int = KLINE_HISTORY_SIZE) -> list[float]:
    """The most recent closed-kline closes, oldest first."""
    state = _state.get(symbol.upper())
    if state is None:
        return []
    klines = list(state.closed_klines)[-max(count, 0) :]
    return [kline.close for kline in klines]


def get_kline_at(symbol: str, start_ms: int) -> Kline | None:
    """The closed kline that started at ``start_ms`` (exact match)."""
    state = _state.get(symbol.upper())
    if state is None:
        return None
    for kline in reversed(state.closed_klines):
        if kline.start_ms == start_ms:
            return kline
        if kline.start_ms < start_ms:
            break
    return None


def get_window_open_price(symbol: str, window_start_epoch: int) -> float | None:
    """Price at the start of a window.

    Uses the open of the 1m kline that started exactly at the window
    boundary; when that kline has rotated out of history, falls back
    to the close of the latest kline that predates the window.
    """
    start_ms = int(window_start_epoch) * 1000
    exact = get_kline_at(symbol, start_ms)
    if exact is not None:
        return exact.open
    state = _state.get(symbol.upper())
    if state is None:
        return None
    fallback: Kline | None = None
    for kline in reversed(state.closed_klines):
        if kline.start_ms <= start_ms:
            fallback = kline
            break
    return fallback.close if fallback is not None else None


def get_window_close_price(symbol: str, window_end_epoch: int) -> float | None:
    """Price at the end of a window (close of the last kline in it)."""
    end_ms = int(window_end_epoch) * 1000
    state = _state.get(symbol.upper())
    if state is None:
        return None
    for kline in reversed(state.closed_klines):
        if kline.end_ms == end_ms:
            return kline.close
        if kline.end_ms < end_ms:
            break
    return None


def get_feed_lag_ms(symbol: str) -> float | None:
    """Milliseconds since Binance's last event time for a symbol."""
    state = _state.get(symbol.upper())
    if state is None or not state.last_event_time_ms:
        return None
    now_ms = datetime.now(UTC).timestamp() * 1000.0
    return max(0.0, now_ms - state.last_event_time_ms)


# ── Lifecycle ─────────────────────────────────────────────────────


async def start_binance_feed() -> None:
    """Start the Binance feed as a background task.

    The "latency_arb" scheduler lock is acquired by
    ``latency_arb_service.start_latency_arb_engine``, which owns the
    whole engine lifecycle; this helper only manages the task.
    """
    global _feed_task, _stop_event, _running
    if _running:
        return
    _stop_event = asyncio.Event()
    _running = True
    _feed_task = asyncio.create_task(_ws_listen(_stop_event))
    logger.info("Binance kline feed started")


async def stop_binance_feed() -> None:
    """Stop the Binance feed task."""
    global _feed_task, _stop_event, _running
    if not _running:
        return
    _running = False
    if _stop_event is not None:
        _stop_event.set()
    if _feed_task is not None and not _feed_task.done():
        _feed_task.cancel()
        try:
            await _feed_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Binance feed stop error: %s", exc)
    _feed_task = None
    _stop_event = None
    logger.info("Binance kline feed stopped")
