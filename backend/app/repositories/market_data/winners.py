"""Leaderboard winner queries."""

from sqlalchemy.orm import Session

from app.models.market_data.winner import Winner


class WinnerRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_wallet(self, wallet_address: str) -> Winner | None:
        return self.db.query(Winner).filter(Winner.wallet_address == wallet_address).first()

    def list_with_minimum_trades(self, min_trades: int) -> list[Winner]:
        return self.db.query(Winner).filter(Winner.trade_count >= min_trades).all()
