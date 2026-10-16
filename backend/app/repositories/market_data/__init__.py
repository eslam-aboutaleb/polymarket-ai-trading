"""Persistence queries for normalized market data and whale activity."""

from app.repositories.market_data.markets import MarketRepository
from app.repositories.market_data.venues import VenueMarketRepository
from app.repositories.market_data.whales import WhaleRepository
from app.repositories.market_data.winners import WinnerRepository

WhaleEventRepository = WhaleRepository

__all__ = [
    "MarketRepository",
    "VenueMarketRepository",
    "WhaleEventRepository",
    "WhaleRepository",
    "WinnerRepository",
]
