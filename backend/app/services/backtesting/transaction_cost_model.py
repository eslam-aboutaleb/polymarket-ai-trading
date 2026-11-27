"""Fee model. Missing cost data is reported unknown, never as zero."""

from decimal import Decimal

from app.services.backtesting.contracts import EngineConfig


class CostModel:
    def __init__(self, config: EngineConfig):
        self.taker_fee_bps = config.taker_fee_bps
        self.maker_fee_bps = config.maker_fee_bps
        self.fee_schedule_id = config.fee_schedule_id

    @property
    def taker_known(self) -> bool:
        return self.taker_fee_bps is not None

    @property
    def maker_known(self) -> bool:
        return self.maker_fee_bps is not None

    def ledger_bps(self, liquidity: str) -> Decimal:
        """Fee rate handed to the ledger. Unknown rates book as 0 and are flagged."""
        value = self.taker_fee_bps if liquidity == "taker" else self.maker_fee_bps
        return Decimal(0) if value is None else value

    def known(self, liquidity: str) -> bool:
        return self.taker_known if liquidity == "taker" else self.maker_known
