"""Capital-accounting regressions: scope, replay and unknown costs."""

from datetime import timedelta

from test_pnl_reconciliation import BASE_TIME, PnlReconciliationTestBase

from app.services.analytics.pnl_reconciliation import reconcile_realized_pnl


class PnlIntegrityTests(PnlReconciliationTestBase):
    def test_repeated_reconciliation_consumes_previously_closed_lots(self):
        self._buy(price=0.2, amount=2, token_id="yes")
        self._buy(price=0.8, amount=8, token_id="yes", executed_at=BASE_TIME + timedelta(seconds=1))
        self._sell(
            price=0.5, amount=5, token_id="yes", executed_at=BASE_TIME + timedelta(seconds=2)
        )
        reconcile_realized_pnl(self.db, 1)
        final = self._sell(
            price=0.5, amount=5, token_id="yes", executed_at=BASE_TIME + timedelta(seconds=3)
        )
        reconcile_realized_pnl(self.db, 1)
        self.db.refresh(final)
        self.assertAlmostEqual(final.pnl, -3)

    def test_opposite_tokens_of_same_condition_never_share_basis(self):
        self._buy(token_id="yes")
        sell = self._sell(token_id="no")
        reconcile_realized_pnl(self.db, 1)
        self.db.refresh(sell)
        self.assertIsNone(sell.pnl)

    def test_subaccounts_never_share_cost_basis(self):
        self._buy(account_id=11, token_id="yes")
        sell = self._sell(account_id=12, token_id="yes")
        reconcile_realized_pnl(self.db, 1)
        self.db.refresh(sell)
        self.assertIsNone(sell.pnl)
