"""Compatibility export for the canonical whale repository."""

from app.repositories.market_data.whales import WhaleRepository

WhaleEventRepository = WhaleRepository

__all__ = ["WhaleEventRepository", "WhaleRepository"]
