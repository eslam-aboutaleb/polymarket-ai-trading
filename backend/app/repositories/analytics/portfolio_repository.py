"""Compatibility export for the canonical portfolio analytics repository."""

from app.repositories.analytics.portfolio import PortfolioAnalyticsRepository

PortfolioRepository = PortfolioAnalyticsRepository

__all__ = ["PortfolioRepository", "PortfolioAnalyticsRepository"]
