"""Kelly sizing tests (plan 04).

Known p/price combinations are asserted against the
documented Kelly math (p=0.8, price=0.5 → b=1.0,
f*=0.6 → quarter Kelly = 0.15 of bankroll), together
with cap enforcement, the no-edge → 0 path, invalid
multiplier rejection, edge-source resolution against a
real in-memory database, and the paper-mode copy-trade
integration (calculation_details records p, price,
f*, multiplier and the final size).
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.models.assessment import Assessment
from app.models.base import Base
from app.models.followed_trader import FollowedTrader
from app.models.trade_history import TradeHistory
from app.models.user import User
from app.models.user_settings import UserSettings
from app.models.user_trade import UserTrade
from app.models.winner import Winner
from app.services import copy_trade_service, kelly_service
from app.utils.time import utc_now

WALLET = "0xkelly-user-wallet"
USER_ID = 5242
TRADER_WALLET = "0xkelly-trader-wallet"
MARKET_ID = "kelly-test-market"


def _make_db() -> sessionmaker:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


class KellyFractionTests(unittest.TestCase):
    """Full-Kelly fraction for known p/price combinations."""

    def test_even_money_with_80pct_edge_is_60pct_of_bankroll(self):
        # p=0.8, price=0.5 → b=(1-0.5)/0.5=1.0, q=0.2,
        # f*=(1.0*0.8-0.2)/1.0=0.6
        self.assertAlmostEqual(kelly_service.kelly_fraction(0.8, 0.5), 0.6)

    def test_probability_equal_to_price_has_no_edge(self):
        # p=0.5 at price 0.5: b*p == q → f*=0
        self.assertEqual(kelly_service.kelly_fraction(0.5, 0.5), 0.0)

    def test_negative_edge_clamps_to_zero(self):
        # p=0.3 at price 0.5: (0.3-0.7)/1.0 < 0 → 0
        self.assertEqual(kelly_service.kelly_fraction(0.3, 0.5), 0.0)

    def test_favorable_odds_at_high_price(self):
        # p=0.95, price=0.9: b=1/9, q=0.05,
        # f*=((1/9)*0.95-0.05)/(1/9)=0.5
        self.assertAlmostEqual(kelly_service.kelly_fraction(0.95, 0.9), 0.5)

    def test_degenerate_inputs_return_zero(self):
        for p, price in ((0.0, 0.5), (1.0, 0.5), (0.8, 0.0), (0.8, 1.0), (None, 0.5), (0.8, None)):
            self.assertEqual(kelly_service.kelly_fraction(p, price), 0.0)


class KellySizeTests(unittest.TestCase):
    """Fractional-Kelly wager sizing and clamping."""

    def test_quarter_kelly_of_1000_bankroll_is_150(self):
        # f*=0.6 × multiplier 0.25 × bankroll 1000 = 150
        # (0.15 of bankroll)
        self.assertEqual(kelly_service.size(1000, 0.8, 0.5, 0.25), 150.0)

    def test_full_kelly_multiplier_one(self):
        self.assertEqual(kelly_service.size(1000, 0.8, 0.5, 1.0), 600.0)

    def test_max_position_size_cap_enforced(self):
        self.assertEqual(
            kelly_service.size(1000, 0.8, 0.5, 0.25, max_position_size=100),
            100.0,
        )

    def test_min_order_floor_enforced(self):
        # 0.6 × 0.25 × 1 = 0.15 → floored to the $1 minimum
        self.assertEqual(
            kelly_service.size(1, 0.8, 0.5, 0.25, min_order=1.0),
            1.0,
        )

    def test_no_edge_sizes_to_zero(self):
        self.assertEqual(kelly_service.size(1000, 0.5, 0.5, 0.25), 0.0)
        self.assertEqual(kelly_service.size(1000, 0.2, 0.5, 0.25), 0.0)

    def test_zero_bankroll_sizes_to_zero(self):
        self.assertEqual(kelly_service.size(0, 0.8, 0.5, 0.25), 0.0)

    def test_invalid_multiplier_rejected(self):
        for multiplier in (0.0, -0.25, 1.5, 2.0):
            with self.assertRaises(ValueError):
                kelly_service.size(1000, 0.8, 0.5, multiplier)

    def test_default_multiplier_is_quarter_kelly(self):
        self.assertEqual(
            kelly_service.size(1000, 0.8, 0.5),
            kelly_service.size(1000, 0.8, 0.5, 0.25),
        )


class EffectiveTradeParamsTests(unittest.TestCase):
    """Side mapping: a SELL is the mirror image of a BUY."""

    def test_buy_uses_market_probability_and_price(self):
        self.assertEqual(
            kelly_service.effective_trade_params(0.7, "BUY", 0.6),
            (0.7, 0.6),
        )

    def test_sell_inverts_probability_and_price(self):
        # Selling Yes at 0.6 == buying No at 0.4 with
        # win probability 1-0.7=0.3
        p, price = kelly_service.effective_trade_params(0.7, "SELL", 0.6)
        self.assertAlmostEqual(p, 0.3)
        self.assertAlmostEqual(price, 0.4)

    def test_sell_kelly_matches_buy_of_the_opposite_outcome(self):
        # Kelly of selling Yes at 0.6 with P(Yes)=0.7 must
        # equal Kelly of buying No at 0.4 with P(No)=0.3.
        p_sell, price_sell = kelly_service.effective_trade_params(0.7, "SELL", 0.6)
        self.assertEqual(
            kelly_service.kelly_fraction(p_sell, price_sell),
            kelly_service.kelly_fraction(0.3, 0.4),
        )


class ResolveEdgeProbabilityTests(unittest.TestCase):
    """Edge-source resolution preference order against a real DB."""

    def setUp(self):
        self.session_factory = _make_db()
        self.db = self.session_factory()
        self.db.add(User(id=USER_ID, wallet_address=WALLET))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _add_assessment(self, confidence: float, recommendation: str = "copy"):
        history = TradeHistory(
            market_id=MARKET_ID,
            wallet_address=TRADER_WALLET,
            order_type="buy",
            amount=100.0,
            price=0.5,
            timestamp=utc_now(),
        )
        self.db.add(history)
        self.db.flush()
        self.db.add(
            Assessment(
                trade_history_id=history.id,
                ai_score=confidence,
                reasoning="test",
                recommendation=recommendation,
                risk_level="medium",
                confidence=confidence,
            )
        )
        self.db.commit()

    def test_no_estimates_returns_none(self):
        p, source = kelly_service.resolve_edge_probability(self.db, USER_ID, MARKET_ID)
        self.assertIsNone(p)
        self.assertIsNone(source)

    def test_ai_assessment_confidence_is_preferred(self):
        self._add_assessment(confidence=72.0)
        p, source = kelly_service.resolve_edge_probability(self.db, USER_ID, MARKET_ID)
        self.assertEqual(p, 0.72)
        self.assertEqual(source, "ai_assessment")

    def test_avoid_recommendation_carries_no_edge(self):
        self._add_assessment(confidence=90.0, recommendation="avoid")
        p, source = kelly_service.resolve_edge_probability(self.db, USER_ID, MARKET_ID)
        self.assertIsNone(p)
        self.assertIsNone(source)

    def test_trader_win_rate_fallback(self):
        self.db.add(
            Winner(
                wallet_address=TRADER_WALLET,
                win_rate=64.0,
                trade_count=50,
            )
        )
        self.db.commit()
        p, source = kelly_service.resolve_edge_probability(
            self.db,
            USER_ID,
            MARKET_ID,
            trader_wallet=TRADER_WALLET,
        )
        self.assertEqual(p, 0.64)
        self.assertEqual(source, "trader_win_rate")

    def test_ai_assessment_beats_trader_win_rate(self):
        self._add_assessment(confidence=72.0)
        self.db.add(
            Winner(
                wallet_address=TRADER_WALLET,
                win_rate=64.0,
                trade_count=50,
            )
        )
        self.db.commit()
        p, source = kelly_service.resolve_edge_probability(
            self.db,
            USER_ID,
            MARKET_ID,
            trader_wallet=TRADER_WALLET,
        )
        self.assertEqual(source, "ai_assessment")
        self.assertEqual(p, 0.72)

    def test_user_own_win_rate_fallback(self):
        for pnl in (10.0, -5.0, 10.0, 10.0):  # 3 wins / 4 trades
            self.db.add(
                UserTrade(
                    user_id=USER_ID,
                    market_id=MARKET_ID,
                    action="buy",
                    amount=10.0,
                    price=0.5,
                    status="executed",
                    pnl=pnl,
                )
            )
        self.db.commit()
        p, source = kelly_service.resolve_edge_probability(self.db, USER_ID, MARKET_ID)
        self.assertEqual(p, 0.75)
        self.assertEqual(source, "user_win_rate")

    def test_pending_trades_do_not_count_toward_win_rate(self):
        self.db.add(
            UserTrade(
                user_id=USER_ID,
                market_id=MARKET_ID,
                action="buy",
                amount=10.0,
                price=0.5,
                status="pending",
                pnl=10.0,
            )
        )
        self.db.commit()
        p, source = kelly_service.resolve_edge_probability(self.db, USER_ID, MARKET_ID)
        self.assertIsNone(p)
        self.assertIsNone(source)


class ComputeBankrollTests(unittest.IsolatedAsyncioTestCase):
    """Bankroll = USDC balance + unrealized PnL."""

    async def test_bankroll_sums_balance_and_unrealized_pnl(self):
        service = MagicMock()
        service.get_wallet_balance = AsyncMock(
            return_value={"usdc_balance": 500.0},
        )
        service.get_positions = AsyncMock(
            return_value=[
                {"size": 10.0, "avgPrice": 0.4, "curPrice": 0.7},  # +3.0
                {"size": 5.0, "avgPrice": 0.6, "curPrice": 0.5},  # -0.5
            ],
        )
        bankroll = await kelly_service.compute_bankroll(service, WALLET)
        self.assertEqual(bankroll, 502.5)

    async def test_bankroll_degrades_when_endpoints_fail(self):
        service = MagicMock()
        service.get_wallet_balance = AsyncMock(side_effect=RuntimeError("down"))
        service.get_positions = AsyncMock(side_effect=RuntimeError("down"))
        bankroll = await kelly_service.compute_bankroll(service, WALLET)
        self.assertEqual(bankroll, 0.0)


class KellyCopyTradeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Paper-mode copy trade with sizing_mode=kelly records the math."""

    async def asyncSetUp(self):
        self.session_factory = _make_db()
        self.db = self.session_factory()
        self.db.add(User(id=USER_ID, wallet_address=WALLET))
        self.db.add(
            UserSettings(
                user_id=USER_ID,
                copy_trading_enabled=True,
                simulation_mode=True,
                max_position_size=100.0,
                daily_loss_limit=500.0,
                kelly_fraction=0.25,
            )
        )
        self.db.add(
            FollowedTrader(
                user_id=USER_ID,
                trader_wallet=TRADER_WALLET,
                is_active=True,
                sizing_mode="kelly",
            )
        )
        self.db.add(
            Winner(
                wallet_address=TRADER_WALLET,
                win_rate=80.0,
                trade_count=50,
            )
        )
        self.db.commit()

    async def asyncTearDown(self):
        self.db.close()

    async def test_kelly_sizing_records_calculation_details(self):
        balance = {"usdc_balance": 1000.0}
        service = MagicMock()
        service.get_wallet_balance = AsyncMock(return_value=balance)
        service.get_positions = AsyncMock(return_value=[])

        with patch.object(
            copy_trade_service,
            "get_polymarket_service",
            return_value=service,
        ):
            result = await copy_trade_service.execute_copy_trade(
                self.db,
                USER_ID,
                TRADER_WALLET,
                MARKET_ID,
                "0xtoken",
                "BUY",
                0.5,
                100.0,
                trade_history_id=None,
                assessment=None,
            )

        self.assertTrue(result["executed"])
        self.assertTrue(result["simulated"])

        trade = (
            self.db.query(UserTrade)
            .filter(UserTrade.user_id == USER_ID)
            .order_by(UserTrade.id.desc())
            .first()
        )
        self.assertIsNotNone(trade)
        self.assertEqual(trade.sizing_mode_applied, "kelly")

        import json

        details = json.loads(trade.calculation_details)
        # Edge: trader win rate 80% at price 0.5 →
        # f*=0.6, quarter Kelly of 1000 bankroll = 150.
        self.assertEqual(details["kelly_p"], 0.8)
        self.assertEqual(details["kelly_price"], 0.5)
        self.assertAlmostEqual(details["kelly_f_star"], 0.6)
        self.assertEqual(details["kelly_multiplier"], 0.25)
        self.assertEqual(details["kelly_bankroll"], 1000.0)
        self.assertEqual(details["kelly_edge_source"], "trader_win_rate")
        self.assertEqual(details["trade_size_final"], 100.0)  # capped by max_position_size
        self.assertEqual(trade.amount, 100.0)

    async def test_no_edge_copy_trade_is_skipped(self):
        # Win rate 50% at price 0.5 → f*=0 → no size.
        self.db.query(Winner).update({"win_rate": 50.0})
        self.db.commit()

        service = MagicMock()
        service.get_wallet_balance = AsyncMock(return_value={"usdc_balance": 1000.0})
        service.get_positions = AsyncMock(return_value=[])

        with patch.object(
            copy_trade_service,
            "get_polymarket_service",
            return_value=service,
        ):
            result = await copy_trade_service.execute_copy_trade(
                self.db,
                USER_ID,
                TRADER_WALLET,
                MARKET_ID,
                "0xtoken",
                "BUY",
                0.5,
                100.0,
            )

        self.assertFalse(result["executed"])
        self.assertIn("zero", result["reason"])


if __name__ == "__main__":
    unittest.main()
