"""On-chain whale monitoring via CTF Exchange / Conditional Tokens events.

Subscribes to Polygon logs for the Conditional Tokens contract
(ERC-1155 position transfers plus the split/merge/redemption
lifecycle events) and the CTF Exchange, decodes them, filters for
whale-sized activity, and persists ``WhaleEvent`` rows seconds
after the transaction confirms — well ahead of the public
positions API poll.

Whale set = leaderboard top wallets (global feed) ∪ the user's
followed traders ∪ the user's configured watchlist. Events that
match a followed trader or watchlist entry also dispatch an alert
(plan 09) and, when enabled, route into the copy-trade path.

Failure modes are deliberately non-fatal: undecodable logs are
logged and skipped, WS RPC failures rotate to the next public
endpoint with backoff, and the positions-API poll remains the
slower fallback so no data is lost, only delayed.
"""

import asyncio
import contextlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session
from web3 import Web3
from web3.datastructures import AttributeDict

from app.models.followed_trader import FollowedTrader
from app.models.whale_config import WhaleConfig
from app.models.whale_event import WhaleEvent
from app.utils.cache import get_cache
from app.utils.database import SessionLocal
from app.utils.scheduler_lock import (
    acquire_scheduler_lock,
    release_scheduler_lock,
    scheduler_heartbeat,
)
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

# ── Contracts (Polygon mainnet) ──────────────────────────────
CTF_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
CONDITIONAL_TOKENS_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476042"

# Public Polygon WS RPCs — rotated on failure (no dedicated node provider).
WS_RPC_URLS = [
    "wss://polygon-bor-rpc.publicnode.com",
    "wss://polygon.llamarpc.com",
    "wss://rpc.ankr.com/polygon",
]
# HTTPS RPC for block timestamps (best-effort, non-blocking).
HTTPS_RPC_URL = os.environ.get("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")

# ── Tunables ─────────────────────────────────────────────────
WHALE_MIN_NOTIONAL = float(os.environ.get("WHALE_MIN_NOTIONAL", "10000"))
WHALE_AUTO_COPY = os.environ.get("WHALE_AUTO_COPY", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
WHALE_SET_SIZE = int(os.environ.get("WHALE_SET_SIZE", "50"))
REFRESH_INTERVAL_SECONDS = int(os.environ.get("WHALE_REFRESH_INTERVAL", "300"))
DEDUP_TTL_SECONDS = 3600
BLOCK_TS_TTL_SECONDS = 3600
WS_CONNECT_TIMEOUT = 10.0
MAX_BACKOFF_SECONDS = 60.0

ZERO_ADDRESS = "0x" + "0" * 40

_cache = get_cache("whales")

# ── ABI loading and topic map ────────────────────────────────
_ABI_DIR = Path(__file__).resolve().parent.parent / "abi"


def _load_abi(filename: str) -> list[dict[str, Any]]:
    return json.loads((_ABI_DIR / filename).read_text(encoding="utf-8"))


CTF_ABI = _load_abi("conditional_tokens.json")
EXCHANGE_ABI = _load_abi("ctf_exchange.json")

_w3 = Web3()
_ctf_contract = _w3.eth.contract(
    address=Web3.to_checksum_address(CONDITIONAL_TOKENS_ADDRESS), abi=CTF_ABI
)
_exchange_contract = _w3.eth.contract(
    address=Web3.to_checksum_address(CTF_EXCHANGE_ADDRESS), abi=EXCHANGE_ABI
)


def _event_topic(abi_entry: dict[str, Any]) -> str:
    """keccak256 of the canonical event signature."""
    params = ",".join(inp.get("type", "") for inp in abi_entry.get("inputs", []))
    signature = f"{abi_entry['name']}({params})"
    return Web3.keccak(text=signature).hex()


# topic0 → (event name, contract). OrderFilled is pinned for future
# use but excluded: every fill settles as an ERC-1155 transfer, so
# decoding both would double-count the same economic event.
_ACTIVE_EVENTS = {
    "TransferSingle",
    "TransferBatch",
    "PositionSplit",
    "PositionMerge",
    "Redemption",
}

# Event name → stored event_type (snake_case).
_EVENT_TYPE_MAP = {
    "TransferSingle": "transfer",
    "TransferBatch": "transfer",
    "PositionSplit": "position_split",
    "PositionMerge": "position_merge",
    "Redemption": "redemption",
}

_TOPIC_MAP: dict[str, tuple[str, Any]] = {}
for _abi, _contract in ((CTF_ABI, _ctf_contract), (EXCHANGE_ABI, _exchange_contract)):
    for _entry in _abi:
        if _entry.get("type") != "event":
            continue
        _name = _entry["name"]
        if _name not in _ACTIVE_EVENTS:
            continue
        _TOPIC_MAP[_event_topic(_entry)] = (_name, _contract)

# ── Runtime state ────────────────────────────────────────────
_whale_wallets: set[str] = set()
_followed_map: dict[str, set[int]] = {}
_watchlist_map: dict[str, set[int]] = {}
_token_market_map: dict[str, dict[str, Any]] = {}
_condition_market_map: dict[str, dict[str, Any]] = {}
_config_cache: dict[int, WhaleConfig] = {}

_monitor_task: asyncio.Task | None = None
_refresh_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_running = False


# ── Decoding ─────────────────────────────────────────────────
def _to_hex_string(value: Any) -> str:
    """Normalize a decoded bytes/bytes32 value to a 0x-hex string."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    return str(value)


def _hex_to_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value, 16) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None


def decode_log(log: dict[str, Any]) -> dict[str, Any] | None:
    """Decode a raw Polygon log into a normalized event dict.

    Returns None for unknown topics or undecodable payloads
    (contract upgrades, ABI drift) — those are logged, not raised.
    """
    topics = log.get("topics") or []
    if not topics:
        return None
    entry = _TOPIC_MAP.get(topics[0])
    if entry is None:
        logger.debug("Skipping log with unknown topic %s", topics[0])
        return None
    event_name, contract = entry
    try:
        event = getattr(contract.events, event_name)()
        decoded = event.process_log(AttributeDict(log))
        args = dict(decoded.args)
    except Exception as exc:
        logger.warning(
            "Undecodable %s log (tx=%s): %s",
            event_name,
            log.get("transactionHash"),
            exc,
        )
        return None
    return {
        "event": event_name,
        "args": args,
        "tx_hash": log.get("transactionHash") or "",
        "log_index": _hex_to_int(log.get("logIndex")),
        "block_number": _hex_to_int(log.get("blockNumber")),
    }


def _whale_candidates(decoded: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand a decoded log into per-wallet observation candidates."""
    event_name = decoded["event"]
    args = decoded["args"]
    candidates: list[dict[str, Any]] = []

    if event_name in ("TransferSingle", "TransferBatch"):
        from_wallet = str(args.get("from") or "").lower()
        to_wallet = str(args.get("to") or "").lower()
        if event_name == "TransferSingle":
            transfers = [(str(args.get("id")), float(args.get("value") or 0))]
        else:
            transfers = [
                (str(token_id), float(value))
                for token_id, value in zip(
                    args.get("ids") or [],
                    args.get("values") or [],
                    strict=False,
                )
            ]
        for token_id, size in transfers:
            if to_wallet and to_wallet != ZERO_ADDRESS:
                side = "open" if from_wallet == ZERO_ADDRESS else "buy"
                candidates.append(
                    {
                        "wallet": to_wallet,
                        "token_id": token_id,
                        "size": size,
                        "side": side,
                        "event_type": "transfer",
                    }
                )
            if from_wallet and from_wallet != ZERO_ADDRESS:
                side = "close" if to_wallet == ZERO_ADDRESS else "sell"
                candidates.append(
                    {
                        "wallet": from_wallet,
                        "token_id": token_id,
                        "size": size,
                        "side": side,
                        "event_type": "transfer",
                    }
                )

    elif event_name in ("PositionSplit", "PositionMerge"):
        owner = str(args.get("stakeOwner") or "").lower()
        condition_id = _to_hex_string(args.get("conditionId")).lower()
        # Collateral is USDC (6 decimals) — the amount is already USD.
        amount = float(args.get("amount") or 0) / 1e6
        if owner and condition_id:
            candidates.append(
                {
                    "wallet": owner,
                    "token_id": f"cond:{condition_id}",
                    "size": amount,
                    "side": "open" if event_name == "PositionSplit" else "close",
                    "event_type": _EVENT_TYPE_MAP[event_name],
                }
            )

    elif event_name == "Redemption":
        owner = str(args.get("stakeOwner") or "").lower()
        condition_id = _to_hex_string(args.get("conditionId")).lower()
        payout = float(args.get("payout") or 0) / 1e6
        if owner and condition_id:
            candidates.append(
                {
                    "wallet": owner,
                    "token_id": f"cond:{condition_id}",
                    "size": payout,
                    "side": "redeem",
                    "event_type": _EVENT_TYPE_MAP[event_name],
                }
            )

    return candidates


# ── Market / whale-set resolution ────────────────────────────
def _resolve_market(token_id: str) -> dict[str, Any] | None:
    """Map a token_id (or cond:<conditionId>) to market info."""
    if token_id.startswith("cond:"):
        return _condition_market_map.get(token_id[5:].lower())
    return _token_market_map.get(token_id)


async def _refresh_market_maps() -> None:
    """Rebuild token_id → market and condition_id → market maps."""
    global _token_market_map, _condition_market_map
    try:
        # get_active_markets/get_newest_markets are methods on the
        # PolymarketService singleton, not module-level functions.
        from app.services.polymarket_service import get_polymarket_service

        service = get_polymarket_service()

        markets: list[dict[str, Any]] = []
        markets.extend(await service.get_active_markets(limit=500))
        markets.extend(await service.get_newest_markets(limit=500))
    except Exception as exc:
        logger.warning("Market map refresh failed: %s", exc)
        return

    token_map: dict[str, dict[str, Any]] = {}
    condition_map: dict[str, dict[str, Any]] = {}
    for market in markets:
        market_id = str(market.get("id") or market.get("condition_id") or "")
        question = str(
            market.get("question")
            or market.get("groupItemTitle")
            or market.get("_event_title")
            or ""
        )
        prices = market.get("outcomePrices") or []
        try:
            price = float(prices[0]) if prices else 0.5
        except (IndexError, ValueError, TypeError):
            price = 0.5
        info = {
            "market_id": market_id,
            "question": question,
            "price": price,
        }
        condition_id = str(market.get("condition_id") or market.get("conditionId") or "").lower()
        if condition_id:
            condition_map[condition_id] = info
        for token in market.get("tokens") or []:
            token_id = str(token.get("token_id") or token.get("tokenId") or "")
            if token_id:
                token_map[token_id] = info

    _token_market_map = token_map
    _condition_market_map = condition_map
    logger.info(
        "Market maps refreshed: %d tokens, %d conditions",
        len(token_map),
        len(condition_map),
    )


async def _refresh_whale_set() -> None:
    """Refresh the global whale set from the leaderboard top wallets."""
    global _whale_wallets
    try:
        from app.services.leaderboard_service import fetch_leaderboard

        entries = await fetch_leaderboard(limit=WHALE_SET_SIZE, period="all_time")
        _whale_wallets = {
            str(entry.get("address") or "").lower() for entry in entries if entry.get("address")
        }
        logger.info("Whale set refreshed: %d wallets", len(_whale_wallets))
    except Exception as exc:
        logger.warning("Whale set refresh failed: %s", exc)


def _refresh_followed_map() -> None:
    """Refresh wallet → user_ids maps for followed traders and watchlists."""
    global _followed_map, _watchlist_map
    db = SessionLocal()
    try:
        followed: dict[str, set[int]] = {}
        rows = (
            db.query(FollowedTrader.user_id, FollowedTrader.trader_wallet)
            .filter(FollowedTrader.is_active.is_(True))
            .all()
        )
        for user_id, wallet in rows:
            followed.setdefault(str(wallet).lower(), set()).add(user_id)
        _followed_map = followed

        watchlist: dict[str, set[int]] = {}
        for config in db.query(WhaleConfig.user_id, WhaleConfig.watchlist).all():
            for wallet in config.watchlist or []:
                watchlist.setdefault(str(wallet).lower(), set()).add(config.user_id)
        _watchlist_map = watchlist
    except Exception as exc:
        logger.warning("Followed-map refresh failed: %s", exc)
    finally:
        db.close()


def get_user_config(user_id: int, db: Session) -> WhaleConfig:
    """Return the user's whale config (cached; defaults when unset)."""
    config = _config_cache.get(user_id)
    if config is not None:
        return config
    config = db.query(WhaleConfig).filter(WhaleConfig.user_id == user_id).first()
    if config is None:
        # Column defaults only apply at flush time, so a
        # transient row must be constructed with explicit values.
        config = WhaleConfig(
            user_id=user_id,
            min_notional=WHALE_MIN_NOTIONAL,
            auto_copy=WHALE_AUTO_COPY,
            watchlist=[],
            whale_set_size=WHALE_SET_SIZE,
        )
    _config_cache[user_id] = config
    return config


def invalidate_config_cache(user_id: int | None = None) -> None:
    """Drop cached configs so route updates take effect immediately."""
    if user_id is None:
        _config_cache.clear()
    else:
        _config_cache.pop(user_id, None)


# ── Event handling ───────────────────────────────────────────
def _already_seen(tx_hash: str, log_index: int | None, wallet: str, token_id: str) -> bool:
    key = f"seen:{tx_hash}:{log_index}:{wallet}:{token_id}"
    if _cache.get(key) is not None:
        return True
    _cache.set(key, "1", ttl_seconds=DEDUP_TTL_SECONDS)
    return False


async def _update_block_ts(event_id: int, block_number: int) -> None:
    """Best-effort block timestamp backfill (non-blocking)."""
    cache_key = f"block_ts:{block_number}"
    try:
        cached = _cache.get(cache_key)
        if cached:
            block_ts = datetime.fromisoformat(str(cached))
        else:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    HTTPS_RPC_URL,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_getBlockByNumber",
                        "params": [hex(block_number), False],
                    },
                )
            if response.status_code != 200:
                return
            block = (response.json() or {}).get("result") or {}
            timestamp_hex = block.get("timestamp")
            if not timestamp_hex:
                return
            block_ts = datetime.fromtimestamp(int(timestamp_hex, 16), tz=UTC)
            _cache.set(cache_key, block_ts.isoformat(), ttl_seconds=BLOCK_TS_TTL_SECONDS)
    except Exception as exc:
        logger.debug("Block timestamp fetch failed for %s: %s", block_number, exc)
        return

    db = SessionLocal()
    try:
        event = db.get(WhaleEvent, event_id)
        if event is not None and not event.block_ts:
            event.block_ts = block_ts
            db.commit()
    except Exception as exc:
        logger.debug("Block timestamp backfill failed for event %s: %s", event_id, exc)
        db.rollback()
    finally:
        db.close()


def _dispatch_alert(user_id: int, payload: dict[str, Any]) -> None:
    """Dispatch a whale-activity alert via the plan 09 alert service."""
    try:
        from app.services.alert_service import dispatch
    except ImportError:
        logger.debug("alert_service unavailable — whale alert skipped")
        return
    try:
        result = dispatch("whale_alert", user_id, payload, background=True)
        if asyncio.iscoroutine(result):
            asyncio.get_event_loop().create_task(result)
    except Exception as exc:
        logger.debug("Whale alert dispatch failed for user %s: %s", user_id, exc)


async def _run_auto_copy(
    user_id: int,
    wallet: str,
    market_info: dict[str, Any],
    candidate: dict[str, Any],
    price: float,
    size: float,
    tx_hash: str,
) -> None:
    """Execute a whale-triggered copy trade (owns its DB session)."""
    try:
        from app.services.copy_trade_service import execute_copy_trade
    except ImportError:
        logger.debug("copy_trade_service unavailable — whale auto-copy skipped")
        return
    db = SessionLocal()
    try:
        await execute_copy_trade(
            db=db,
            user_id=user_id,
            trader_wallet=wallet,
            market_id=market_info["market_id"],
            token_id=candidate["token_id"],
            side="buy" if candidate["side"] in ("buy", "open") else "sell",
            price=price,
            trader_amount=size,
            idempotency_key=f"whale:{tx_hash}:{wallet}:{candidate['token_id']}",
        )
    except Exception as exc:
        logger.warning("Whale auto-copy failed user=%s wallet=%s: %s", user_id, wallet, exc)
    finally:
        db.close()


def _maybe_auto_copy(
    user_id: int,
    wallet: str,
    market_info: dict[str, Any],
    candidate: dict[str, Any],
    price: float,
    size: float,
    tx_hash: str,
) -> None:
    """Route a whale event into the copy-trade path when enabled."""
    if not WHALE_AUTO_COPY:
        return
    config = _config_cache.get(user_id)
    if config is None or not config.auto_copy:
        return
    # Only transfer events carry a concrete outcome token_id; condition-
    # referenced events (split/merge/redemption) cannot be copied yet.
    if candidate["token_id"].startswith("cond:"):
        return
    try:
        asyncio.get_event_loop().create_task(
            _run_auto_copy(user_id, wallet, market_info, candidate, price, size, tx_hash)
        )
    except Exception as exc:
        logger.warning("Whale auto-copy scheduling failed: %s", exc)


async def _handle_decoded(decoded: dict[str, Any]) -> None:
    """Filter, persist and dispatch a decoded on-chain event."""
    candidates = _whale_candidates(decoded)
    if not candidates:
        return

    tx_hash = decoded.get("tx_hash") or ""
    log_index = decoded.get("log_index")
    block_number = decoded.get("block_number")

    db = SessionLocal()
    try:
        for candidate in candidates:
            wallet = candidate["wallet"]
            interested_users: set[int] = set(_followed_map.get(wallet, set()))
            interested_users |= set(_watchlist_map.get(wallet, set()))
            if not interested_users and wallet not in _whale_wallets:
                continue

            market_info = _resolve_market(candidate["token_id"])
            if market_info is None:
                logger.debug(
                    "Unmapped token %s — skipping whale event",
                    candidate["token_id"][:24],
                )
                continue

            price = market_info["price"]
            size = candidate["size"]
            # Condition-referenced sizes are already USD (collateral);
            # transfer sizes are share counts priced at the market price.
            notional = size if candidate["token_id"].startswith("cond:") else size * price

            min_notional = WHALE_MIN_NOTIONAL
            for user_id in interested_users:
                min_notional = min(min_notional, get_user_config(user_id, db).min_notional)
            if notional < min_notional:
                continue

            if _already_seen(tx_hash, log_index, wallet, candidate["token_id"]):
                continue

            event = WhaleEvent(
                wallet=wallet,
                market_id=market_info["market_id"],
                token_id=candidate["token_id"],
                event_type=candidate["event_type"],
                side=candidate["side"],
                size=size,
                price=price,
                notional=notional,
                tx_hash=tx_hash,
                log_index=log_index,
                block_number=block_number,
                detected_at=utc_now(),
            )
            db.add(event)
            db.commit()
            db.refresh(event)

            if block_number:
                asyncio.get_event_loop().create_task(_update_block_ts(event.id, block_number))

            payload = {
                "wallet": wallet,
                "market_id": market_info["market_id"],
                "question": market_info["question"],
                "token_id": candidate["token_id"],
                "event_type": candidate["event_type"],
                "side": candidate["side"],
                "size": size,
                "price": price,
                "notional": notional,
                "tx_hash": tx_hash,
            }
            for user_id in interested_users:
                _dispatch_alert(user_id, payload)
                _maybe_auto_copy(user_id, wallet, market_info, candidate, price, size, tx_hash)
    except Exception as exc:
        logger.warning("Whale event handling failed: %s", exc)
        db.rollback()
    finally:
        db.close()


# ── WebSocket subscription ───────────────────────────────────
async def _handle_ws_message(message: str | bytes) -> None:
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return
    if payload.get("method") != "eth_subscription":
        return
    log = (payload.get("params") or {}).get("result")
    if not isinstance(log, dict):
        return
    decoded = decode_log(log)
    if decoded is None:
        return
    await _handle_decoded(decoded)


async def _ws_listen(stop_event: asyncio.Event) -> None:
    """Subscribe to CTF logs, rotating across public WS RPCs on failure."""
    import websockets
    import websockets.exceptions

    url_index = 0
    backoff = 1.0
    while not stop_event.is_set():
        url = WS_RPC_URLS[url_index % len(WS_RPC_URLS)]
        try:
            async with websockets.connect(
                url,
                ping_interval=25,
                ping_timeout=20,
                close_timeout=5,
                additional_headers={"User-Agent": "polymarket-ai/1.0"},
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "eth_subscribe",
                            "params": [
                                "logs",
                                {
                                    "address": [
                                        CTF_EXCHANGE_ADDRESS,
                                        CONDITIONAL_TOKENS_ADDRESS,
                                    ]
                                },
                            ],
                        }
                    )
                )
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=WS_CONNECT_TIMEOUT))
                if "error" in reply:
                    raise RuntimeError(f"eth_subscribe error: {reply['error']}")
                logger.info(
                    "Subscribed to CTF logs via %s (sub=%s)",
                    url,
                    reply.get("result"),
                )
                backoff = 1.0
                async for message in ws:
                    if stop_event.is_set():
                        break
                    await _handle_ws_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("CTF WS connection to %s failed: %s", url, exc)
        url_index += 1
        if stop_event.is_set():
            break
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)


async def _periodic_refresh(stop_event: asyncio.Event) -> None:
    """Refresh whale set, followed map and market maps on a timer."""
    while not stop_event.is_set():
        _refresh_followed_map()
        await _refresh_whale_set()
        await _refresh_market_maps()
        scheduler_heartbeat("ctf_events")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=REFRESH_INTERVAL_SECONDS)


# ── Lifecycle ────────────────────────────────────────────────
async def start_ctf_events_monitor() -> None:
    """Start the whale monitor as a background task (scheduler-locked)."""
    global _monitor_task, _refresh_task, _stop_event, _running
    if _running:
        return
    if not acquire_scheduler_lock("ctf_events"):
        logger.info("CTF events monitor: scheduler lock held elsewhere; not starting")
        return
    _stop_event = asyncio.Event()
    _running = True
    _monitor_task = asyncio.create_task(_ws_listen(_stop_event))
    _refresh_task = asyncio.create_task(_periodic_refresh(_stop_event))
    logger.info("CTF events whale monitor started")


async def stop_ctf_events_monitor() -> None:
    """Stop the whale monitor and release the scheduler lock."""
    global _monitor_task, _refresh_task, _stop_event, _running
    if not _running:
        return
    _running = False
    if _stop_event is not None:
        _stop_event.set()
    for task in (_monitor_task, _refresh_task):
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("CTF monitor task stop error: %s", exc)
    _monitor_task = None
    _refresh_task = None
    _stop_event = None
    release_scheduler_lock("ctf_events")
    logger.info("CTF events whale monitor stopped")
