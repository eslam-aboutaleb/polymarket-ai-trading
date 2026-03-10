"""Portfolio API routes - balance, positions, and portfolio summary."""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

from app.api.routes.auth import get_current_user_from_token, get_optional_user_from_token
from app.services.polymarket_service import get_polymarket_service
from app.security.credential_store import CredentialStoreError, load_wallet_credentials
from py_clob_client.clob_types import BookParams
from app.utils.time import utc_now

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


# --- Response Schemas ---

class WalletBalance(BaseModel):
    """Wallet balance on Polygon."""
    wallet_address: str
    usdc_balance: float = 0.0
    matic_balance: float = 0.0
    chain: str = "polygon"
    error: Optional[str] = None


class Position(BaseModel):
    """A single Polymarket position."""
    market: Optional[str] = None
    title: Optional[str] = None
    outcome: Optional[str] = None
    size: float = 0.0
    avg_price: float = 0.0
    current_price: float = 0.0
    pnl: float = 0.0
    market_slug: Optional[str] = None


class PortfolioSummary(BaseModel):
    """Complete portfolio summary."""
    wallet_address: str
    usdc_balance: float = 0.0
    matic_balance: float = 0.0
    active_positions: int = 0
    total_positions: int = 0
    total_invested: float = 0.0
    total_current_value: float = 0.0
    total_pnl: float = 0.0
    pnl_percentage: float = 0.0
    win_rate: float = 0.0
    wins_positions_history: int = 0
    total_positions_history: int = 0
    wins_positions: int = 0
    resolved_trades: int = 0
    positions: list = []


class MarketSummary(BaseModel):
    """Active market summary."""
    id: Optional[str] = None
    question: Optional[str] = None
    volume_24hr: Optional[float] = None
    liquidity: Optional[float] = None
    end_date: Optional[str] = None
    outcomes: Optional[list] = None


# --- Endpoints ---

@router.get("/balance", response_model=WalletBalance)
async def get_balance(
    current_user: dict = Depends(get_current_user_from_token),
):
    """Get USDC and MATIC balance for the authenticated user's wallet."""
    wallet_address = current_user["wallet_address"]
    service = get_polymarket_service()

    # Retrieve stored private-key / CLOB creds (if user logged in with PK)
    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError:
        stored = None
    pk = stored["private_key"] if stored else None
    creds = stored.get("clob_creds") if stored else None

    balance = await service.get_wallet_balance(
        wallet_address, private_key=pk, clob_creds=creds
    )
    return WalletBalance(**balance)


@router.get("/positions")
async def get_positions(
    current_user: dict = Depends(get_current_user_from_token),
):
    """Get all Polymarket positions for the authenticated user."""
    wallet_address = current_user["wallet_address"]
    service = get_polymarket_service()

    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError:
        stored = None
    pk = stored["private_key"] if stored else None
    creds = stored.get("clob_creds") if stored else None

    positions = await service.get_positions(wallet_address, private_key=pk, clob_creds=creds)
    return {"wallet_address": wallet_address, "positions": positions, "count": len(positions)}


@router.get("/summary", response_model=PortfolioSummary)
async def get_portfolio_summary(
    current_user: dict = Depends(get_current_user_from_token),
):
    """
    Get complete portfolio summary including balance, positions, P&L, and win rate.
    This is the main endpoint used by the Dashboard.
    """
    wallet_address = current_user["wallet_address"]
    service = get_polymarket_service()

    # Retrieve stored private-key / CLOB creds
    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError:
        stored = None
    pk = stored["private_key"] if stored else None
    creds = stored.get("clob_creds") if stored else None

    summary = await service.get_portfolio_summary(
        wallet_address, private_key=pk, clob_creds=creds
    )
    return PortfolioSummary(**summary)


@router.get("/markets")
async def get_active_markets(
    limit: int = 10,
    current_user: dict = Depends(get_current_user_from_token),
):
    """Get trending active markets from Polymarket."""
    service = get_polymarket_service()
    markets = await service.get_active_markets(limit=limit)
    return {"markets": markets, "count": len(markets)}


@router.get("/markets/newest")
async def get_newest_markets(
    limit: int = 60,
):
    """Get the newest/most recently created markets from Polymarket (public)."""
    service = get_polymarket_service()
    markets = await service.get_newest_markets(limit=limit)
    return {"markets": markets, "count": len(markets)}


@router.get("/markets/combined")
async def get_combined_markets(
    limit: int = 60,
    offset: int = 0,
):
    """
    Return the same active-market universe used by the Markets tab,
    with offset pagination for the Opportunities view.
    """
    service = get_polymarket_service()
    result = await service.search_all_markets(
        query="",
        tag="",
        limit=limit,
        offset=offset,
        sort="volume24hr",
    )
    page = [{**m, "_source": "all_markets"} for m in result.get("markets", [])]
    total = int(result.get("total", 0) or 0)
    result_offset = int(result.get("offset", offset) or 0)
    has_more = bool(result.get("has_more", False))
    return {
        "markets": page,
        "count": len(page),
        "total": total,
        "offset": result_offset,
        "has_more": has_more,
    }


@router.get("/markets/prices")
async def get_market_prices(
    limit: int = 20,
):
    """
    Lightweight endpoint that returns only the fields needed for a price
    refresh: id, outcomePrices, volume24hr, liquidity.

    The frontend calls this every ~15 s so we keep the response small.
    """
    service = get_polymarket_service()
    markets = await service.get_active_markets(limit=limit)
    slim = []
    for m in markets:
        slim.append({
            "id": m.get("condition_id") or m.get("id") or m.get("question", ""),
            "question": m.get("question", ""),
            "outcomePrices": m.get("outcomePrices"),
            "bestAsk": m.get("bestAsk"),
            "bestBid": m.get("bestBid"),
            "lastTradePrice": m.get("lastTradePrice"),
            "volume24hr": m.get("volume24hr"),
            "liquidity": m.get("liquidity"),
            "slug": m.get("slug"),
        })
    return {"markets": slim, "ts": utc_now().isoformat()}


@router.get("/positions/prices")
async def get_position_prices(
    current_user: dict = Depends(get_current_user_from_token),
):
    """
    Lightweight endpoint returning current prices for the user's open positions.
    Called every ~15s by the Dashboard for real-time price updates.
    """
    wallet_address = current_user["wallet_address"]
    service = get_polymarket_service()

    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError:
        stored = None
    pk = stored["private_key"] if stored else None
    creds = stored.get("clob_creds") if stored else None

    if not pk:
        return {"prices": {}, "ts": utc_now().isoformat()}

    try:
        clob = service._get_clob_client(pk, creds, signature_type=1)
        positions = await service.get_positions(wallet_address, private_key=pk, clob_creds=creds)

        # Collect asset_ids from positions
        asset_ids = [p["asset_id"] for p in positions if p.get("asset_id")]
        if not asset_ids:
            return {"prices": {}, "ts": utc_now().isoformat()}

        prices: dict[str, float] = {}
        try:
            prices_resp = clob.get_last_trades_prices(
                [BookParams(token_id=aid) for aid in asset_ids]
            )
            for entry in prices_resp:
                token_id = entry.get("token_id") or entry.get("asset_id")
                price = service._to_float(entry.get("price"), 0.0)
                if token_id and price > 0:
                    prices[token_id] = price
        except Exception:
            pass

        # Fill missing with midpoints
        missing = [aid for aid in asset_ids if aid not in prices]
        if missing:
            try:
                mid_resp = clob.get_midpoints(
                    [BookParams(token_id=aid) for aid in missing]
                )
                for entry in mid_resp:
                    token_id = entry.get("token_id") or entry.get("asset_id")
                    mid = service._to_float(entry.get("mid"), 0.0)
                    if token_id and mid > 0:
                        prices[token_id] = mid
            except Exception:
                pass

        return {"prices": prices, "ts": utc_now().isoformat()}
    except Exception as e:
        return {"prices": {}, "error": str(e), "ts": utc_now().isoformat()}
