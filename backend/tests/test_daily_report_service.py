"""Coverage for :mod:`app.services.users.daily_report_service`.

The digest cycle is exercised against a real in-memory SQLite database with
every external boundary doubled:

* ``get_polymarket_service`` returns a fake service (positions, summary,
  markets),
* ``AnalysisClient`` is a recording double for the LLM article,
* the alert dispatch, the SMTP configuration probe and the digest email are
  patched at module level.

The behaviours under test are the ones that cost money or trust when wrong:
assembly and truncation, the per-UTC-day idempotency marker, the paper-mode
branch, the no-channel/no-email skip, and the deterministic fallback article.
"""

import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.models.base import Base
from app.models.market_data.whale_event import WhaleEvent
from app.models.notifications.notification_channel import NotificationChannel
from app.models.notifications.notification_feed_event import NotificationFeedEvent
from app.models.trading.stop_loss import StopLossOrder
from app.models.trading.user_trade import UserTrade
from app.models.users.user import User
from app.models.users.user_settings import UserSettings
from app.services.users import daily_report_service
from app.utils.time import utc_now

USER_ID = 1
WALLET = "0x" + "ab" * 20
MARKET_A = "0xmarket-a"
MARKET_B = "0xmarket-b"
ARTICLE = "P&L summary: +$12.50 today. Key movers and catalysts follow."

POSITIONS = [
    {
        "title": "Will X happen?",
        "condition_id": MARKET_A,
        "market": MARKET_A,
        "outcome": "Yes",
        "size": 10.0,
        "avgPrice": 0.4,
        "curPrice": 0.5,
        "pnl": 1.0,
    },
    {
        "title": "Will Y happen?",
        "condition_id": MARKET_B,
        "market": MARKET_B,
        "outcome": "No",
        "size": 5.0,
        "avgPrice": 0.2,
        "curPrice": 0.1,
        "pnl": -0.5,
    },
]

SUMMARY = {"usdc_balance": 100.0, "total_current_value": 200.0}

MARKETS = [
    {
        "condition_id": MARKET_A,
        "question": "Will X happen?",
        "end_date_iso": "2026-12-31",
    }
]


class FakePolymarketService:
    """Async double for the markets/portfolio surface of the service."""

    def __init__(self, positions=None, summary=None, markets=None):
        self._positions = POSITIONS if positions is None else positions
        self._summary = SUMMARY if summary is None else summary
        self._markets = MARKETS if markets is None else markets
        self.positions_calls: list[tuple] = []
        self.summary_calls: list[tuple] = []
        self.market_calls: list[int] = []

    async def get_positions(self, wallet_address, private_key=None, clob_creds=None):
        self.positions_calls.append((wallet_address, private_key, clob_creds))
        return self._positions

    async def get_portfolio_summary(self, wallet_address, private_key=None, clob_creds=None):
        self.summary_calls.append((wallet_address, private_key, clob_creds))
        return self._summary

    async def get_active_markets(self, limit=60):
        self.market_calls.append(limit)
        return self._markets


class FakeAnalysisClient:
    """Recording double for the analysis client's digest RPC."""

    article = ARTICLE
    instances: list["FakeAnalysisClient"] = []

    def __init__(self, backend=None):
        self.backend = backend
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        FakeAnalysisClient.instances.append(self)

    async def generate_daily_digest(
        self, date, start_value, current_value, positions_json, events, llm_config=None
    ):
        self.calls.append(
            {
                "date": date,
                "start_value": start_value,
                "current_value": current_value,
                "positions_json": positions_json,
                "events": events,
                "llm_config": llm_config,
            }
        )
        return FakeAnalysisClient.article

    async def close(self):
        self.closed = True


class DailyReportServiceTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.db = self.session_factory()

        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        FakeAnalysisClient.instances = []

        self.service = FakePolymarketService()
        self.polymarket_patcher = patch.object(
            daily_report_service, "get_polymarket_service", return_value=self.service
        )
        self.polymarket_patcher.start()
        self.addCleanup(self.polymarket_patcher.stop)

        self.client_patcher = patch.object(
            daily_report_service, "AnalysisClient", FakeAnalysisClient
        )
        self.client_patcher.start()
        self.addCleanup(self.client_patcher.stop)

        self.dispatch_mock = AsyncMock(return_value={"status": "dispatched"})
        self.dispatch_patcher = patch.object(
            daily_report_service, "alert_dispatch", self.dispatch_mock
        )
        self.dispatch_patcher.start()
        self.addCleanup(self.dispatch_patcher.stop)

        self.email_mock = AsyncMock()
        self.email_patcher = patch.object(
            daily_report_service, "send_daily_digest_email", self.email_mock
        )
        self.email_patcher.start()
        self.addCleanup(self.email_patcher.stop)

        self.smtp_patcher = patch.object(
            daily_report_service, "is_smtp_configured", return_value=True
        )
        self.smtp_patcher.start()
        self.addCleanup(self.smtp_patcher.stop)

    # ── fixtures ──

    def _add_user(
        self,
        user_id=USER_ID,
        email="user@example.com",
        *,
        settings_kwargs=None,
        add_settings=True,
        add_channel=True,
    ):
        user = User(
            id=user_id,
            wallet_address=WALLET if user_id == USER_ID else f"0x{user_id:040x}",
            email=email,
        )
        self.db.add(user)
        if add_settings:
            settings = UserSettings(user_id=user_id, **(settings_kwargs or {}))
            self.db.add(settings)
        if add_channel:
            self.db.add(
                NotificationChannel(
                    user_id=user_id,
                    channel_type="telegram",
                    name="Alerts",
                    config_json='{"chat_id": "1"}',
                    is_active=True,
                    created_at=utc_now(),
                )
            )
        self.db.commit()
        return user

    def _seed_activity(self, user_id=USER_ID):
        # Midday anchor keeps every relative offset inside the same UTC day,
        # regardless of when the suite runs.
        now = utc_now().replace(hour=12, minute=0, second=0, microsecond=0)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.db.add(
            NotificationFeedEvent(
                user_id=user_id,
                trader_wallet="",
                event_type="fill",
                market_id=MARKET_A,
                email_status="skipped",
                created_at=now,
            ),
        )
        self.db.add(
            WhaleEvent(
                wallet="0x" + "cd" * 20,
                market_id=MARKET_A,
                side="buy",
                notional=25000.0,
                tx_hash="0xwhale",
                detected_at=now,
            ),
        )
        self.db.add(
            StopLossOrder(
                user_id=user_id,
                token_id="0xtoken",
                market_id=MARKET_B,
                market_title="Will Y happen?",
                size=5.0,
                stop_price=0.15,
                status="triggered",
                triggered_at=now,
            ),
        )
        self.db.add(
            UserTrade(
                user_id=user_id,
                market_id=MARKET_B,
                token_id="0xtoken",
                action="sell",
                amount=1.0,
                price=0.1,
                status="executed",
                pnl=12.5,
                executed_at=now - timedelta(hours=3),
                created_at=day_start,
            )
        )
        self.db.commit()
        return now, day_start

    def _digest_payload(self) -> dict[str, Any]:
        self.assertTrue(self.dispatch_mock.await_args is not None)
        return self.dispatch_mock.await_args.args[2]

    def _email_call(self) -> tuple[str, str, str]:
        self.assertTrue(self.email_mock.await_args is not None)
        return self.email_mock.await_args.args


class AssemblyAndDeliveryTests(DailyReportServiceTestBase):
    async def test_assembles_and_delivers_the_digest(self):
        user = self._add_user()
        now, _day_start = self._seed_activity()

        result = await daily_report_service.generate_digest_for_user(self.db, user, now=now)

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["chat"], "dispatched")
        self.assertEqual(result["email"], "sent")

        # The LLM call receives the full prompt payload.
        self.assertEqual(len(FakeAnalysisClient.instances), 1)
        call = FakeAnalysisClient.instances[0].calls[0]
        self.assertEqual(call["date"], now.strftime("%Y-%m-%d"))
        self.assertEqual(call["current_value"], "300.00")
        # No PortfolioSnapshot has landed: the start value is derived from
        # today's realized P&L (300.00 - 12.50).
        self.assertEqual(call["start_value"], "287.50")
        positions = json.loads(call["positions_json"])
        self.assertEqual(len(positions), 2)
        self.assertEqual(positions[0]["market"], "Will X happen?")
        self.assertIn("fill on 0xmarket-a", call["events"])
        self.assertIn("stop-loss filled on Will Y happen?", call["events"])
        self.assertIn("whale buy $25,000 on 0xmarket-a", call["events"])
        self.assertIn("resolves 2026-12-31", call["events"])

        # Chat dispatch carries the article and reuses the marker row.
        payload = self._digest_payload()
        self.assertIn(ARTICLE, payload["message"])
        self.assertIn("feed_event_id", payload)
        self.assertTrue(payload["skip_feed"])
        self.assertEqual(self.dispatch_mock.await_args.args[0], "daily_digest")
        self.assertEqual(self.dispatch_mock.await_args.args[1], USER_ID)

        # Direct email carries the labelled header plus the article.
        to_email, subject, body = self._email_call()
        self.assertEqual(to_email, "user@example.com")
        self.assertIn(now.strftime("%Y-%m-%d"), subject)
        self.assertIn("Start value: $287.50 (derived from today's realized P&L)", body)
        self.assertIn("Realized P&L today: +$12.50", body)
        self.assertIn(ARTICLE, body)

        # The idempotency marker was written and records the email.
        marker = (
            self.db.query(NotificationFeedEvent)
            .filter(NotificationFeedEvent.event_type == "daily_digest")
            .one()
        )
        self.assertEqual(marker.email_status, "sent")
        self.assertIsNotNone(marker.emailed_at)
        self.assertEqual(payload["feed_event_id"], marker.id)

    async def test_full_position_list_is_capped_and_sorted(self):
        many = [
            {
                "condition_id": f"market-{index}",
                "market": f"market-{index}",
                "title": f"Market {index}",
                "outcome": "Yes",
                "size": float(index),
                "avgPrice": 0.5,
                "curPrice": 0.5,
                "pnl": 0.0,
            }
            for index in range(30)
        ]
        self.service._positions = many
        user = self._add_user()

        result = await daily_report_service.generate_digest_for_user(self.db, user)

        self.assertEqual(result["status"], "sent")
        positions = json.loads(FakeAnalysisClient.instances[0].calls[0]["positions_json"])
        self.assertEqual(len(positions), 25)
        self.assertEqual(positions[0]["market"], "Market 29")
        # The prompt is capped, the count is not: a 30-position user reads
        # "Open positions: 30", never the post-cap 25.
        _to_email, _subject, body = self._email_call()
        self.assertIn("Open positions: 30", body)

    async def test_default_settings_row_is_used_when_missing(self):
        user = self._add_user(add_settings=False)

        result = await daily_report_service.generate_digest_for_user(self.db, user)

        self.assertEqual(result["status"], "sent")
        self.assertEqual(self.email_mock.await_count, 1)


class IdempotencyTests(DailyReportServiceTestBase):
    async def test_second_digest_same_utc_day_is_skipped(self):
        user = self._add_user()
        now, _ = self._seed_activity()

        first = await daily_report_service.generate_digest_for_user(self.db, user, now=now)
        second = await daily_report_service.generate_digest_for_user(self.db, user, now=now)

        self.assertEqual(first["status"], "sent")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["reason"], "already_sent_today")
        self.assertEqual(self.email_mock.await_count, 1)
        self.assertEqual(self.dispatch_mock.await_count, 1)

    async def test_existing_marker_blocks_dispatch_and_email(self):
        user = self._add_user()
        now = utc_now()
        self.db.add(
            NotificationFeedEvent(
                user_id=USER_ID,
                trader_wallet="",
                event_type="daily_digest",
                email_status="sent",
                created_at=now,
            ),
        )
        self.db.commit()

        result = await daily_report_service.generate_digest_for_user(self.db, user, now=now)

        self.assertEqual(result["reason"], "already_sent_today")
        self.email_mock.assert_not_awaited()
        self.dispatch_mock.assert_not_awaited()

    async def test_assembly_failure_removes_the_marker_for_a_retry(self):
        user = self._add_user()
        now, _ = self._seed_activity()

        with patch.object(
            daily_report_service,
            "_fetch_portfolio",
            new=AsyncMock(side_effect=RuntimeError("upstream down")),
        ):
            result = await daily_report_service.generate_digest_for_user(self.db, user, now=now)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "assembly_failed")
        markers = (
            self.db.query(NotificationFeedEvent)
            .filter(NotificationFeedEvent.event_type == "daily_digest")
            .all()
        )
        self.assertEqual(markers, [])
        self.email_mock.assert_not_awaited()


class PreferenceTests(DailyReportServiceTestBase):
    async def test_disabled_flag_skips_the_digest(self):
        user = self._add_user(settings_kwargs={"daily_digest_enabled": False})
        self._seed_activity()

        result = await daily_report_service.generate_digest_for_user(self.db, user)

        self.assertEqual(result["reason"], "disabled")
        self.email_mock.assert_not_awaited()
        self.dispatch_mock.assert_not_awaited()

    async def test_no_channels_and_no_email_is_skipped_silently(self):
        user = self._add_user(email=None, add_channel=False)

        result = await daily_report_service.generate_digest_for_user(self.db, user)

        self.assertEqual(result["reason"], "no_delivery_target")
        markers = (
            self.db.query(NotificationFeedEvent)
            .filter(NotificationFeedEvent.event_type == "daily_digest")
            .all()
        )
        self.assertEqual(markers, [])
        self.email_mock.assert_not_awaited()

    async def test_smtp_not_configured_skips_the_email_only(self):
        user = self._add_user()
        self._seed_activity()
        with patch.object(daily_report_service, "is_smtp_configured", return_value=False):
            result = await daily_report_service.generate_digest_for_user(self.db, user)

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["chat"], "dispatched")
        self.assertEqual(result["email"], "skipped")
        self.email_mock.assert_not_awaited()


class PaperModeTests(DailyReportServiceTestBase):
    async def test_paper_users_are_reported_against_their_paper_balance(self):
        user = self._add_user(settings_kwargs={"simulation_mode": True, "paper_balance": 1234.5})
        now = utc_now().replace(hour=12, minute=0, second=0, microsecond=0)
        self.db.add(
            UserTrade(
                user_id=USER_ID,
                market_id=MARKET_A,
                token_id="0xtoken",
                action="sell",
                amount=1.0,
                price=0.5,
                status="simulated",
                pnl=-3.25,
                executed_at=now - timedelta(hours=2),
                created_at=now,
            )
        )
        self.db.commit()

        result = await daily_report_service.generate_digest_for_user(self.db, user, now=now)

        self.assertEqual(result["status"], "sent")
        call = FakeAnalysisClient.instances[0].calls[0]
        self.assertEqual(call["current_value"], "1234.50")
        # Paper start value: 1234.50 - (-3.25) = 1237.75.
        self.assertEqual(call["start_value"], "1237.75")
        _to_email, subject, body = self._email_call()
        self.assertIn("Paper", subject)
        self.assertIn("(Paper)", body)


class DeliveryFailureTests(DailyReportServiceTestBase):
    async def test_email_failure_is_recorded_on_the_marker(self):
        user = self._add_user()
        self._seed_activity()
        self.email_mock.side_effect = RuntimeError("smtp down")

        result = await daily_report_service.generate_digest_for_user(self.db, user)

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["email"], "failed")
        marker = (
            self.db.query(NotificationFeedEvent)
            .filter(NotificationFeedEvent.event_type == "daily_digest")
            .one()
        )
        self.assertEqual(marker.email_status, "failed")
        self.assertIsNone(marker.emailed_at)
        # The day is consumed even though the email failed: at-most-once.

    async def test_dispatch_failure_does_not_block_the_email(self):
        user = self._add_user()
        self._seed_activity()
        self.dispatch_mock.side_effect = RuntimeError("telegram down")

        result = await daily_report_service.generate_digest_for_user(self.db, user)

        self.assertEqual(result["chat"], "failed")
        self.assertEqual(result["email"], "sent")
        self.assertEqual(self.email_mock.await_count, 1)


class FallbackArticleTests(DailyReportServiceTestBase):
    async def test_empty_llm_article_falls_back_to_the_deterministic_report(self):
        FakeAnalysisClient.article = ""
        try:
            user = self._add_user()
            self._seed_activity()

            result = await daily_report_service.generate_digest_for_user(self.db, user)
        finally:
            FakeAnalysisClient.article = ARTICLE

        self.assertEqual(result["status"], "sent")
        _to_email, _subject, body = self._email_call()
        self.assertIn("P&L today: +$12.50", body)
        self.assertIn("stop-loss filled on Will Y happen?", body)
        self.assertGreaterEqual(daily_report_service.get_daily_report_metrics()["llm_fallbacks"], 1)

    async def test_llm_failure_still_sends_the_deterministic_report(self):
        """A failing LLM call must not burn the whole day.

        Only the constructor used to be guarded: the awaited digest call
        escaped to the assembly-failure path, so one bad night meant no
        digest at all and a fresh retry the next day.
        """

        async def _boom(self, **_kwargs):
            raise RuntimeError("llm down")

        original = FakeAnalysisClient.generate_daily_digest
        FakeAnalysisClient.generate_daily_digest = _boom
        try:
            user = self._add_user()
            self._seed_activity()

            result = await daily_report_service.generate_digest_for_user(self.db, user)
        finally:
            FakeAnalysisClient.generate_daily_digest = original

        self.assertEqual(result["status"], "sent")
        _to_email, _subject, body = self._email_call()
        self.assertIn("P&L today: +$12.50", body)
        self.assertNotIn(ARTICLE, body)
        self.assertGreaterEqual(daily_report_service.get_daily_report_metrics()["llm_fallbacks"], 1)
        # The article never reached the chat payload.
        payload = self._digest_payload()
        self.assertNotIn(ARTICLE, payload["message"])


class CycleTests(DailyReportServiceTestBase):
    def setUp(self):
        super().setUp()
        self.session_patcher = patch.object(daily_report_service, "SessionLocal", lambda: self.db)
        self.session_patcher.start()
        self.addCleanup(self.session_patcher.stop)

    async def test_run_daily_digests_walks_every_user(self):
        first = self._add_user(user_id=1)
        second = self._add_user(
            user_id=2,
            email="second@example.com",
            settings_kwargs={"daily_digest_enabled": False},
        )
        self._seed_activity()

        stats = await daily_report_service.run_daily_digests()

        self.assertEqual(stats["users"], 2)
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(self.email_mock.await_count, 1)
        self.assertIsNotNone(first.id)
        self.assertIsNotNone(second.id)

    async def test_run_daily_digests_survives_a_user_failure(self):
        self._add_user()
        self._seed_activity()

        with patch.object(
            daily_report_service,
            "generate_digest_for_user",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            stats = await daily_report_service.run_daily_digests()

        self.assertEqual(stats["failed"], 1)
        # The user's attribute access is deliberately avoided: the failure
        # path rolls the shared session back, expiring pending instances.

    async def test_assembly_failure_is_counted_as_failed_not_skipped(self):
        """A returned ``failed`` status is damage, not a quiet skip.

        Folding it into ``skipped`` (and never counting
        ``digests_failed``) made an assembly-wide outage look like a
        normal opt-out night after night.
        """
        self._add_user()
        self._seed_activity()

        with patch.object(
            daily_report_service,
            "generate_digest_for_user",
            new=AsyncMock(return_value={"status": "failed", "reason": "assembly_failed"}),
        ):
            stats = await daily_report_service.run_daily_digests()

        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(
            daily_report_service.get_daily_report_metrics()["digests_failed"],
            1,
        )

    async def test_long_sleep_is_chunked_so_the_dead_man_switch_fires(self):
        """Heartbeats must refresh before the TTL, not only after a cycle.

        ``HEARTBEAT_TTL_SECONDS`` is 86400, i.e. exactly one day, so the
        un-chunked sleep-first loop expired the previous heartbeat at the
        very moment the next write happened and a dead digest job was
        silent forever. The wait is chunked at ≤1h with a heartbeat per
        chunk.
        """
        heartbeat_patcher = patch.object(daily_report_service, "scheduler_heartbeat")
        heartbeat_mock = heartbeat_patcher.start()
        self.addCleanup(heartbeat_patcher.stop)
        chunks: list[float] = []

        async def _fake_sleep(seconds):
            chunks.append(seconds)
            if len(chunks) > 3:
                raise asyncio.CancelledError

        with (
            patch.object(daily_report_service, "acquire_scheduler_lock", return_value=True),
            patch.object(daily_report_service, "release_scheduler_lock"),
            patch.object(daily_report_service, "run_daily_digests", new=AsyncMock()) as run_mock,
            patch.object(daily_report_service, "_seconds_until_next_run", lambda: 5 * 3600),
            patch.object(daily_report_service.asyncio, "sleep", new=_fake_sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await daily_report_service._daily_digest_loop()

        # Three completed chunks (≤1h each), one heartbeat after each.
        self.assertEqual(len(chunks), 4)
        self.assertTrue(all(c <= daily_report_service.HEARTBEAT_CHUNK_SECONDS for c in chunks[:3]))
        self.assertEqual(heartbeat_mock.call_count, 3)
        run_mock.assert_not_awaited()

    async def test_service_lifecycle_starts_and_stops(self):
        with (
            patch.object(daily_report_service, "acquire_scheduler_lock", return_value=True),
            patch.object(daily_report_service, "release_scheduler_lock"),
            patch.object(daily_report_service, "scheduler_heartbeat"),
            patch.object(daily_report_service, "run_daily_digests", new=AsyncMock()) as run_mock,
        ):
            await daily_report_service.start_daily_report_service()
            await asyncio.sleep(0)
            await daily_report_service.stop_daily_report_service()

        run_mock.assert_not_awaited()
        self.assertIsNone(daily_report_service._digest_task)


class ScheduleMathTests(unittest.TestCase):
    def test_seconds_until_next_run_before_the_hour(self):
        now = datetime(2026, 10, 11, 7, 0, 0, tzinfo=UTC)
        self.assertEqual(daily_report_service._seconds_until_next_run(now), 3600.0)

    def test_seconds_until_next_run_after_the_hour(self):
        now = datetime(2026, 10, 11, 9, 30, 0, tzinfo=UTC)
        self.assertEqual(daily_report_service._seconds_until_next_run(now), 22.5 * 3600)


if __name__ == "__main__":
    unittest.main()
