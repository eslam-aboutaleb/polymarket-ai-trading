"""Services for Polymarket operations - portfolio, balance, and positions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import httpx
from web3 import Web3
from typing import Optional, Any
from decimal import Decimal
import logging
from datetime import datetime, timezone
from py_clob_client.clob_types import BookParams
from app.utils.cache import get_cache

logger = logging.getLogger(__name__)

# In-memory TTL cache for Gamma API market data (avoids hitting upstream on every page load)
_market_data_cache = get_cache("market_data")

# Polygon Mainnet RPC (free public endpoints)
POLYGON_RPC_URL = "https://polygon-bor-rpc.publicnode.com"

# USDC contract on Polygon (PoS bridged)
USDC_CONTRACT_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Minimal ERC-20 ABI for balanceOf
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]

# Polymarket APIs
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_API = "https://clob.polymarket.com"
POLYMARKET_DATA_API = "https://data-api.polymarket.com"


class PolymarketService:
    """Service for interacting with Polymarket and Polygon chain."""
    # Dedicated thread pool for blocking Web3 / CLOB calls so that the
    # asyncio event loop is never blocked.  A bounded pool prevents
    # unbounded thread creation under load.
    _executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="web3")

    _historical_winrate_cache: dict[str, dict[str, Any]] = {}
    _historical_winrate_cache_ttl_seconds = 60

    def __init__(self, rpc_url: str = POLYGON_RPC_URL):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.usdc_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_CONTRACT_ADDRESS),
            abi=ERC20_ABI,
        )

    @staticmethod
    async def _run_blocking(func, *args, **kwargs):
        """Run a blocking Web3 / CLOB call off the event loop using a
        dedicated thread-pool (avoids starving the default executor)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            PolymarketService._executor,
            lambda: func(*args, **kwargs),
        )

    # ------------------------------------------------------------------
    # Balance helpers
    # ------------------------------------------------------------------

    def _get_clob_client(
        self,
        private_key: str,
        clob_creds: dict = None,
        signature_type: int = 1,
        funder: str = None,
    ):
        """Create an authenticated ClobClient for Polymarket proxy-wallet queries.

        signature_type:
          0 = EOA  (plain externally-owned account)
          1 = POLY_PROXY  (Polymarket proxy wallet – most common)
          2 = POLY_GNOSIS_SAFE

        funder:
          The proxy wallet address.  Required for POLY_PROXY (sig_type=1) so
          that API credential derivation and any order signing use the correct
          maker address.  Pass None for EOA / gnosis safe.
        """
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        client = ClobClient(
            host=POLYMARKET_CLOB_API,
            chain_id=137,
            key=private_key,
            signature_type=signature_type,
            funder=funder,
        )

        if clob_creds:
            creds = ApiCreds(
                api_key=clob_creds["api_key"],
                api_secret=clob_creds["api_secret"],
                api_passphrase=clob_creds["api_passphrase"],
            )
            client.set_api_creds(creds)
        else:
            # Derive on the fly (slower – makes a network call)
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)

        return client

    async def get_wallet_balance(
        self,
        wallet_address: str,
        private_key: str = None,
        clob_creds: dict = None,
    ) -> dict:
        """
        Get wallet balance.
        If *private_key* is provided, query the real Polymarket proxy-wallet
        balance via the CLOB API (matches what the Polymarket UI shows).
        Otherwise fall back to on-chain EOA USDC balance.
        """
        usdc_balance = 0.0
        matic_balance = 0.0

        try:
            checksum_addr = Web3.to_checksum_address(wallet_address)

            # MATIC balance (always on-chain)
            matic_balance_wei = await self._run_blocking(self.w3.eth.get_balance, checksum_addr)
            matic_balance = float(Web3.from_wei(matic_balance_wei, "ether"))
        except Exception as e:
            logger.warning(f"Error fetching MATIC balance: {e}")

        # ----- USDC: prefer CLOB proxy-wallet balance -----
        if private_key:
            try:
                from py_clob_client.clob_types import (
                    BalanceAllowanceParams,
                    AssetType,
                )

                # Try POLY_PROXY (1) first, fall back to EOA (0) and
                # POLY_GNOSIS_SAFE (2) if the balance is 0.
                best_balance = "0"
                best_resp = None
                for st in [1, 0, 2]:
                    try:
                        c = await self._run_blocking(
                            self._get_clob_client,
                            private_key,
                            clob_creds,
                            signature_type=st,
                        )
                        r = await self._run_blocking(
                            c.get_balance_allowance,
                            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
                        )
                        bal = r.get("balance", "0")
                        if bal != "0":
                            best_balance = bal
                            best_resp = r
                            break
                        if best_resp is None:
                            best_resp = r
                    except Exception:
                        continue
                resp = best_resp or {"balance": "0"}
                # resp is a dict, e.g. {"balance": "...", "allowance": "..."}
                raw = best_balance if best_balance != "0" else resp.get("balance", "0")

                # The CLOB API may return micro-USDC (integer) or USDC with
                # decimals.  Detect by checking for a decimal point.
                if "." in str(raw):
                    usdc_balance = float(raw)
                else:
                    usdc_balance = float(Decimal(str(raw)) / Decimal(10**6))

                logger.info(
                    f"CLOB proxy-wallet USDC balance for {wallet_address}: {usdc_balance}"
                )
                return {
                    "wallet_address": wallet_address,
                    "matic_balance": round(matic_balance, 6),
                    "usdc_balance": round(usdc_balance, 2),
                    "chain": "polygon",
                }
            except Exception as e:
                logger.warning(
                    f"CLOB balance query failed, falling back to on-chain: {e}"
                )

        # Fallback: read bridged-USDC ERC-20 balance on the EOA
        try:
            checksum_addr = Web3.to_checksum_address(wallet_address)
            usdc_balance_raw = await self._run_blocking(
                self.usdc_contract.functions.balanceOf(checksum_addr).call
            )
            usdc_balance = float(Decimal(usdc_balance_raw) / Decimal(10**6))
        except Exception as e:
            logger.error(f"Error fetching on-chain USDC balance: {e}")

        return {
            "wallet_address": wallet_address,
            "matic_balance": round(matic_balance, 6),
            "usdc_balance": round(usdc_balance, 2),
            "chain": "polygon",
        }

    async def get_positions(
        self,
        wallet_address: str,
        private_key: str = None,
        clob_creds: dict = None,
    ) -> list:
        """
        Get Polymarket positions for a wallet.

        Strategy:
          1. Authenticated path (preferred): derive net positions from the
             complete trade history returned by the CLOB ``get_trades()``
             endpoint (Level-2 auth).  Current prices are fetched via CLOB
             ``get_last_trades_prices``.
          2. Public fallback: query the Polymarket Data API.
        """

        # ── 1. Authenticated: build positions from trade history ────
        if private_key:
            try:
                clob = await self._run_blocking(
                    self._get_clob_client,
                    private_key, clob_creds, signature_type=1
                )
                raw_trades = await self._run_blocking(clob.get_trades)
                if raw_trades:
                    return await self._run_blocking(
                        self._build_positions_from_trades,
                        clob,
                        raw_trades,
                    )
            except Exception as e:
                logger.warning("Authenticated position build failed: %s", e)

        # ── 2. Data API fallback (public, no auth needed) ──────────
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    "https://data-api.polymarket.com/positions",
                    params={"user": wallet_address.lower()},
                )
                if response.status_code == 200:
                    data = response.json()
                    items = data if isinstance(data, list) else data.get("positions", [])
                    if items:
                        return self._normalize_data_api_positions(items)
        except Exception as e:
            logger.debug("Data API positions fallback failed: %s", e)

        return []

    # ------------------------------------------------------------------
    # Position helpers
    # ------------------------------------------------------------------

    def _build_positions_from_trades(self, clob, raw_trades: list) -> list:
        """Aggregate trade history into net positions per (market, outcome).
        
        Only includes positions on markets that are still open (not resolved/closed).
        """
        from collections import defaultdict

        # Group trades by (condition_id, outcome)
        buckets: dict[tuple[str, str], dict] = defaultdict(
            lambda: {
                "buy_size": 0.0,
                "buy_cost": 0.0,
                "sell_size": 0.0,
                "sell_proceeds": 0.0,
                "asset_id": None,
            }
        )

        for t in raw_trades:
            cid = t.get("market", "")
            outcome = t.get("outcome") or "Unknown"
            side = (t.get("side") or "").upper()
            size = self._to_float(t.get("size"), 0.0)
            price = self._to_float(t.get("price"), 0.0)
            asset_id = t.get("asset_id")

            key = (cid, outcome)
            b = buckets[key]
            if asset_id:
                b["asset_id"] = asset_id
            if side == "BUY":
                b["buy_size"] += size
                b["buy_cost"] += size * price
            elif side == "SELL":
                b["sell_size"] += size
                b["sell_proceeds"] += size * price

        # Resolve market names + status (deduplicated)
        cids = list({k[0] for k in buckets})
        for cid in cids:
            if cid and cid not in self._market_cache:
                self._resolve_market_question(clob, cid)

        # Collect asset_ids for open markets that have a meaningful net position
        asset_ids = []
        for (cid, _outcome), b in buckets.items():
            net_size = b["buy_size"] - b["sell_size"]
            # Skip tiny dust positions
            if net_size < 0.01:
                continue
            # Skip closed/resolved markets
            minfo = self._market_cache.get(cid, {})
            if minfo.get("closed") or not minfo.get("accepting_orders", True):
                continue
            if b["asset_id"]:
                asset_ids.append(b["asset_id"])

        # Fetch current prices in bulk
        current_prices: dict[str, float] = {}
        if asset_ids:
            try:
                prices_resp = clob.get_last_trades_prices(
                    [BookParams(token_id=aid) for aid in asset_ids]
                )
                for entry in prices_resp:
                    token_id = entry.get("token_id") or entry.get("asset_id")
                    price = self._to_float(entry.get("price"), 0.0)
                    if token_id:
                        current_prices[token_id] = price
            except Exception as e:
                logger.debug("Bulk price lookup failed: %s", e)

        # Also try midpoints for any missing prices
        missing = [aid for aid in asset_ids if aid not in current_prices or current_prices[aid] == 0.0]
        if missing:
            try:
                mid_resp = clob.get_midpoints(
                    [BookParams(token_id=aid) for aid in missing]
                )
                for entry in mid_resp:
                    token_id = entry.get("token_id") or entry.get("asset_id")
                    mid = self._to_float(entry.get("mid"), 0.0)
                    if token_id and mid > 0:
                        current_prices[token_id] = mid
            except Exception as e:
                logger.debug("Midpoint lookup failed: %s", e)

        # Build positions list — only open markets
        positions: list[dict] = []
        for (cid, outcome), b in buckets.items():
            net_size = b["buy_size"] - b["sell_size"]
            # Filter out dust (< 0.01 shares)
            if net_size < 0.01:
                continue

            minfo = self._market_cache.get(cid, {})

            # Skip closed / resolved markets
            if minfo.get("closed") or not minfo.get("accepting_orders", True):
                continue

            avg_price = (
                (b["buy_cost"] / b["buy_size"]) if b["buy_size"] > 0 else 0.0
            )

            # Current price: prefer bulk lookup, else use order book midpoint
            cur_price = current_prices.get(b["asset_id"] or "", 0.0)
            if cur_price == 0.0:
                # Try individual price lookup as last resort
                try:
                    if b["asset_id"]:
                        p = clob.get_last_trade_price(b["asset_id"])
                        cur_price = self._to_float(p.get("price"), 0.0) if isinstance(p, dict) else 0.0
                except Exception:
                    pass
            if cur_price == 0.0:
                cur_price = avg_price

            invested = net_size * avg_price
            value = net_size * cur_price
            pnl = value - invested

            # Resolve token outcome name from market info when available
            token_outcome = outcome
            token_map = minfo.get("tokens")
            if token_map and b["asset_id"] and b["asset_id"] in token_map:
                token_outcome = token_map[b["asset_id"]]

            positions.append(
                {
                    "title": minfo.get("question") or cid,
                    "market": minfo.get("question") or cid,
                    "condition_id": cid,
                    "market_slug": minfo.get("market_slug"),
                    "outcome": token_outcome,
                    "size": round(net_size, 4),
                    "avgPrice": round(avg_price, 6),
                    "curPrice": round(cur_price, 6),
                    "pnl": round(pnl, 4),
                    "asset_id": b["asset_id"],
                }
            )

        # Sort by invested value descending
        positions.sort(
            key=lambda p: abs(p["size"] * p["avgPrice"]), reverse=True
        )
        return positions

    @staticmethod
    def _normalize_data_api_positions(items: list) -> list:
        """Normalize positions from the Polymarket Data API format."""
        positions: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                size = float(item.get("size") or item.get("amount") or 0)
            except (TypeError, ValueError):
                size = 0.0
            if size < 0.001:
                continue

            try:
                avg_price = float(item.get("avgPrice") or item.get("avg_price") or 0)
            except (TypeError, ValueError):
                avg_price = 0.0
            try:
                cur_price = float(
                    item.get("curPrice") or item.get("cur_price") or item.get("price") or avg_price
                )
            except (TypeError, ValueError):
                cur_price = avg_price

            invested = size * avg_price
            value = size * cur_price
            pnl = value - invested

            positions.append(
                {
                    "title": item.get("title") or item.get("question") or item.get("market") or "Unknown",
                    "market": item.get("market") or item.get("condition_id") or "",
                    "market_slug": item.get("market_slug") or item.get("marketSlug"),
                    "outcome": item.get("outcome") or item.get("outcome_name"),
                    "size": round(size, 4),
                    "avgPrice": round(avg_price, 6),
                    "curPrice": round(cur_price, 6),
                    "pnl": round(pnl, 4),
                    "asset_id": item.get("asset_id") or item.get("token_id"),
                }
            )
        return positions

    # ------------------------------------------------------------------
    # Trade history helpers
    # ------------------------------------------------------------------

    # In-memory cache for market question lookups (condition_id → question)
    _market_cache: dict[str, dict] = {}

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def _resolve_market_question(self, clob_client, condition_id: str) -> dict:
        """Resolve a condition_id to a human-readable market question + slug.
        Results are cached so repeated calls for the same market are free.
        """
        if condition_id in self._market_cache:
            return self._market_cache[condition_id]

        info: dict = {"question": condition_id, "market_slug": None}
        try:
            m = clob_client.get_market(condition_id)
            if isinstance(m, dict):
                info["question"] = m.get("question") or condition_id
                info["market_slug"] = m.get("market_slug")
                info["description"] = m.get("description")
                info["end_date_iso"] = m.get("end_date_iso")
                info["closed"] = m.get("closed", False)
                info["active"] = m.get("active", True)
                info["accepting_orders"] = m.get("accepting_orders", True)
                # Cache the token outcomes for this market
                tokens = m.get("tokens", [])
                if tokens:
                    info["tokens"] = {
                        t.get("token_id", ""): t.get("outcome", "")
                        for t in tokens
                        if isinstance(t, dict)
                    }
        except Exception as e:
            logger.debug("Could not resolve market %s: %s", condition_id[:16], e)

        self._market_cache[condition_id] = info
        return info

    def _normalize_clob_trade(self, item: dict, market_info: dict) -> dict:
        """Normalize a raw CLOB trade dict into our standard format."""
        size = self._to_float(item.get("size"), 0.0)
        price = self._to_float(item.get("price"), 0.0)
        fee_bps = self._to_float(item.get("fee_rate_bps"), 0.0)
        fee_amount = round(size * price * fee_bps / 10_000, 6)

        # match_time is a Unix epoch string
        match_time = item.get("match_time") or item.get("last_update")
        timestamp = None
        if match_time:
            try:
                from datetime import datetime, timezone
                timestamp = datetime.fromtimestamp(
                    int(match_time), tz=timezone.utc
                ).isoformat()
            except (ValueError, OSError):
                timestamp = match_time

        return {
            "id": item.get("id"),
            "market": market_info.get("question", item.get("market")),
            "market_slug": market_info.get("market_slug"),
            "condition_id": item.get("market"),
            "outcome": item.get("outcome"),
            "side": (item.get("side") or "").upper(),
            "size": size,
            "price": price,
            "type": item.get("trader_side") or "TRADE",
            "status": (item.get("status") or "CONFIRMED").upper(),
            "timestamp": timestamp,
            "fee_rate_bps": fee_bps,
            "fee": fee_amount,
            "transaction_hash": item.get("transaction_hash"),
            "maker_address": item.get("maker_address"),
            "trader_side": item.get("trader_side"),
            "pnl": None,  # CLOB trades don't include P&L directly
        }

    def _normalize_authenticated_trades(
        self,
        clob,
        raw_trades: list[dict[str, Any]],
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        """Normalize authenticated trade-history payload in a worker thread."""
        condition_ids = list(set(t.get("market", "") for t in raw_trades))
        for cid in condition_ids:
            if cid and cid not in self._market_cache:
                self._resolve_market_question(clob, cid)

        normalized: list[dict] = []
        for item in raw_trades:
            minfo = self._market_cache.get(item.get("market", ""), {})
            normalized.append(self._normalize_clob_trade(item, minfo))

        normalized.sort(
            key=lambda t: t.get("timestamp") or "", reverse=True
        )
        return normalized[offset: offset + limit]

    async def get_trade_history(
        self,
        wallet_address: str,
        private_key: str = None,
        clob_creds: dict = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        """
        Get wallet trade history from Polymarket.

        When *private_key* is provided, uses the authenticated ClobClient
        ``get_trades()`` method (Level-2 auth) which returns the real trade
        log.  Market condition-IDs are resolved to human-readable questions.

        Falls back to the unauthenticated public endpoints and finally to
        synthetic entries derived from positions.
        """
        # ── Authenticated path (preferred) ──────────────────────────
        if private_key:
            try:
                clob = await self._run_blocking(
                    self._get_clob_client,
                    private_key,
                    clob_creds,
                    signature_type=1,
                )
                raw_trades = await self._run_blocking(clob.get_trades)
                return await self._run_blocking(
                    self._normalize_authenticated_trades,
                    clob,
                    raw_trades,
                    limit,
                    offset,
                )
            except Exception as e:
                logger.warning("Authenticated trade fetch failed, falling back: %s", e)

        # ── Unauthenticated fallback ────────────────────────────────
        normalized_fallback: list[dict] = []
        seen: set[str] = set()
        user = wallet_address.lower()

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                endpoints = [
                    (
                        f"{POLYMARKET_CLOB_API}/data/trades",
                        {"user": user, "limit": limit, "offset": offset},
                    ),
                    (
                        f"{POLYMARKET_CLOB_API}/data/activity",
                        {"user": user, "limit": limit, "offset": offset},
                    ),
                ]

                for url, params in endpoints:
                    try:
                        response = await client.get(url, params=params)
                        if response.status_code != 200:
                            continue
                        payload = response.json()
                        items = (
                            payload
                            if isinstance(payload, list)
                            else payload.get("trades", payload.get("data", []))
                        )
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            tid = item.get("id", "")
                            if tid in seen:
                                continue
                            seen.add(tid)
                            normalized_fallback.append(
                                {
                                    "id": tid,
                                    "market": item.get("market")
                                    or item.get("title")
                                    or item.get("question"),
                                    "market_slug": item.get("marketSlug"),
                                    "outcome": item.get("outcome") or item.get("side"),
                                    "side": (item.get("side") or "").upper(),
                                    "size": self._to_float(item.get("size"), 0.0),
                                    "price": self._to_float(item.get("price"), 0.0),
                                    "type": item.get("type") or "TRADE",
                                    "status": (item.get("status") or "filled").upper(),
                                    "timestamp": item.get("timestamp")
                                    or item.get("createdAt")
                                    or item.get("matchTime"),
                                    "fee": self._to_float(
                                        item.get("fee") or item.get("feeAmount"), 0.0
                                    ),
                                    "pnl": self._to_float(item.get("pnl"), 0.0)
                                    if item.get("pnl") is not None
                                    else None,
                                }
                            )
                        if normalized_fallback:
                            break
                    except Exception as inner:
                        logger.debug("Trade endpoint %s failed: %s", url, inner)

            # Last resort — derive pseudo-trades from positions
            if not normalized_fallback:
                positions = await self.get_positions(wallet_address)
                for pos in positions:
                    normalized_fallback.append(
                        {
                            "id": pos.get("id"),
                            "market": pos.get("title")
                            or pos.get("market")
                            or pos.get("asset"),
                            "market_slug": pos.get("marketSlug"),
                            "outcome": pos.get("outcome"),
                            "side": "BUY",
                            "size": self._to_float(pos.get("size"), 0.0),
                            "price": self._to_float(pos.get("avgPrice"), 0.0),
                            "type": "POSITION",
                            "status": "OPEN",
                            "timestamp": pos.get("updatedAt") or pos.get("createdAt"),
                            "fee": 0.0,
                            "pnl": self._to_float(pos.get("pnl"), 0.0)
                            if pos.get("pnl") is not None
                            else None,
                        }
                    )

            normalized_fallback.sort(
                key=lambda t: t.get("timestamp") or "", reverse=True
            )
            return normalized_fallback[:limit]
        except Exception as e:
            logger.error("Error fetching trade history: %s", e)
            return []

    async def get_active_markets(self, limit: int = 60) -> list:
        """
        Get trending/active markets from Polymarket.
        Uses Gamma API /events to get live bestAsk/bestBid prices,
        normalises field names, and filters out resolved markets.

        Results are cached for 60 seconds to avoid hammering the upstream
        Gamma API on every page load / opportunity scan.
        """
        cache_key = f"active_markets:{limit}"
        cached = _market_data_cache.get(cache_key)
        if cached is not None:
            logger.debug("get_active_markets cache HIT (limit=%s)", limit)
            return cached

        try:
            # Request more events than needed so filtering still yields `limit`
            fetch_limit = max(limit * 3, 30)
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{POLYMARKET_GAMMA_API}/events",
                    params={
                        "limit": fetch_limit,
                        "active": True,
                        "closed": False,
                        "order": "volume24hr",
                        "ascending": False,
                    },
                )
                if response.status_code != 200:
                    logger.warning("Gamma /events returned %s", response.status_code)
                    return []

                events = response.json()
                markets: list[dict] = []
                for event in events if isinstance(events, list) else []:
                    event_markets = event.get("markets", [])
                    if not event_markets:
                        continue
                    for m in event_markets:
                        m["_event_title"] = event.get("title")
                        m["_event_slug"] = event.get("slug")
                        m["_event_image"] = event.get("image")
                        m["_event_volume"] = event.get("volume")
                        m["_event_liquidity"] = event.get("liquidity")
                        m["_event_volume_24hr"] = event.get("volume24hr")
                        m["_event_tags"] = event.get("tags", [])
                        # Preserve sub-market label (e.g. "Before July 2026")
                        if "groupItemTitle" not in m:
                            m["groupItemTitle"] = m.get("groupItemTitle", "")

                        # Normalise camelCase → snake_case
                        if "conditionId" in m and "condition_id" not in m:
                            m["condition_id"] = m["conditionId"]
                        if "endDateIso" in m and "end_date_iso" not in m:
                            m["end_date_iso"] = m["endDateIso"]

                        # ── Build accurate YES/NO prices ──
                        best_ask = m.get("bestAsk")
                        best_bid = m.get("bestBid")
                        last_trade = m.get("lastTradePrice")

                        yes_price = None
                        if best_ask is not None and float(best_ask) > 0:
                            yes_price = float(best_ask)
                        elif best_bid is not None and float(best_bid) > 0:
                            yes_price = float(best_bid)
                        elif last_trade is not None and 0 < float(last_trade) < 1:
                            yes_price = float(last_trade)

                        if yes_price is not None:
                            no_price = round(1.0 - yes_price, 4)
                            m["outcomePrices"] = [
                                str(round(yes_price, 4)),
                                str(round(no_price, 4)),
                            ]
                        else:
                            raw_prices = m.get("outcomePrices")
                            if isinstance(raw_prices, str):
                                try:
                                    import json as _json
                                    raw_prices = _json.loads(raw_prices)
                                except Exception:
                                    raw_prices = None
                            if isinstance(raw_prices, list) and len(raw_prices) >= 2:
                                try:
                                    m["outcomePrices"] = [
                                        str(float(raw_prices[0])),
                                        str(float(raw_prices[1])),
                                    ]
                                except (ValueError, TypeError):
                                    m["outcomePrices"] = ["0.5", "0.5"]
                            else:
                                m["outcomePrices"] = ["0.5", "0.5"]

                        # Expose raw book prices
                        m["bestAsk"] = best_ask
                        m["bestBid"] = best_bid
                        m["lastTradePrice"] = last_trade

                    # Filter out resolved markets
                    for m in event_markets:
                        prices = m.get("outcomePrices", [])
                        try:
                            p0 = float(prices[0])
                        except (IndexError, ValueError, TypeError):
                            p0 = 0.5
                        if p0 <= 0.005 or p0 >= 0.995:
                            continue
                        markets.append(m)

                result = markets[:limit]
                _market_data_cache.set(cache_key, result, ttl_seconds=60)
                logger.debug("get_active_markets cached %d markets (limit=%s)", len(result), limit)
                return result
        except Exception as e:
            logger.error(f"Error fetching active markets: {e}")
            return []

    # ---- noise-filter patterns for get_newest_markets ----
    _NOISE_TITLE_PATTERNS: list[str] = [
        "up or down",          # 5-min crypto price up/down markets
        "updown",              # slug variant
    ]
    _MIN_LIQUIDITY_FOR_NEWEST = 40  # skip ultra-thin markets

    def _is_noise_event(self, event: dict) -> bool:
        """Return True for spam/noise events that should be skipped."""
        title = (event.get("title") or "").lower()
        for pattern in self._NOISE_TITLE_PATTERNS:
            if pattern in title:
                return True
        liq = event.get("liquidity") or 0
        if liq < self._MIN_LIQUIDITY_FOR_NEWEST:
            return True
        return False

    async def get_newest_markets(self, limit: int = 60) -> list:
        """
        Get the newest/most recently created markets from Polymarket.
        Sorted by start date descending so the freshest markets come first.
        These are used by the Easy Trade finder to spot obvious mispricings
        in newly listed markets before the crowd catches on.

        Pre-filters noise (5-min crypto price prediction, ultra-low liquidity)
        so the resulting list contains diverse, interesting markets.
        Results are cached for 60 seconds.
        """
        cache_key = f"newest_markets:{limit}"
        cached = _market_data_cache.get(cache_key)
        if cached is not None:
            logger.debug("get_newest_markets cache HIT (limit=%s)", limit)
            return cached

        try:
            # Fetch extra events because ~80% are crypto noise that gets filtered
            fetch_limit = max(limit * 8, 300)
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{POLYMARKET_GAMMA_API}/events",
                    params={
                        "limit": fetch_limit,
                        "active": True,
                        "closed": False,
                        "order": "startDate",
                        "ascending": False,
                    },
                )
                if response.status_code != 200:
                    logger.warning("Gamma /events (newest) returned %s", response.status_code)
                    return []

                events = response.json()
                markets: list[dict] = []
                for event in events if isinstance(events, list) else []:
                    # Skip noise events (5-min crypto, ultra-thin liquidity)
                    if self._is_noise_event(event):
                        continue
                    event_markets = event.get("markets", [])
                    if not event_markets:
                        continue
                    for m in event_markets:
                        m["_event_title"] = event.get("title")
                        m["_event_slug"] = event.get("slug")
                        m["_event_image"] = event.get("image")
                        m["_event_volume"] = event.get("volume")
                        m["_event_liquidity"] = event.get("liquidity")
                        m["_event_volume_24hr"] = event.get("volume24hr")
                        m["_event_tags"] = event.get("tags", [])
                        m["_event_start_date"] = event.get("startDate")
                        if "groupItemTitle" not in m:
                            m["groupItemTitle"] = m.get("groupItemTitle", "")
                        if "conditionId" in m and "condition_id" not in m:
                            m["condition_id"] = m["conditionId"]
                        if "endDateIso" in m and "end_date_iso" not in m:
                            m["end_date_iso"] = m["endDateIso"]

                        # Build accurate YES/NO prices
                        best_ask = m.get("bestAsk")
                        best_bid = m.get("bestBid")
                        last_trade = m.get("lastTradePrice")

                        yes_price = None
                        if best_ask is not None and float(best_ask) > 0:
                            yes_price = float(best_ask)
                        elif best_bid is not None and float(best_bid) > 0:
                            yes_price = float(best_bid)
                        elif last_trade is not None and 0 < float(last_trade) < 1:
                            yes_price = float(last_trade)

                        if yes_price is not None:
                            no_price = round(1.0 - yes_price, 4)
                            m["outcomePrices"] = [
                                str(round(yes_price, 4)),
                                str(round(no_price, 4)),
                            ]
                        else:
                            raw_prices = m.get("outcomePrices")
                            if isinstance(raw_prices, str):
                                try:
                                    import json as _json
                                    raw_prices = _json.loads(raw_prices)
                                except Exception:
                                    raw_prices = None
                            if isinstance(raw_prices, list) and len(raw_prices) >= 2:
                                try:
                                    m["outcomePrices"] = [
                                        str(float(raw_prices[0])),
                                        str(float(raw_prices[1])),
                                    ]
                                except (ValueError, TypeError):
                                    m["outcomePrices"] = ["0.5", "0.5"]
                            else:
                                m["outcomePrices"] = ["0.5", "0.5"]

                        m["bestAsk"] = best_ask
                        m["bestBid"] = best_bid
                        m["lastTradePrice"] = last_trade

                    # Filter out resolved markets (p0 near 0 or 1)
                    for m in event_markets:
                        prices = m.get("outcomePrices", [])
                        try:
                            p0 = float(prices[0])
                        except (IndexError, ValueError, TypeError):
                            p0 = 0.5
                        if p0 <= 0.005 or p0 >= 0.995:
                            continue
                        markets.append(m)

                result = markets[:limit]
                _market_data_cache.set(cache_key, result, ttl_seconds=60)
                logger.debug("get_newest_markets cached %d markets (limit=%s)", len(result), limit)
                return result
        except Exception as e:
            logger.error(f"Error fetching newest markets: {e}")
            return []

    async def get_high_pnl_markets(self, limit: int = 80) -> list:
        """
        Fetch active markets and filter to those where BOTH yes AND no prices
        are < 0.90 (neither outcome near-certain → higher PNL potential).
        Returns markets sorted by PNL potential descending.
        Each market gets an added `pnl_potential` field (0-1).
        """
        raw = await self.get_active_markets(limit=limit * 2)
        filtered = []
        for m in raw:
            prices = m.get("outcomePrices", [])
            try:
                yes_p = float(prices[0]) if prices else 0.5
                no_p = float(prices[1]) if len(prices) > 1 else 1.0 - yes_p
            except (ValueError, TypeError):
                yes_p, no_p = 0.5, 0.5

            if yes_p < 0.90 and no_p < 0.90:
                # PNL potential = how far the cheaper side is from 0 (more upside)
                min_price = min(yes_p, no_p)
                m["pnl_potential"] = round(1.0 - min_price, 4)
                filtered.append(m)

        # Sort by PNL potential descending
        filtered.sort(key=lambda x: x.get("pnl_potential", 0), reverse=True)
        return filtered[:limit]

    async def get_smart_money_analysis(self, condition_id: str) -> dict:
        """
        Enhanced smart money analysis: fetches order book data, identifies
        whale-sized orders, and returns structured whale positioning data.
        """
        stats = await self.get_market_trader_stats(condition_id)
        top_traders = stats.get("top_traders", [])

        # Identify whales: orders > $500 total volume
        whales = [t for t in top_traders if t.get("total_volume", 0) > 500]
        whale_count = len(whales)

        yes_whale_vol = sum(t.get("yes_volume", 0) for t in whales)
        no_whale_vol = sum(t.get("no_volume", 0) for t in whales)
        total_whale_vol = yes_whale_vol + no_whale_vol

        if total_whale_vol > 0:
            yes_whale_pct = round(yes_whale_vol / total_whale_vol * 100, 1)
            no_whale_pct = round(no_whale_vol / total_whale_vol * 100, 1)
        else:
            yes_whale_pct = 50.0
            no_whale_pct = 50.0

        if yes_whale_pct > 60:
            whale_bias = "YES"
        elif no_whale_pct > 60:
            whale_bias = "NO"
        else:
            whale_bias = "MIXED"

        # Build smart money context for LLM prompt
        top_traders_text = ""
        for t in top_traders[:8]:
            top_traders_text += (
                f"  - {t.get('short_address', '?')} — "
                f"total ${t.get('total_volume', 0):.2f} "
                f"(YES ${t.get('yes_volume', 0):.2f} / NO ${t.get('no_volume', 0):.2f}) "
                f"lean: {t.get('lean', '?')}\n"
            )

        context_text = (
            f"Order book participants — YES side: {stats['yes_traders']} | NO side: {stats['no_traders']}\n"
            f"Volume split — YES: ${stats['yes_volume']:,.2f} ({stats['side_ratio']['yes']:.1f}%) | "
            f"NO: ${stats['no_volume']:,.2f} ({stats['side_ratio']['no']:.1f}%)\n"
            f"Whale orders (>${500}): {whale_count} detected, bias: {whale_bias}\n"
            f"  YES whale volume: ${yes_whale_vol:,.2f} ({yes_whale_pct}%)\n"
            f"  NO whale volume: ${no_whale_vol:,.2f} ({no_whale_pct}%)\n"
            f"Top traders by order size:\n{top_traders_text}"
        )

        return {
            "condition_id": condition_id,
            "whale_count": whale_count,
            "whale_bias": whale_bias,
            "total_whale_volume": round(total_whale_vol, 2),
            "yes_whale_pct": yes_whale_pct,
            "no_whale_pct": no_whale_pct,
            "yes_traders": stats["yes_traders"],
            "no_traders": stats["no_traders"],
            "yes_volume": stats["yes_volume"],
            "no_volume": stats["no_volume"],
            "side_ratio": stats["side_ratio"],
            "top_traders": top_traders[:6],
            "context_text": context_text,
        }

    # ------------------------------------------------------------------
    # Market categories & browsing
    # ------------------------------------------------------------------

    @staticmethod
    def _build_tokens_from_gamma(m: dict) -> None:
        """
        Populate a ``tokens`` list on a Gamma-sourced market dict so the
        frontend can send a valid ``token_id`` when placing trades.

        Gamma API returns ``clobTokenIds`` (JSON string of token IDs)
        and ``outcomes`` (JSON string like '["Yes","No"]').
        This method pairs them into the same ``tokens`` structure
        that the CLOB ``/markets/{cid}`` endpoint returns.
        """
        if "tokens" in m and isinstance(m.get("tokens"), list) and m["tokens"]:
            return  # already has tokens

        raw_tids = m.get("clobTokenIds") or m.get("clob_token_ids")
        raw_outcomes = m.get("outcomes")

        if not raw_tids:
            return

        import json as _json
        try:
            tids = _json.loads(raw_tids) if isinstance(raw_tids, str) else raw_tids
            outcomes = _json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
        except Exception:
            return

        if not isinstance(tids, list) or not tids:
            return
        if not isinstance(outcomes, list):
            outcomes = ["Yes", "No"]

        tokens = []
        for i, tid in enumerate(tids):
            outcome = outcomes[i] if i < len(outcomes) else ("Yes" if i == 0 else "No")
            tokens.append({"token_id": str(tid), "outcome": outcome})
        m["tokens"] = tokens

    MARKET_CATEGORIES = [
        {"id": "sports", "label": "Sports", "icon": "trophy", "description": "NFL, NBA, Soccer, Tennis & more"},
        {"id": "politics", "label": "Politics", "icon": "landmark", "description": "Elections, policy & geopolitics"},
        {"id": "crypto", "label": "Crypto", "icon": "bitcoin", "description": "BTC, ETH, Solana & token prices"},
        {"id": "pop-culture", "label": "Pop Culture", "icon": "star", "description": "Celebrities, music, movies & TV"},
        {"id": "business", "label": "Business", "icon": "briefcase", "description": "Earnings, IPOs & corporate moves"},
        {"id": "science", "label": "Science", "icon": "flask", "description": "Research, space & discoveries"},
        {"id": "technology", "label": "Technology", "icon": "cpu", "description": "AI, launches & tech industry"},
        {"id": "world", "label": "World", "icon": "globe", "description": "International events & conflicts"},
        {"id": "entertainment", "label": "Entertainment", "icon": "film", "description": "Award shows, streaming & media"},
    ]

    # Map our category IDs → sets of Gamma API tag slugs that qualify.
    # The Gamma API ignores the `tag` query-param, so we filter server-side
    # by checking each event's `tags[].slug` against these sets.
    TAG_SLUG_MAP: dict[str, set[str]] = {
        "sports":        {"sports", "nba", "nfl", "soccer", "hockey", "nhl", "basketball", "stanley-cup", "nba-champion", "nba-finals", "2026-fifa-world-cup", "fifa-world-cup", "world-cup"},
        "politics":      {"politics", "elections", "congress", "senate-primary", "house", "president", "primaries", "primary-elections", "us-presidential-election", "global-elections", "world-elections", "republican-primary", "us-government", "uptspt-politics", "texas-primary", "texas-senate", "senate-primary"},
        "crypto":        {"crypto", "crypto-prices", "airdrops", "fdv", "exchange", "megaeth"},
        "pop-culture":   {"pop-culture", "celebrities", "music", "taylor-swift", "creators", "awards", "gta-vi", "video-games"},
        "business":      {"business", "finance", "economy", "stocks", "ipos", "macro-geopolitics", "pre-market", "microstrategy", "trade-war", "taxes"},
        "science":       {"science"},
        "technology":    {"tech", "ai", "big-tech", "openai", "gpt-5", "sam-altman"},
        "world":         {"world", "world-affairs", "geopolitics", "foreign-policy", "ukraine", "ukraine-peace-deal", "ukraine-map", "russia", "russia-capture", "china", "india", "eu", "uk", "france", "middle-east", "iran", "israel", "nato", "military-action", "immigration", "immigrationborder", "syria", "poland", "us-iran", "trump-zelenskyy", "trump-putin", "zelensky", "putin", "security-guarantee"},
        "entertainment": {"entertainment", "movies", "music", "awards", "creators", "celebrities"},
    }

    @classmethod
    def _event_matches_tag(cls, event: dict, tag: str) -> bool:
        """Return True if *event* belongs to category *tag* (server-side check)."""
        allowed = cls.TAG_SLUG_MAP.get(tag)
        if not allowed:
            return False
        event_tags = event.get("tags") or []
        for t in event_tags:
            slug = (t.get("slug") or "").lower()
            if slug in allowed:
                return True
        return False

    def get_market_categories(self) -> list[dict]:
        """Return the hardcoded list of Polymarket event categories."""
        return self.MARKET_CATEGORIES

    async def get_markets_by_category(self, tag: str, limit: int = 20) -> list:
        """
        Fetch active markets for a specific category tag from the Gamma API.
        The Gamma ``/events`` endpoint does NOT reliably honour the ``tag``
        query-param, so we over-fetch and filter server-side.
        """
        try:
            # Over-fetch because server-side tag filtering will drop many events
            fetch_limit = max(limit * 5, 100)
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Primary: Gamma API events (over-fetch, filter locally)
                response = await client.get(
                    f"{POLYMARKET_GAMMA_API}/events",
                    params={
                        "limit": fetch_limit,
                        "active": True,
                        "closed": False,
                        "order": "volume24hr",
                        "ascending": False,
                    },
                )
                if response.status_code == 200:
                    events = response.json()
                    # Gamma events contain nested markets – flatten
                    markets: list[dict] = []
                    for event in events if isinstance(events, list) else []:
                        # ── Server-side tag filter ──
                        if not self._event_matches_tag(event, tag):
                            continue
                        event_markets = event.get("markets", [])
                        if event_markets:
                            for m in event_markets:
                                m["_event_title"] = event.get("title")
                                m["_event_slug"] = event.get("slug")
                                m["_event_image"] = event.get("image")
                                m["_event_volume"] = event.get("volume")
                                m["_event_liquidity"] = event.get("liquidity")
                                m["_event_volume_24hr"] = event.get("volume24hr")
                                if "groupItemTitle" not in m:
                                    m["groupItemTitle"] = m.get("groupItemTitle", "")

                                # Normalize field names: camelCase -> snake_case
                                if "conditionId" in m and "condition_id" not in m:
                                    m["condition_id"] = m["conditionId"]
                                if "endDateIso" in m and "end_date_iso" not in m:
                                    m["end_date_iso"] = m["endDateIso"]
                                if "clobTokenIds" in m and "clob_token_ids" not in m:
                                    m["clob_token_ids"] = m["clobTokenIds"]

                                # Build tokens array for trade execution
                                self._build_tokens_from_gamma(m)

                                # ── Build accurate YES/NO prices ──
                                # Polymarket shows YES price = bestAsk (cost to buy YES share)
                                # and NO price = 1 - YES price.
                                # bestBid / bestAsk are the live order-book prices.
                                # outcomePrices from Gamma can be stale or represent
                                # resolved markets ("0"/"1").
                                best_ask = m.get("bestAsk")
                                best_bid = m.get("bestBid")
                                last_trade = m.get("lastTradePrice")

                                # Prefer bestAsk > bestBid > lastTradePrice > outcomePrices
                                yes_price = None
                                if best_ask is not None and float(best_ask) > 0:
                                    yes_price = float(best_ask)
                                elif best_bid is not None and float(best_bid) > 0:
                                    yes_price = float(best_bid)
                                elif last_trade is not None and 0 < float(last_trade) < 1:
                                    yes_price = float(last_trade)

                                if yes_price is not None:
                                    no_price = round(1.0 - yes_price, 4)
                                    m["outcomePrices"] = [
                                        str(round(yes_price, 4)),
                                        str(round(no_price, 4)),
                                    ]
                                else:
                                    # Fall back to Gamma outcomePrices
                                    raw_prices = m.get("outcomePrices")
                                    if isinstance(raw_prices, str):
                                        try:
                                            import json as _json
                                            raw_prices = _json.loads(raw_prices)
                                        except Exception:
                                            raw_prices = None
                                    if isinstance(raw_prices, list) and len(raw_prices) >= 2:
                                        try:
                                            m["outcomePrices"] = [
                                                str(float(raw_prices[0])),
                                                str(float(raw_prices[1])),
                                            ]
                                        except (ValueError, TypeError):
                                            m["outcomePrices"] = ["0.5", "0.5"]
                                    else:
                                        m["outcomePrices"] = ["0.5", "0.5"]

                                # Also expose raw book prices so the UI can decide
                                m["bestAsk"] = best_ask
                                m["bestBid"] = best_bid
                                m["lastTradePrice"] = last_trade

                            # Filter out effectively-resolved markets
                            # (outcomePrices exactly ["0.0","1.0"] or ["1.0","0.0"])
                            alive = []
                            for m in event_markets:
                                prices = m.get("outcomePrices", [])
                                try:
                                    p0, p1 = float(prices[0]), float(prices[1])
                                except (IndexError, ValueError, TypeError):
                                    p0, p1 = 0.5, 0.5
                                # Skip markets whose yes price is 0 or 1 (resolved)
                                if p0 <= 0.005 or p0 >= 0.995:
                                    continue
                                alive.append(m)
                            markets.extend(alive)
                        else:
                            # Treat the event itself as a single market
                            markets.append(event)
                    return markets[:limit]

                # Fallback: CLOB markets with tag (CLOB includes outcomePrices)
                response = await client.get(
                    f"{POLYMARKET_CLOB_API}/markets",
                    params={
                        "tag": tag,
                        "limit": limit,
                        "active": True,
                        "closed": False,
                        "order": "volume24hr",
                        "ascending": False,
                    },
                )
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, list):
                        return data
                    return data.get("data", data.get("markets", []))
                return []
        except Exception as e:
            logger.error(f"Error fetching markets for tag={tag}: {e}")
            return []

    # Shared httpx client for connection reuse across requests
    _shared_http_client: httpx.AsyncClient | None = None

    @classmethod
    def _get_http_client(cls) -> httpx.AsyncClient:
        """Return a shared httpx.AsyncClient for connection pooling."""
        if cls._shared_http_client is None or cls._shared_http_client.is_closed:
            cls._shared_http_client = httpx.AsyncClient(
                timeout=15.0,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return cls._shared_http_client

    async def search_all_markets(
        self,
        query: str = "",
        tag: str = "",
        limit: int = 60,
        offset: int = 0,
        sort: str = "volume24hr",
    ) -> dict:
        """
        Browse / search ALL active Polymarket markets.

        Uses the Gamma API ``/events`` endpoint with optional text search.
        Results are cached for 30 seconds to avoid hammering the upstream
        Gamma API on every page load.
        Returns ``{markets: [...], total: int, offset: int, has_more: bool}``.
        """
        try:
            # Cache the upstream Gamma fetch by (sort, tag) so that pagination
            # and text search can be served from the cached full list.
            gamma_cache_key = f"search_all_markets:{sort}:{tag}"
            all_markets: list[dict] | None = _market_data_cache.get(gamma_cache_key)

            if all_markets is None:
                client = self._get_http_client()
                all_markets: list[dict] = []
                seen_market_keys: set[str] = set()

                # Walk Gamma events in pages so we can mirror the broader Polymarket list.
                page_size = 200
                max_pages = 40
                max_markets = 6000
                page_offset = 0
                previous_page_fingerprint = ""
                reached_cap = False

                def _as_prob(value: Any) -> float | None:
                    try:
                        n = float(value)
                        return n if 0 < n < 1 else None
                    except (TypeError, ValueError):
                        return None

                for _ in range(max_pages):
                    params: dict[str, Any] = {
                        "limit": page_size,
                        "offset": page_offset,
                        "active": True,
                        "closed": False,
                        "order": sort if sort else "volume24hr",
                        "ascending": False,
                    }
                    response = await client.get(
                        f"{POLYMARKET_GAMMA_API}/events",
                        params=params,
                    )
                    if response.status_code != 200:
                        if page_offset == 0:
                            logger.warning(
                                "Gamma /events search returned %s", response.status_code
                            )
                            return {
                                "markets": [],
                                "total": 0,
                                "offset": offset,
                                "has_more": False,
                            }
                        logger.warning(
                            "Gamma /events pagination stopped at offset=%s status=%s",
                            page_offset,
                            response.status_code,
                        )
                        break

                    events = response.json()
                    if not isinstance(events, list) or not events:
                        break

                    first_event = events[0]
                    page_fingerprint = (
                        f"{first_event.get('id') or first_event.get('slug') or ''}:{len(events)}"
                    )
                    # Safety: if upstream ignores offset, avoid looping same page forever.
                    if page_fingerprint and page_fingerprint == previous_page_fingerprint:
                        logger.warning(
                            "Gamma /events returned duplicate page fingerprint; stopping pagination"
                        )
                        break
                    previous_page_fingerprint = page_fingerprint
                    page_offset += len(events)

                    for event in events:
                        # ── Server-side tag filter ──
                        if tag and not self._event_matches_tag(event, tag):
                            continue
                        event_markets = event.get("markets", [])
                        if not event_markets:
                            continue

                        for m in event_markets:
                            m["_event_title"] = event.get("title")
                            m["_event_slug"] = event.get("slug")
                            m["_event_image"] = event.get("image")
                            m["_event_volume"] = event.get("volume")
                            m["_event_liquidity"] = event.get("liquidity")
                            m["_event_volume_24hr"] = event.get("volume24hr")
                            m["_event_tags"] = event.get("tags", [])
                            if "groupItemTitle" not in m:
                                m["groupItemTitle"] = m.get("groupItemTitle", "")

                            if "conditionId" in m and "condition_id" not in m:
                                m["condition_id"] = m["conditionId"]
                            if "endDateIso" in m and "end_date_iso" not in m:
                                m["end_date_iso"] = m["endDateIso"]

                            # Build tokens array for trade execution
                            self._build_tokens_from_gamma(m)

                            # Build YES / NO prices
                            best_ask = m.get("bestAsk")
                            best_bid = m.get("bestBid")
                            last_trade = m.get("lastTradePrice")
                            yes_price = (
                                _as_prob(best_ask)
                                or _as_prob(best_bid)
                                or _as_prob(last_trade)
                            )

                            if yes_price is not None:
                                no_price = round(1.0 - yes_price, 4)
                                m["outcomePrices"] = [
                                    str(round(yes_price, 4)),
                                    str(round(no_price, 4)),
                                ]
                            else:
                                raw_prices = m.get("outcomePrices")
                                if isinstance(raw_prices, str):
                                    try:
                                        import json as _json
                                        raw_prices = _json.loads(raw_prices)
                                    except Exception:
                                        raw_prices = None
                                if isinstance(raw_prices, list) and len(raw_prices) >= 2:
                                    try:
                                        m["outcomePrices"] = [
                                            str(float(raw_prices[0])),
                                            str(float(raw_prices[1])),
                                        ]
                                    except (ValueError, TypeError):
                                        m["outcomePrices"] = ["0.5", "0.5"]
                                else:
                                    m["outcomePrices"] = ["0.5", "0.5"]

                            m["bestAsk"] = best_ask
                            m["bestBid"] = best_bid
                            m["lastTradePrice"] = last_trade

                        # Filter resolved + dedupe
                        for m in event_markets:
                            prices = m.get("outcomePrices", [])
                            try:
                                p0 = float(prices[0])
                            except (IndexError, ValueError, TypeError):
                                p0 = 0.5
                            if p0 <= 0.005 or p0 >= 0.995:
                                continue

                            cid = str(
                                m.get("condition_id")
                                or m.get("conditionId")
                                or m.get("id")
                                or ""
                            )
                            dedupe_key = cid or str(
                                (m.get("_event_slug") or m.get("slug") or "")
                                + "|"
                                + (m.get("question") or "")
                            )
                            if dedupe_key in seen_market_keys:
                                continue
                            seen_market_keys.add(dedupe_key)
                            all_markets.append(m)

                            if len(all_markets) >= max_markets:
                                reached_cap = True
                                break
                        if reached_cap:
                            break
                    if reached_cap or len(events) < page_size:
                        break

                # Cache the full market list for 30s (shared across paginated requests)
                _market_data_cache.set(gamma_cache_key, all_markets, ttl_seconds=30)
                logger.debug(
                    "search_all_markets cached %d markets (sort=%s, tag=%s)",
                    len(all_markets), sort, tag,
                )

            # Client-side text search (operates on cached list)
            filtered = all_markets
            if query:
                q_lower = query.lower()
                filtered = [
                    m
                    for m in all_markets
                    if q_lower
                    in (
                        (m.get("question") or m.get("_event_title") or "")
                        .lower()
                    )
                ]

            total = len(filtered)
            page = filtered[offset : offset + limit]
            return {
                "markets": page,
                "total": total,
                "offset": offset,
                "has_more": (offset + limit) < total,
            }
        except Exception as e:
            logger.error("Error in search_all_markets: %s", e)
            return {"markets": [], "total": 0, "offset": offset, "has_more": False}

    async def get_market_trader_stats(self, condition_id: str) -> dict:
        """
        Fetch trader positioning statistics for a market using public APIs.

        Combines data from:
          - CLOB /markets/{condition_id}  (public) → token IDs & live prices
          - CLOB /book?token_id=…         (public) → order-book depth
          - Gamma /markets?conditionId=…  (public) → total traded volume

        Returns dict with:
          - yes_traders / no_traders  (order-book participant levels per side)
          - yes_volume  / no_volume   (estimated dollar volume per side)
          - total_trades
          - top_traders  (up to 10 largest resting orders with size & price)
          - side_ratio   {yes: %, no: %}
          - recent_trades (snapshot of best bid/ask levels)
        """
        stats: dict = {
            "condition_id": condition_id,
            "yes_traders": 0,
            "no_traders": 0,
            "yes_volume": 0.0,
            "no_volume": 0.0,
            "total_trades": 0,
            "top_traders": [],
            "side_ratio": {"yes": 50.0, "no": 50.0},
            "recent_trades": [],
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                # ── 1. Get token IDs & live prices from CLOB market endpoint ──
                market_resp = await client.get(
                    f"{POLYMARKET_CLOB_API}/markets/{condition_id}"
                )
                if market_resp.status_code != 200:
                    logger.warning(
                        "CLOB /markets/%s returned %s",
                        condition_id[:16], market_resp.status_code,
                    )
                    return stats

                market_data = market_resp.json()
                tokens = market_data.get("tokens", [])
                yes_token_id = None
                no_token_id = None
                yes_price = 0.5
                no_price = 0.5
                for t in tokens:
                    outcome = (t.get("outcome") or "").lower()
                    if outcome == "yes":
                        yes_token_id = t["token_id"]
                        yes_price = float(t.get("price", 0.5))
                    elif outcome == "no":
                        no_token_id = t["token_id"]
                        no_price = float(t.get("price", 0.5))

                # ── 2. Get total volume from Gamma API ────────────────────
                total_volume = 0.0
                try:
                    gamma_resp = await client.get(
                        f"{POLYMARKET_GAMMA_API}/markets",
                        params={"conditionId": condition_id, "limit": 1},
                    )
                    if gamma_resp.status_code == 200:
                        gamma_markets = gamma_resp.json()
                        if gamma_markets:
                            total_volume = float(
                                gamma_markets[0].get("volumeNum", 0)
                            )
                except Exception as ge:
                    logger.debug("Gamma volume lookup failed: %s", ge)

                # ── 3. Fetch order book for YES token ─────────────────────
                bids: list = []
                asks: list = []
                if yes_token_id:
                    try:
                        book_resp = await client.get(
                            f"{POLYMARKET_CLOB_API}/book",
                            params={"token_id": yes_token_id},
                        )
                        if book_resp.status_code == 200:
                            book = book_resp.json()
                            bids = book.get("bids", [])
                            asks = book.get("asks", [])
                    except Exception as be:
                        logger.debug("Book fetch failed: %s", be)

                # ── 4. Compute stats ──────────────────────────────────────
                # Bid side of YES token → participants wanting YES
                # Ask side of YES token → participants wanting NO (selling YES)
                yes_bid_levels = len(bids)
                no_ask_levels = len(asks)

                yes_bid_depth = sum(
                    float(b.get("size", 0)) * float(b.get("price", 0))
                    for b in bids
                )
                no_ask_depth = sum(
                    float(a.get("size", 0)) * float(a.get("price", 0))
                    for a in asks
                )

                stats["yes_traders"] = yes_bid_levels
                stats["no_traders"] = no_ask_levels

                # Split total volume proportional to outcome prices
                price_sum = yes_price + no_price
                if total_volume > 0 and price_sum > 0:
                    stats["yes_volume"] = round(
                        total_volume * (yes_price / price_sum), 2
                    )
                    stats["no_volume"] = round(
                        total_volume * (no_price / price_sum), 2
                    )
                elif total_volume > 0:
                    stats["yes_volume"] = round(total_volume / 2, 2)
                    stats["no_volume"] = round(total_volume / 2, 2)
                else:
                    # Fall back to order-book depth as volume proxy
                    stats["yes_volume"] = round(yes_bid_depth, 2)
                    stats["no_volume"] = round(no_ask_depth, 2)

                stats["total_trades"] = yes_bid_levels + no_ask_levels

                total_vol = stats["yes_volume"] + stats["no_volume"]
                if total_vol > 0:
                    stats["side_ratio"] = {
                        "yes": round(stats["yes_volume"] / total_vol * 100, 1),
                        "no": round(stats["no_volume"] / total_vol * 100, 1),
                    }

                # ── 5. Top participants from largest resting orders ───────
                all_orders: list[dict] = []
                for b in bids:
                    sz = float(b.get("size", 0))
                    px = float(b.get("price", 0))
                    all_orders.append({"size": sz, "price": px, "side": "YES"})
                for a in asks:
                    sz = float(a.get("size", 0))
                    px = float(a.get("price", 0))
                    all_orders.append({"size": sz, "price": px, "side": "NO"})

                all_orders.sort(key=lambda x: x["size"], reverse=True)

                top: list[dict] = []
                for i, order in enumerate(all_orders[:10]):
                    vol = round(order["size"] * order["price"], 2)
                    top.append({
                        "address": f"Order-{i+1}",
                        "short_address": f"@{order['price']:.3f}",
                        "yes_volume": vol if order["side"] == "YES" else 0,
                        "no_volume": vol if order["side"] == "NO" else 0,
                        "total_volume": vol,
                        "lean": order["side"],
                    })
                stats["top_traders"] = top

                # ── 6. Recent activity snapshot from book ─────────────────
                recent: list[dict] = []
                for b in bids[:10]:
                    recent.append({
                        "side": "BUY",
                        "outcome": "Yes",
                        "size": float(b.get("size", 0)),
                        "price": float(b.get("price", 0)),
                        "trader": f"bid@{b.get('price', '?')}",
                    })
                for a in asks[:10]:
                    recent.append({
                        "side": "SELL",
                        "outcome": "Yes",
                        "size": float(a.get("size", 0)),
                        "price": float(a.get("price", 0)),
                        "trader": f"ask@{a.get('price', '?')}",
                    })
                stats["recent_trades"] = recent[:20]

        except Exception as e:
            logger.error(f"Error fetching trader stats for {condition_id[:16]}: {e}")

        return stats

    async def get_market_prices(self, condition_id: str) -> dict:
        """
        Fetch current bid/ask prices for a market from the CLOB API.
        Returns { "yes_price": float, "no_price": float } representing
        the current mid-price or best-ask for each outcome.
        """
        prices = {"yes_price": 0.5, "no_price": 0.5}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Fetch order book for this market
                response = await client.get(
                    f"{POLYMARKET_CLOB_API}/order_book",
                    params={"market_id": condition_id},
                )
                if response.status_code == 200:
                    data = response.json()
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    
                    # Process YES and NO prices from bids/asks
                    for side_data in bids + asks:
                        outcome = (side_data.get("outcome") or "").lower()
                        price = float(side_data.get("price", 0))
                        if outcome == "yes":
                            prices["yes_price"] = price
                        elif outcome == "no":
                            prices["no_price"] = price
        except Exception as e:
            logger.warning(f"Error fetching prices for {condition_id[:16]}: {e}")
        
        return prices

    async def _get_historical_win_stats(self, wallet_address: str) -> dict[str, Any]:
        """
        Compute historical closed-position win stats from Polymarket activity.

        Closed-position win rules (aligned with leaderboard service):
          - REDEEM => closed win
          - MERGE (with buy exposure) => closed non-win
          - BUY+SELL roundtrip => closed; win if sell_revenue > buy_cost

        Win-rate denominator:
          total historical positions (opened markets), not only closed ones.
          win_rate = winning_closed_positions / total_positions_history * 100
        """
        wallet = (wallet_address or "").lower()
        now_ts = datetime.now(timezone.utc).timestamp()
        cached = self._historical_winrate_cache.get(wallet)
        if cached and (now_ts - float(cached.get("ts", 0))) < self._historical_winrate_cache_ttl_seconds:
            return cached.get("data", {})

        result = {
            "wins_positions_history": 0,
            "total_positions_history": 0,
            "win_rate": 0.0,
            "history_fetch_ok": False,
        }

        if not wallet:
            return result

        market_trades: dict[str, list[dict[str, Any]]] = {}
        had_success = False
        page_limit = 500
        max_pages = 20

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                for page in range(max_pages):
                    offset = page * page_limit
                    resp = await client.get(
                        f"{POLYMARKET_DATA_API}/activity",
                        params={"user": wallet, "limit": page_limit, "offset": offset},
                    )
                    if resp.status_code != 200:
                        logger.warning(
                            "Historical win-rate fetch failed for %s (status=%s, page=%s)",
                            wallet[:10],
                            resp.status_code,
                            page,
                        )
                        break
                    payload = resp.json()
                    if not isinstance(payload, list) or not payload:
                        break

                    had_success = True
                    for row in payload:
                        if not isinstance(row, dict):
                            continue
                        market_id = str(
                            row.get("conditionId")
                            or row.get("market")
                            or row.get("condition_id")
                            or ""
                        )
                        if not market_id:
                            continue
                        market_trades.setdefault(market_id, []).append(row)

                    if len(payload) < page_limit:
                        break
        except Exception as e:
            logger.warning("Historical win-rate activity fetch failed for %s: %s", wallet[:10], e)

        if had_success:
            wins_closed = 0
            total_closed = 0
            total_positions_history = 0

            for acts in market_trades.values():
                buy_cost = 0.0
                sell_revenue = 0.0
                has_redemption = False
                has_merge = False

                for act in acts:
                    act_type = str(act.get("type", "")).upper()
                    side = str(act.get("side", "")).upper()
                    amount = self._to_float(
                        act.get("usdcSize", act.get("value", act.get("amount", 0))),
                        0.0,
                    )

                    if act_type == "TRADE":
                        if side == "BUY":
                            buy_cost += amount
                        elif side == "SELL":
                            sell_revenue += amount
                    elif act_type == "REDEEM":
                        has_redemption = True
                    elif act_type == "MERGE":
                        has_merge = True

                # A market counts as a historical position if the user opened/held
                # exposure there at any point.
                is_historical_position = buy_cost > 0 or has_redemption or has_merge
                if is_historical_position:
                    total_positions_history += 1

                if has_redemption:
                    total_closed += 1
                    wins_closed += 1
                elif has_merge and buy_cost > 0:
                    total_closed += 1
                elif buy_cost > 0 and sell_revenue > 0:
                    total_closed += 1
                    if sell_revenue > buy_cost:
                        wins_closed += 1

            result["wins_positions_history"] = wins_closed
            result["total_positions_history"] = total_positions_history
            result["win_rate"] = (
                round((wins_closed / total_positions_history * 100), 1)
                if total_positions_history > 0
                else 0.0
            )
            result["history_fetch_ok"] = True

        self._historical_winrate_cache[wallet] = {
            "data": result,
            "ts": now_ts,
        }
        return result

    async def get_portfolio_summary(
        self,
        wallet_address: str,
        private_key: str = None,
        clob_creds: dict = None,
    ) -> dict:
        """
        Get complete portfolio summary: balance + positions + P&L.
        """
        balance = await self.get_wallet_balance(
            wallet_address, private_key=private_key, clob_creds=clob_creds
        )
        positions = await self.get_positions(
            wallet_address, private_key=private_key, clob_creds=clob_creds
        )

        # Calculate portfolio metrics
        total_invested = 0.0
        total_current_value = 0.0
        active_positions = 0

        for pos in positions:
            try:
                size = float(pos.get("size", 0))
                avg_price = float(pos.get("avgPrice", 0))
                current_price = float(pos.get("curPrice", pos.get("price", 0)))

                invested = size * avg_price
                current_value = size * current_price

                total_invested += invested
                total_current_value += current_value
                if size > 0:
                    active_positions += 1
            except (ValueError, TypeError):
                continue

        total_pnl = total_current_value - total_invested
        pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0.0

        total_positions = len(positions)
        current_wins = 0
        for pos in positions:
            try:
                if float(pos.get("pnl", 0)) > 0:
                    current_wins += 1
            except (TypeError, ValueError):
                continue

        history = await self._get_historical_win_stats(wallet_address)
        wins_positions_history = int(history.get("wins_positions_history", 0) or 0)
        total_positions_history = int(history.get("total_positions_history", 0) or 0)
        history_win_rate = float(history.get("win_rate", 0.0) or 0.0)
        history_fetch_ok = bool(history.get("history_fetch_ok"))

        # Fallback when historical feed is unavailable: keep dashboard usable
        # instead of showing empty win-rate values.
        if not history_fetch_ok and total_positions > 0:
            wins_positions_history = current_wins
            total_positions_history = total_positions
            history_win_rate = round((current_wins / total_positions * 100), 1)

        return {
            "wallet_address": wallet_address,
            "usdc_balance": balance["usdc_balance"],
            "matic_balance": balance["matic_balance"],
            "active_positions": active_positions,
            "total_positions": total_positions,
            "total_invested": round(total_invested, 2),
            "total_current_value": round(total_current_value, 2),
            "total_pnl": round(total_pnl, 2),
            "pnl_percentage": round(pnl_pct, 2),
            "win_rate": round(history_win_rate, 1),
            "wins_positions_history": wins_positions_history,
            "total_positions_history": total_positions_history,
            # kept for backward compatibility with existing frontend usages
            "wins_positions": wins_positions_history,
            "resolved_trades": total_positions_history,
            "positions": positions[:20],  # Return latest 20 positions
        }


# Singleton instance
_polymarket_service: Optional[PolymarketService] = None


def get_polymarket_service() -> PolymarketService:
    """Get or create the Polymarket service singleton."""
    global _polymarket_service
    if _polymarket_service is None:
        _polymarket_service = PolymarketService()
    return _polymarket_service
