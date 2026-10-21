"""Collaborative forecasting tests (plan 02).

Pins the contracts the panel's credibility rests on:

* the ensemble is a weighted mean where a newcomer's weight is exactly 1
  (never 0) and a measured skill raises it by ``k · skill``,
* a consensus pools only the *latest* vote per forecaster, and only
  public ones,
* a private annotation or vote never appears in another user's response,
* a market resolving within an hour, a closed market and an exhausted
  vote budget are all refused,
* a cached consensus is served, and a new vote invalidates it,
* every route refuses an unauthenticated caller, and the forecaster
  board's global scope is admin-only,
* self-following is refused.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register every model on Base.metadata)
from app.api.deps import get_active_account
from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.platform.collaboration import (
    _collaboration_read_rate_limit,
    _collaboration_write_rate_limit,
    router,
)
from app.models.analytics.assessment import Assessment
from app.models.base import Base
from app.models.intelligence.collaboration import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    ForecastVote,
    MarketAnnotation,
    UserFollow,
)
from app.models.market_data.market import Market
from app.models.users.user import User
from app.repositories.intelligence.assessments import AssessmentRepository
from app.services.intelligence.consensus_service import (
    CONSENSUS_CACHE_TTL_SECONDS,
    MAX_VOTES_PER_HOUR,
    MIN_RESOLVED_FOR_SKILL,
    SKILL_WEIGHT_K,
    VoteRejected,
    forecaster_leaderboard,
    forecaster_skill,
    get_consensus,
    latest_votes,
    reset_consensus_cache,
    save_annotation,
    submit_vote,
    vote_weight,
)
from app.services.intelligence.forecaster_follow_service import (
    FollowRejected,
    follow_user,
    following_forecasts,
    list_following,
    unfollow_user,
)
from app.utils.database import get_db
from app.utils.time import utc_now

WALLET_A = "0x" + "aa" * 20
WALLET_B = "0x" + "bb" * 20
WALLET_C = "0x" + "cc" * 20
WALLET_D = "0x" + "dd" * 20

CONDITION = "0xcondition-1"
RESOLVED_CONDITIONS = [f"0xresolved-{index}" for index in range(3)]


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, session_factory


def _assessment(condition_id: str, outcome: str, probability: float = 0.9) -> Assessment:
    return Assessment(
        condition_id=condition_id,
        ai_score=50,
        reasoning="seeded",
        recommendation="hold",
        risk_level="unknown",
        confidence=50,
        ai_probability=probability,
        resolved_outcome=outcome,
        brier_score=(probability - 1.0) ** 2 if outcome == "YES" else probability**2,
        task="opportunity_scan",
        created_at=utc_now(),
    )


class ConsensusTestCase(unittest.TestCase):
    """Shared in-memory database with four seeded users."""

    def setUp(self):
        self.engine, self.session_factory = _make_db()
        self.db = self.session_factory()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.addCleanup(reset_consensus_cache)
        for wallet in (WALLET_A, WALLET_B, WALLET_C, WALLET_D):
            self.db.add(User(wallet_address=wallet))
        self.db.commit()
        # A2's votes resolve YES three times at p=0.9 → mean Brier 0.01.
        for condition in RESOLVED_CONDITIONS:
            self.db.add(
                ForecastVote(
                    user_id=2,
                    condition_id=condition,
                    probability=0.9,
                    vote_seq=1,
                    visibility=VISIBILITY_PUBLIC,
                    created_at=utc_now(),
                )
            )
            self.db.add(_assessment(condition, "YES"))
        self.db.commit()

    def _vote(
        self,
        user_id: int,
        probability: float,
        *,
        condition_id: str = CONDITION,
        visibility: str = VISIBILITY_PUBLIC,
        vote_seq: int = 1,
    ) -> ForecastVote:
        vote = ForecastVote(
            user_id=user_id,
            condition_id=condition_id,
            probability=probability,
            vote_seq=vote_seq,
            visibility=visibility,
            created_at=utc_now(),
        )
        self.db.add(vote)
        self.db.commit()
        return vote


class EnsembleMathTest(ConsensusTestCase):
    """The weighted mean, hand-computed against the documented formula."""

    def test_newcomer_weight_is_one_not_zero(self):
        skill = forecaster_skill(self.db, 1)
        self.assertEqual(skill["resolved"], 0)
        self.assertFalse(skill["sufficient_samples"])
        self.assertEqual(skill["skill"], 0.0)
        # A user with no resolved forecasts must still be able to
        # participate: weight 1, never 0.
        self.assertEqual(vote_weight(skill), 1.0)

    def test_skill_raises_the_weight_by_k_times_skill(self):
        skill = forecaster_skill(self.db, 2)
        self.assertEqual(skill["resolved"], MIN_RESOLVED_FOR_SKILL)
        self.assertTrue(skill["sufficient_samples"])
        self.assertAlmostEqual(skill["mean_brier"], 0.01, places=4)
        # Brier skill against the 0.25 baseline, clipped at zero.
        self.assertAlmostEqual(skill["skill"], 0.24, places=4)
        self.assertAlmostEqual(vote_weight(skill), 1.0 + SKILL_WEIGHT_K * 0.24, places=6)

    def test_skill_is_clipped_at_zero_for_a_worse_than_baseline_forecaster(self):
        for condition in (f"0xbad-{index}" for index in range(MIN_RESOLVED_FOR_SKILL)):
            self.db.add(
                ForecastVote(
                    user_id=3,
                    condition_id=condition,
                    probability=0.9,
                    vote_seq=1,
                    visibility=VISIBILITY_PRIVATE,
                    created_at=utc_now(),
                )
            )
            self.db.add(_assessment(condition, "NO"))
        self.db.commit()

        skill = forecaster_skill(self.db, 3)
        self.assertAlmostEqual(skill["mean_brier"], 0.81, places=4)
        self.assertEqual(skill["skill"], 0.0)
        # Never a negative weight: silencing a forecaster is a moderation
        # decision, not an arithmetic one.
        self.assertEqual(vote_weight(skill), 1.0)

    def test_consensus_is_the_hand_computed_weighted_mean(self):
        self._vote(2, 0.7)  # measured forecaster, weight 1 + 4·0.24
        self._vote(1, 0.6)  # newcomer, weight 1
        self._vote(3, 0.9)  # no resolved forecasts, weight 1

        skilled_weight = 1.0 + SKILL_WEIGHT_K * 0.24
        expected = (skilled_weight * 0.7 + 1.0 * 0.6 + 1.0 * 0.9) / (skilled_weight + 2.0)

        result = get_consensus(self.db, CONDITION)
        self.assertEqual(result["n_votes"], 3)
        self.assertEqual(result["n_contributors"], 3)
        self.assertAlmostEqual(result["consensus"], expected, places=4)
        self.assertEqual(len(result["contributors"]), 3)

    def test_consensus_reads_the_latest_vote_per_user(self):
        self._vote(1, 0.2, vote_seq=1)
        self._vote(1, 0.8, vote_seq=2)
        self._vote(4, 0.5)

        active = latest_votes(self.db, CONDITION)
        self.assertEqual(len(active), 2)
        self.assertEqual({vote.probability for vote in active}, {0.8, 0.5})

        result = get_consensus(self.db, CONDITION)
        self.assertEqual(result["n_votes"], 2)
        self.assertAlmostEqual(result["consensus"], 0.65, places=4)

    def test_private_votes_are_not_pooled(self):
        self._vote(1, 0.9, visibility=VISIBILITY_PUBLIC)
        self._vote(2, 0.2, condition_id=CONDITION, visibility=VISIBILITY_PRIVATE)

        result = get_consensus(self.db, CONDITION)
        self.assertEqual(result["n_votes"], 1)
        self.assertAlmostEqual(result["consensus"], 0.9, places=4)
        self.assertEqual([row["user_id"] for row in result["contributors"]], [1])

    def test_viewers_own_vote_is_excluded_from_their_own_view(self):
        self._vote(1, 0.9)
        self._vote(2, 0.3)

        result = get_consensus(self.db, CONDITION, viewer_user_id=1, exclude_self=True)
        self.assertTrue(result["excluded_self"])
        self.assertEqual(result["n_votes"], 1)
        self.assertAlmostEqual(result["consensus"], 0.3, places=4)

        included = get_consensus(self.db, CONDITION, viewer_user_id=1, exclude_self=False)
        self.assertFalse(included["excluded_self"])
        self.assertEqual(included["n_votes"], 2)

    def test_ai_leg_and_gap_come_from_the_latest_assessment(self):
        self._vote(1, 0.7)
        self.db.add(
            Assessment(
                condition_id=CONDITION,
                ai_score=50,
                reasoning="ai",
                recommendation="hold",
                risk_level="unknown",
                confidence=50,
                ai_probability=0.62,
                market_price_at_assessment=0.55,
                created_at=utc_now(),
            )
        )
        self.db.commit()

        result = get_consensus(self.db, CONDITION)
        self.assertAlmostEqual(result["ai_probability"], 0.62, places=4)
        self.assertAlmostEqual(result["market_price"], 0.55, places=4)
        # gap is consensus vs market price; ai_gap is consensus vs the AI.
        self.assertAlmostEqual(result["gap"], 0.7 - 0.55, places=4)
        self.assertAlmostEqual(result["ai_gap"], 0.7 - 0.62, places=4)
        self.assertEqual(result["advisory"], "informational")

    def test_consensus_is_cached_and_invalidated_by_a_vote(self):
        self._vote(1, 0.4)

        first = get_consensus(self.db, CONDITION)
        self.assertFalse(first["cached"])
        second = get_consensus(self.db, CONDITION)
        self.assertTrue(second["cached"])

        submit_vote(self.db, 2, CONDITION, 0.6, visibility=VISIBILITY_PUBLIC)
        third = get_consensus(self.db, CONDITION)
        self.assertFalse(third["cached"])
        self.assertEqual(third["n_votes"], 2)

    def test_cache_ttl_is_sixty_seconds(self):
        self.assertEqual(CONSENSUS_CACHE_TTL_SECONDS, 60)


class VoteGuardTest(ConsensusTestCase):
    """The write path's abuse controls."""

    def test_probability_must_be_strictly_inside_the_unit_interval(self):
        for bad in (0.0, 1.0, -0.2, 1.4, "abc", None):
            with self.subTest(bad=bad):
                with self.assertRaises(VoteRejected) as caught:
                    submit_vote(self.db, 1, CONDITION, bad)
                self.assertEqual(caught.exception.code, "invalid_probability")

    def test_one_vote_seq_per_revision(self):
        first = submit_vote(self.db, 1, CONDITION, 0.4, visibility=VISIBILITY_PUBLIC)
        second = submit_vote(self.db, 1, CONDITION, 0.5, visibility=VISIBILITY_PUBLIC)
        third = submit_vote(self.db, 1, CONDITION, 0.6, visibility=VISIBILITY_PUBLIC)
        self.assertEqual((first.vote_seq, second.vote_seq, third.vote_seq), (1, 2, 3))
        rows = (
            self.db.query(ForecastVote)
            .filter(ForecastVote.user_id == 1, ForecastVote.condition_id == CONDITION)
            .count()
        )
        self.assertEqual(rows, 3)
        # The active vote is the newest revision.
        self.assertEqual(latest_votes(self.db, CONDITION)[0].probability, 0.6)

    def test_votes_default_to_private(self):
        vote = submit_vote(self.db, 1, CONDITION, 0.5)
        self.assertEqual(vote.visibility, VISIBILITY_PRIVATE)

    def test_market_resolving_within_an_hour_is_closed(self):
        self.db.add(
            Market(
                id=CONDITION,
                question="closing",
                status="active",
                end_date=utc_now() + timedelta(minutes=30),
            )
        )
        self.db.commit()
        with self.assertRaises(VoteRejected) as caught:
            submit_vote(self.db, 1, CONDITION, 0.5)
        self.assertEqual(caught.exception.code, "market_resolving")

    def test_open_market_is_accepted(self):
        self.db.add(
            Market(
                id=CONDITION,
                question="open",
                status="active",
                end_date=utc_now() + timedelta(days=2),
            )
        )
        self.db.commit()
        vote = submit_vote(self.db, 1, CONDITION, 0.5)
        self.assertEqual(vote.probability, 0.5)

    def test_closed_market_is_refused(self):
        self.db.add(Market(id=CONDITION, question="closed", status="closed"))
        self.db.commit()
        with self.assertRaises(VoteRejected) as caught:
            submit_vote(self.db, 1, CONDITION, 0.5)
        self.assertEqual(caught.exception.code, "market_closed")

    def test_unknown_market_is_allowed(self):
        """The local market table is a cache: an absent row proves nothing."""
        vote = submit_vote(self.db, 1, CONDITION, 0.5)
        self.assertEqual(vote.condition_id, CONDITION)

    def test_vote_rate_limit_is_per_user(self):
        for index in range(MAX_VOTES_PER_HOUR):
            submit_vote(self.db, 1, f"0xmarket-{index}", 0.5)
        with self.assertRaises(VoteRejected) as caught:
            submit_vote(self.db, 1, "0xmarket-one-too-many", 0.5)
        self.assertEqual(caught.exception.code, "rate_limited")
        # A different user is unaffected by the first user's budget.
        submit_vote(self.db, 2, "0xmarket-one-too-many", 0.5)


class AnnotationPrivacyTest(ConsensusTestCase):
    """A private note never reaches another user's response."""

    def test_private_annotation_stays_private(self):
        save_annotation(self.db, 1, CONDITION, body="my secret thesis", probability=0.7)
        save_annotation(self.db, 2, CONDITION, body="public thesis", visibility=VISIBILITY_PUBLIC)

        from app.services.intelligence.consensus_service import list_annotations

        rows = list_annotations(self.db, CONDITION, 1)
        self.assertEqual([row.user_id for row in rows], [1, 2])
        self.assertEqual([row.body for row in rows], ["my secret thesis", "public thesis"])

        other = list_annotations(self.db, CONDITION, 3)
        self.assertEqual([row.user_id for row in other], [2])
        self.assertNotIn("secret", " ".join(row.body for row in other))

    def test_annotation_is_one_editable_row_per_user_and_market(self):
        first = save_annotation(self.db, 1, CONDITION, body="v1")
        second = save_annotation(self.db, 1, CONDITION, body="v2", probability=0.4)
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.body, "v2")
        self.assertEqual(second.probability, 0.4)
        rows = self.db.query(MarketAnnotation).filter(MarketAnnotation.user_id == 1).count()
        self.assertEqual(rows, 1)

    def test_empty_body_and_out_of_range_probability_are_refused(self):
        with self.assertRaises(VoteRejected) as caught:
            save_annotation(self.db, 1, CONDITION, body="   ")
        self.assertEqual(caught.exception.code, "empty_body")
        with self.assertRaises(VoteRejected) as caught:
            save_annotation(self.db, 1, CONDITION, body="thesis", probability=1.0)
        self.assertEqual(caught.exception.code, "invalid_probability")

    def test_private_vote_never_pooled_for_another_user(self):
        save_annotation(self.db, 1, CONDITION, body="private", probability=0.95)
        result = get_consensus(self.db, CONDITION)
        self.assertEqual(result["n_votes"], 0)
        self.assertIsNone(result["consensus"])
        # An annotation alone is not a vote: it never enters the ensemble.
        self.assertEqual(get_consensus(self.db, CONDITION)["contributors"], [])


class FollowServiceTest(ConsensusTestCase):
    """User→user follows, distinct from the external-wallet follows."""

    def test_follow_unfollow_and_listing(self):
        follow_user(self.db, 1, 2)
        follow_user(self.db, 1, 3)
        self.assertEqual([row["user_id"] for row in list_following(self.db, 1)], [2, 3])
        self.assertTrue(unfollow_user(self.db, 1, 2))
        self.assertEqual([row["user_id"] for row in list_following(self.db, 1)], [3])

    def test_follow_is_idempotent(self):
        first = follow_user(self.db, 1, 2)
        second = follow_user(self.db, 1, 2)
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.db.query(UserFollow).count(), 1)

    def test_self_follow_is_refused(self):
        with self.assertRaises(FollowRejected) as caught:
            follow_user(self.db, 1, 1)
        self.assertEqual(caught.exception.code, "self_follow")

    def test_unknown_followee_is_refused(self):
        with self.assertRaises(FollowRejected) as caught:
            follow_user(self.db, 1, 9999)
        self.assertEqual(caught.exception.code, "unknown_user")

    def test_following_feed_carries_public_votes_only(self):
        follow_user(self.db, 1, 2)
        follow_user(self.db, 1, 3)
        self._vote(2, 0.8)
        self._vote(3, 0.3, visibility=VISIBILITY_PRIVATE)

        feed = following_forecasts(self.db, 1)
        on_condition = [row for row in feed if row["condition_id"] == CONDITION]
        self.assertEqual(len(on_condition), 1)
        self.assertEqual(on_condition[0]["forecaster"]["user_id"], 2)
        self.assertEqual(on_condition[0]["forecaster"]["resolved"], MIN_RESOLVED_FOR_SKILL)
        # User 3's private vote is not in the feed, and neither is user 3.
        self.assertNotIn(3, {row["forecaster"]["user_id"] for row in feed})


class LeaderboardOrderingTest(ConsensusTestCase):
    """The board ranks measured skill first, and hides private participants."""

    def test_board_is_sorted_by_skill_then_resolved(self):
        # User 2: 3 resolved at 0.01 Brier → skill 0.24.
        self._vote(2, 0.9)
        # User 1: 3 resolved at 0.09 Brier → skill 0.16.
        for condition in (f"0xmid-{index}" for index in range(MIN_RESOLVED_FOR_SKILL)):
            self.db.add(
                ForecastVote(
                    user_id=1,
                    condition_id=condition,
                    probability=0.7,
                    vote_seq=1,
                    visibility=VISIBILITY_PUBLIC,
                    created_at=utc_now(),
                )
            )
            self.db.add(_assessment(condition, "YES", probability=0.7))
        self.db.commit()
        self._vote(1, 0.6)
        # User 4 votes publicly but has no resolved forecasts: weight 1,
        # skill 0 — still on the public board.
        self._vote(4, 0.5)

        board = forecaster_leaderboard(self.db)
        self.assertEqual(board["scope"], "user")
        self.assertEqual([row["user_id"] for row in board["forecasters"]], [2, 1, 4])
        self.assertAlmostEqual(board["forecasters"][0]["skill"], 0.24, places=4)
        self.assertAlmostEqual(board["forecasters"][1]["skill"], 0.16, places=4)
        self.assertEqual(board["forecasters"][2]["skill"], 0.0)
        self.assertEqual(board["forecasters"][2]["weight"], 1.0)
        self.assertEqual(board["forecasters"][2]["public_votes"], 1)

    def test_public_board_excludes_private_only_forecasters(self):
        self._vote(3, 0.9, visibility=VISIBILITY_PRIVATE)

        public_ids = [row["user_id"] for row in forecaster_leaderboard(self.db)["forecasters"]]
        self.assertIn(2, public_ids)
        self.assertNotIn(3, public_ids)

        global_board = forecaster_leaderboard(self.db, scope="global")
        global_ids = [row["user_id"] for row in global_board["forecasters"]]
        self.assertIn(3, global_ids)
        row = next(row for row in global_board["forecasters"] if row["user_id"] == 3)
        self.assertEqual(row["public_votes"], 0)
        self.assertEqual(row["total_votes"], 1)


class LegacyResolutionTest(ConsensusTestCase):
    """A legacy non-binary label must never break a read path.

    The legacy resolver stored the raw Gamma label ("ALICE") instead of
    a binary YES/NO. Such a row cannot be scored, so the read paths
    skip it — they must not raise, and they must not map it onto a
    binary outcome.
    """

    def _legacy_assessment(self, condition_id: str) -> Assessment:
        row = Assessment(
            condition_id=condition_id,
            ai_score=50,
            reasoning="legacy",
            recommendation="hold",
            risk_level="unknown",
            confidence=50,
            ai_probability=0.9,
            resolved_outcome="ALICE",
            brier_score=0.01,
            task="opportunity_scan",
            created_at=utc_now(),
        )
        self.db.add(row)
        self.db.commit()
        return row

    def test_resolved_condition_rows_skip_a_legacy_label(self):
        condition = "0xlegacy-1"
        self._legacy_assessment(condition)
        rows = AssessmentRepository(self.db).resolved_condition_rows([condition])
        self.assertEqual(rows, [])

    def test_forecaster_skill_skips_a_legacy_label(self):
        condition = "0xlegacy-1"
        self._legacy_assessment(condition)
        self._vote(1, 0.9, condition_id=condition)

        skill = forecaster_skill(self.db, 1)
        self.assertEqual(skill["resolved"], 0)
        self.assertIsNone(skill["mean_brier"])
        self.assertEqual(skill["skill"], 0.0)

    def test_forecaster_leaderboard_skips_a_legacy_label(self):
        condition = "0xlegacy-1"
        self._legacy_assessment(condition)
        self._vote(1, 0.9, condition_id=condition)

        board = forecaster_leaderboard(self.db)
        row = next(row for row in board["forecasters"] if row["user_id"] == 1)
        self.assertEqual(row["resolved"], 0)
        self.assertIsNone(row["mean_brier"])

    def test_consensus_read_does_not_raise_on_a_legacy_label(self):
        condition = "0xlegacy-1"
        self._legacy_assessment(condition)
        self._vote(1, 0.9, condition_id=condition)

        result = get_consensus(self.db, condition)
        self.assertEqual(result["n_votes"], 1)


class ConsensusNarrativeTest(ConsensusTestCase):
    """The optional/degradable narration renders without a live model."""

    def test_prompt_template_is_rendered_when_present(self):
        from app.services.intelligence.consensus_service import render_consensus_prompt

        rendered = render_consensus_prompt(
            CONDITION,
            ai_probability=0.62,
            consensus=0.71,
            market_price=0.55,
            gap=0.16,
            n_votes=3,
            disagreement=0.2,
        )
        self.assertIn(CONDITION, rendered)
        self.assertIn("0.6200", rendered)
        self.assertNotIn("{", rendered)

    def test_narration_degrades_when_the_template_is_missing(self):
        from unittest.mock import patch

        from app.services.intelligence.consensus_service import render_consensus_prompt

        with patch(
            "app.services.intelligence.consensus_service._consensus_summary_template",
            return_value="",
        ):
            rendered = render_consensus_prompt(CONDITION, n_votes=0)
        self.assertIn(CONDITION, rendered)


# ── route tests ─────────────────────────────────────────────────


def _make_app_and_client(is_admin: bool = False, user_id: int = 1):
    engine, session_factory = _make_db()
    db = session_factory()
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.dependency_overrides[get_current_user_from_token] = lambda: {
        "user_id": user_id,
        "wallet_address": WALLET_A,
        "is_admin": is_admin,
    }
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_active_account] = lambda: None
    test_app.dependency_overrides[_collaboration_read_rate_limit] = lambda: None
    test_app.dependency_overrides[_collaboration_write_rate_limit] = lambda: None
    return test_app, TestClient(test_app), db, engine


class CollaborationRouteTest(unittest.TestCase):
    """Every route authenticates, and the documented scopes hold."""

    def setUp(self):
        self.app, self.client, self.db, self.engine = _make_app_and_client()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.addCleanup(reset_consensus_cache)
        for user_id, wallet in enumerate(
            (WALLET_A, WALLET_B, WALLET_C, WALLET_D),
            start=1,
        ):
            self.db.add(User(id=user_id, wallet_address=wallet))
        self.db.commit()

    def test_unauthenticated_callers_are_rejected_everywhere(self):
        # setUp installs the overrides a signed-in caller would need; a
        # bare client must still be refused by every handler's own
        # ``get_current_user_from_token`` dependency.
        self.app.dependency_overrides.clear()
        routes = [
            ("PUT", f"/api/collaboration/votes/{CONDITION}", {"probability": 0.5}),
            ("GET", f"/api/collaboration/consensus/{CONDITION}", None),
            ("GET", f"/api/collaboration/consensus/batch?condition_ids={CONDITION}", None),
            ("GET", f"/api/collaboration/annotations/{CONDITION}", None),
            ("PUT", f"/api/collaboration/annotations/{CONDITION}", {"body": "note"}),
            ("DELETE", f"/api/collaboration/annotations/{CONDITION}", None),
            ("POST", "/api/collaboration/follow/2", None),
            ("DELETE", "/api/collaboration/follow/2", None),
            ("GET", "/api/collaboration/forecasters", None),
            ("GET", "/api/collaboration/leaderboard", None),
        ]
        for method, path, payload in routes:
            with self.subTest(path=path):
                response = self.client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 401, msg=response.text)

    def test_vote_then_consensus_round_trip(self):
        response = self.client.put(
            f"/api/collaboration/votes/{CONDITION}",
            json={"probability": 0.62},
        )
        self.assertEqual(response.status_code, 200, msg=response.text)
        body = response.json()
        self.assertEqual(body["vote_seq"], 1)
        self.assertEqual(body["visibility"], VISIBILITY_PRIVATE)
        self.assertEqual(body["advisory"], "informational")

        # A private vote is not pooled, so consensus stays empty.
        consensus = self.client.get(f"/api/collaboration/consensus/{CONDITION}").json()
        self.assertEqual(consensus["n_votes"], 0)
        self.assertIsNone(consensus["consensus"])
        self.assertIn("narrative", consensus)

        published = self.client.put(
            f"/api/collaboration/votes/{CONDITION}",
            json={"probability": 0.62, "visibility": VISIBILITY_PUBLIC},
        )
        self.assertEqual(published.status_code, 200, msg=published.text)
        self.assertEqual(published.json()["vote_seq"], 2)

        # The caller's own view excludes their own vote: a portfolio view
        # that echoes only the reader's opinion is confirmation.
        own = self.client.get(f"/api/collaboration/consensus/{CONDITION}").json()
        self.assertTrue(own["excluded_self"])
        self.assertEqual(own["n_votes"], 0)

        pooled = self.client.get(
            f"/api/collaboration/consensus/{CONDITION}?exclude_self=false"
        ).json()
        self.assertFalse(pooled["excluded_self"])
        self.assertEqual(pooled["n_votes"], 1)
        self.assertAlmostEqual(pooled["consensus"], 0.62, places=4)
        self.assertEqual(pooled["contributors"][0]["probability"], 0.62)
        self.assertEqual(pooled["advisory"], "informational")

    def test_vote_rejections_map_to_statuses(self):
        response = self.client.put(
            f"/api/collaboration/votes/{CONDITION}", json={"probability": 1.0}
        )
        self.assertEqual(response.status_code, 422, msg=response.text)
        self.assertIn("probability", response.text)

    def test_consensus_batch(self):
        self.client.put(
            f"/api/collaboration/votes/{CONDITION}",
            json={"probability": 0.4, "visibility": VISIBILITY_PUBLIC},
        )
        response = self.client.get(
            f"/api/collaboration/consensus/batch"
            f"?condition_ids={CONDITION},0xother&exclude_self=false"
        )
        self.assertEqual(response.status_code, 200, msg=response.text)
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertAlmostEqual(body["consensus"][CONDITION]["consensus"], 0.4, places=4)
        self.assertIsNone(body["consensus"]["0xother"]["consensus"])

    def test_annotations_are_scoped_to_owner_plus_public(self):
        self.client.put(f"/api/collaboration/annotations/{CONDITION}", json={"body": "mine"})
        # Another user's private note is invisible to the caller.
        self.db.add(
            MarketAnnotation(
                user_id=2,
                condition_id=CONDITION,
                body="theirs (private)",
                visibility=VISIBILITY_PRIVATE,
            )
        )
        self.db.add(
            MarketAnnotation(
                user_id=3,
                condition_id=CONDITION,
                body="theirs (public)",
                visibility=VISIBILITY_PUBLIC,
            )
        )
        self.db.commit()

        rows = self.client.get(f"/api/collaboration/annotations/{CONDITION}").json()
        self.assertEqual(rows["count"], 2)
        bodies = [row["body"] for row in rows["annotations"]]
        self.assertIn("mine", bodies)
        self.assertIn("theirs (public)", bodies)
        self.assertNotIn("theirs (private)", bodies)

    def test_annotation_can_be_withdrawn_only_by_its_owner(self):
        self.client.put(f"/api/collaboration/annotations/{CONDITION}", json={"body": "mine"})
        self.assertEqual(
            self.client.delete(f"/api/collaboration/annotations/{CONDITION}").json(),
            {"deleted": True},
        )
        missing = self.client.delete(f"/api/collaboration/annotations/{CONDITION}")
        self.assertEqual(missing.status_code, 404)

    def test_follow_and_forecaster_feed(self):
        self.assertEqual(self.client.post("/api/collaboration/follow/2").status_code, 200)
        submit_vote(self.db, 2, CONDITION, 0.8, visibility=VISIBILITY_PUBLIC)

        feed = self.client.get("/api/collaboration/forecasters").json()
        self.assertEqual(feed["count"], 1)
        self.assertEqual(feed["following"][0]["user_id"], 2)
        self.assertEqual(len(feed["forecasts"]), 1)
        self.assertEqual(
            self.client.delete("/api/collaboration/follow/2").json(),
            {"unfollowed": True},
        )
        self.assertEqual(
            self.client.delete("/api/collaboration/follow/2").status_code,
            404,
        )

    def test_self_follow_is_a_validation_error(self):
        response = self.client.post("/api/collaboration/follow/1")
        self.assertEqual(response.status_code, 422, msg=response.text)

    def test_leaderboard_orders_by_skill(self):
        self.db.add(
            ForecastVote(
                user_id=2,
                condition_id=CONDITION,
                probability=0.9,
                vote_seq=1,
                visibility=VISIBILITY_PUBLIC,
                created_at=utc_now(),
            )
        )
        for condition in RESOLVED_CONDITIONS:
            self.db.add(
                ForecastVote(
                    user_id=2,
                    condition_id=condition,
                    probability=0.9,
                    vote_seq=1,
                    visibility=VISIBILITY_PUBLIC,
                    created_at=utc_now(),
                )
            )
            self.db.add(_assessment(condition, "YES"))
        self.db.add(
            ForecastVote(
                user_id=3,
                condition_id=CONDITION,
                probability=0.5,
                vote_seq=1,
                visibility=VISIBILITY_PUBLIC,
                created_at=utc_now(),
            )
        )
        self.db.commit()

        board = self.client.get("/api/collaboration/leaderboard").json()
        self.assertEqual(board["scope"], "user")
        self.assertEqual([row["user_id"] for row in board["forecasters"]], [2, 3])
        self.assertAlmostEqual(board["forecasters"][0]["skill"], 0.24, places=4)
        self.assertEqual(board["forecasters"][0]["resolved"], 3)

    def test_leaderboard_global_scope_requires_an_admin(self):
        response = self.client.get("/api/collaboration/leaderboard?scope=global")
        self.assertEqual(response.status_code, 403, msg=response.text)

        admin_app, admin_client, admin_db, admin_engine = _make_app_and_client(is_admin=True)
        self.addCleanup(admin_engine.dispose)
        self.addCleanup(admin_db.close)
        admin = admin_client.get("/api/collaboration/leaderboard?scope=global")
        self.assertEqual(admin.status_code, 200, msg=admin.text)
        self.assertEqual(admin.json()["scope"], "global")

    def test_vote_is_attributed_to_the_active_account(self):
        """The account seam stamps account_id while accounts stay optional."""
        from app.services.users.account_service import ensure_personal_account

        account = ensure_personal_account(self.db, 1)
        self.app.dependency_overrides.pop(get_active_account, None)

        response = self.client.put(
            f"/api/collaboration/votes/{CONDITION}",
            json={"probability": 0.5},
        )
        self.assertEqual(response.status_code, 200, msg=response.text)
        vote = self.db.query(ForecastVote).first()
        self.assertEqual(vote.account_id, account.id)


if __name__ == "__main__":
    unittest.main()
