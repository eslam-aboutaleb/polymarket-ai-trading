"""Live Polymarket order-book feed (CLOB market websocket) for the latency engine.

Keeps a full local copy of the bid/ask ladder for every subscribed outcome
token, rebuilt from the exchange's own ``book`` snapshots and ``price_change``
deltas. ``quote()`` returns the best bid/ask the engine and the AI context
read, or ``None`` when the data is not fresh enough to trade on.

Why this exists: the Gamma ``outcomePrices`` the engine used before are
refreshed every 30 s, so the "market price" in the edge calculation could be
half a minute behind the real book. Here the price comes from the same
websocket the exchange publishes its book on.

Freshness rule (fails closed): a quote is served only when

* the websocket is connected,
* a ``book`` snapshot for that token has been received on this connection,
* some frame (data or heartbeat reply) arrived within ``max_age_s``, and
* the reconstructed book is not crossed (best bid < best ask).

There is no fallback to Gamma prices. If the feed is down the engine skips
the opportunity instead of trading on an old price.

Wire format (market channel): subscribe with
``{"assets_ids": [...], "type": "market"}`` the first time and
``{"assets_ids": [...], "operation": "subscribe"|"unsubscribe"}`` afterwards;
send a ``PING`` text frame every 10 s. Events: ``book`` (full snapshot, may
arrive inside a JSON array), ``price_change`` (``price_changes`` list; size
``0`` removes a level), ``last_trade_price``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

try:
    import websockets

    _WEBSOCKETS_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    websockets = None  # type: ignore[assignment]
    _WEBSOCKETS_AVAILABLE = False

DEFAULT_MAX_AGE_SECONDS = 3.0
DEPTH_LEVELS = 5
HEARTBEAT_INTERVAL_SECONDS = 10.0
INITIAL_RECONNECT_DELAY_SECONDS = 1.0
MAX_RECONNECT_DELAY_SECONDS = 30.0
MAX_TOKENS = 64


def _default_url() -> str:
    from app.services.market_data.clob_ws_manager import _configured_clob_ws_url

    return _configured_clob_ws_url()


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


@dataclass(frozen=True)
class BookQuote:
    """Top of a live book. Prices are probabilities in (0, 1)."""

    token_id: str
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    last_event_age_s: float
    feed_age_s: float

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "mid": self.mid,
            "spread": self.spread,
            "bid_size": self.bid_size,
            "ask_size": self.ask_size,
            "bids": [list(level) for level in self.bids],
            "asks": [list(level) for level in self.asks],
            "last_event_age_s": round(self.last_event_age_s, 3),
            "feed_age_s": round(self.feed_age_s, 3),
            "source": "clob_ws",
        }


@dataclass
class _TokenBook:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    has_snapshot: bool = False
    last_event_at: float = 0.0
    last_trade_price: float | None = None

    def best_bid(self) -> tuple[float, float] | None:
        if not self.bids:
            return None
        price = max(self.bids)
        return price, self.bids[price]

    def best_ask(self) -> tuple[float, float] | None:
        if not self.asks:
            return None
        price = min(self.asks)
        return price, self.asks[price]


class LiveBookFeed:
    """Websocket-fed order books keyed by outcome token id."""

    def __init__(
        self,
        url: str | None = None,
        max_age_s: float = DEFAULT_MAX_AGE_SECONDS,
        clock=time.time,
    ) -> None:
        self._url = url
        self.max_age_s = float(max_age_s)
        self._clock = clock
        self._books: dict[str, _TokenBook] = {}
        self._wanted: set[str] = set()
        self._sent: set[str] = set()
        self._initial_sent = False
        self._ws: Any = None
        self._connected = False
        self._last_frame_at = 0.0
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._wake: asyncio.Event | None = None
        # Integrity counters: exchange-stated best prices vs our reconstruction.
        self.checked_changes = 0
        self.best_mismatches = 0
        self.reconnects = 0
        self.messages = 0

    # ── message handling (pure, no I/O: tested with recorded wire frames) ──

    def handle_message(self, raw: str | bytes | list | dict, now: float | None = None) -> None:
        """Apply one websocket frame (text, or an already-decoded JSON value)."""
        now = self._clock() if now is None else now
        self._last_frame_at = now
        self.messages += 1
        if isinstance(raw, (str, bytes)):
            text = raw.decode() if isinstance(raw, bytes) else raw
            if text.strip().upper() == "PONG":
                return
            try:
                payload = json.loads(text)
            except ValueError:
                return
        else:
            payload = raw
        for event in payload if isinstance(payload, list) else [payload]:
            if isinstance(event, dict):
                self._apply_event(event, now)

    def _apply_event(self, event: dict[str, Any], now: float) -> None:
        kind = event.get("event_type")
        if kind == "book":
            self._apply_book(event, now)
        elif kind == "price_change" or "price_changes" in event:
            for change in event.get("price_changes") or []:
                if isinstance(change, dict):
                    self._apply_change(change, now)
        elif kind == "last_trade_price":
            token = str(event.get("asset_id") or "")
            book = self._books.get(token)
            price = _f(event.get("price"))
            if book is not None and price is not None:
                book.last_trade_price = price

    @staticmethod
    def _levels(raw: Any) -> dict[float, float]:
        out: dict[float, float] = {}
        for level in raw or []:
            if not isinstance(level, dict):
                continue
            price, size = _f(level.get("price")), _f(level.get("size"))
            if price is not None and size is not None and size > 0:
                out[price] = size
        return out

    def _apply_book(self, event: dict[str, Any], now: float) -> None:
        token = str(event.get("asset_id") or "")
        if not token or (self._wanted and token not in self._wanted):
            return
        book = self._books.setdefault(token, _TokenBook())
        book.bids = self._levels(event.get("bids"))
        book.asks = self._levels(event.get("asks"))
        book.has_snapshot = True
        book.last_event_at = now

    def _apply_change(self, change: dict[str, Any], now: float) -> None:
        token = str(change.get("asset_id") or "")
        book = self._books.get(token)
        if book is None or not book.has_snapshot:
            return  # a delta without its snapshot cannot be applied
        price, size = _f(change.get("price")), _f(change.get("size"))
        side = str(change.get("side") or "").upper()
        if price is None or size is None or side not in ("BUY", "SELL"):
            return
        ladder = book.bids if side == "BUY" else book.asks
        if size <= 0:
            ladder.pop(price, None)
        else:
            ladder[price] = size
        book.last_event_at = now
        stated_bid, stated_ask = _f(change.get("best_bid")), _f(change.get("best_ask"))
        if stated_bid is not None and stated_ask is not None:
            self.checked_changes += 1
            best_bid, best_ask = book.best_bid(), book.best_ask()
            if (
                best_bid is None
                or best_ask is None
                or abs(best_bid[0] - stated_bid) > 1e-9
                or abs(best_ask[0] - stated_ask) > 1e-9
            ):
                self.best_mismatches += 1

    # ── reads ──

    @property
    def connected(self) -> bool:
        return self._connected

    def feed_age_s(self, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        return float("inf") if not self._last_frame_at else max(0.0, now - self._last_frame_at)

    def quote(
        self,
        token_id: str,
        max_age_s: float | None = None,
        now: float | None = None,
        require_connected: bool = True,
    ) -> BookQuote | None:
        """Best bid/ask for ``token_id``, or ``None`` if not fresh and valid."""
        now = self._clock() if now is None else now
        limit = self.max_age_s if max_age_s is None else float(max_age_s)
        if require_connected and not self._connected:
            return None
        book = self._books.get(str(token_id))
        if book is None or not book.has_snapshot:
            return None
        feed_age = self.feed_age_s(now)
        if feed_age > limit:
            return None
        bid, ask = book.best_bid(), book.best_ask()
        if bid is None or ask is None or bid[0] >= ask[0]:
            return None
        bids = tuple(sorted(book.bids.items(), reverse=True)[:DEPTH_LEVELS])
        asks = tuple(sorted(book.asks.items())[:DEPTH_LEVELS])
        return BookQuote(
            token_id=str(token_id),
            best_bid=bid[0],
            best_ask=ask[0],
            bid_size=bid[1],
            ask_size=ask[1],
            bids=bids,
            asks=asks,
            last_event_age_s=max(0.0, now - book.last_event_at),
            feed_age_s=feed_age,
        )

    def stats(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "tokens_wanted": len(self._wanted),
            "tokens_with_book": sum(1 for b in self._books.values() if b.has_snapshot),
            "feed_age_s": None if not self._last_frame_at else round(self.feed_age_s(), 3),
            "messages": self.messages,
            "reconnects": self.reconnects,
            "checked_changes": self.checked_changes,
            "best_mismatches": self.best_mismatches,
        }

    # ── subscription set + connection lifecycle ──

    def set_tokens(self, token_ids: set[str] | list[str]) -> None:
        """Declare the tokens to follow; the connection loop syncs the diff."""
        wanted = {str(t) for t in token_ids if t}
        if len(wanted) > MAX_TOKENS:
            logger.warning(
                "Live book feed: %d tokens requested, capping at %d", len(wanted), MAX_TOKENS
            )
            wanted = set(sorted(wanted)[:MAX_TOKENS])
        self._wanted = wanted
        for token in list(self._books):
            if token not in wanted:
                del self._books[token]
        if self._wake is not None:
            self._wake.set()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not _WEBSOCKETS_AVAILABLE:
            logger.warning("Live book feed: websockets package missing; feed disabled")
            return
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        self._connected = False

    async def _run(self) -> None:
        delay = INITIAL_RECONNECT_DELAY_SECONDS
        if self._stop is None:
            return
        while not self._stop.is_set():
            try:
                await self._session()
                delay = INITIAL_RECONNECT_DELAY_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Live book feed disconnected: %s", exc)
            self._connected = False
            self.reconnects += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY_SECONDS)

    async def _send_sync(self, ws: Any) -> None:
        add = sorted(self._wanted - self._sent)
        drop = sorted(self._sent - self._wanted)
        if add:
            if self._initial_sent:
                payload = {"assets_ids": add, "operation": "subscribe"}
            else:
                payload = {"assets_ids": add, "type": "market"}
                self._initial_sent = True
            await ws.send(json.dumps(payload))
            self._sent.update(add)
        if drop:
            await ws.send(json.dumps({"assets_ids": drop, "operation": "unsubscribe"}))
            self._sent.difference_update(drop)

    async def _session(self) -> None:
        if self._stop is None:
            return
        self._books.clear()
        self._sent = set()
        self._initial_sent = False
        async with websockets.connect(
            self._url or _default_url(), open_timeout=10, max_size=None
        ) as ws:
            self._ws = ws
            self._connected = True
            last_ping = time.monotonic()
            while not self._stop.is_set():
                if self._wanted != self._sent:
                    await self._send_sync(ws)
                if time.monotonic() - last_ping >= HEARTBEAT_INTERVAL_SECONDS:
                    await ws.send("PING")
                    last_ping = time.monotonic()
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except TimeoutError:
                    continue
                self.handle_message(message)
        self._ws = None


# ── Process-level singleton ──

_feed: LiveBookFeed | None = None


def get_live_book_feed() -> LiveBookFeed:
    global _feed
    if _feed is None:
        settings = get_settings()
        _feed = LiveBookFeed(max_age_s=settings.latency_arb_book_max_age_ms / 1000.0)
    return _feed


def reset_live_book_feed() -> None:
    """Drop the singleton (tests)."""
    global _feed
    _feed = None
