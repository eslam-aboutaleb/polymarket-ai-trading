"""Tests for app/services/forecast_resolution.py (plan 01).

Gamma lookups are mocked at the ``httpx.AsyncClient`` boundary
(patch-constructor convention from
test_binance_signals_routes_coverage.py); resolution and Brier writes
run against real in-memory SQLite.

The golden fixtures below cover the settlement states issue #29 asks
for: closed-not-resolved, multi-outcome, disputed, void, changed rules,
a reversed outcome mapping and a replayed (stream-deduplicated) verdict.
"""

import asyncio
import contextlib
import json
import math
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base)
from app.models.analytics.assessment import Assessment
from app.models.base import Base
from app.services.intelligence import forecast_resolution
from app.services.intelligence.forecast_resolution import (
    SCORING_VERSION,
    ExclusionReason,
    MarketResolution,
    ResolutionRun,
    brier_score,
    fetch_resolutions,
    is_binary_outcome,
    is_verified_resolution,
    log_loss,
    outcome_value,
    resolve_pending,
    winning_outcome,
)
from app.utils.time import utc_now

CID = "0x" + "ab" * 32
CID_2 = "0x" + "cd" * 32


def _gamma_market(cid, outcomes, prices, **extra):
    market = {
        "conditionId": cid,
        "question": "Will it happen?",
        "outcomes": json.dumps(outcomes),
        "outcomePrices": json.dumps(prices),
        "closed": True,
        "umaResolutionStatus": "resolved",
    }
    market.update(extra)
    return market


def _resolved(outcome: str, closed_at: datetime | None = None) -> MarketResolution:
    """A settled market, as ``fetch_resolutions`` reports it."""
    return MarketResolution(outcome=outcome, closed_at=closed_at)


def _mock_httpx_client(responses=None, status_code=200, get_exc=None):
    """Build an ``httpx.AsyncClient`` double.

    ``responses`` is either a list of payloads consumed one per GET
    call, or a single payload reused for every call. Each payload is
    what ``response.json()`` returns (the Gamma markets array).
    """
    calls: list[tuple[str, dict]] = []
    queue = list(responses) if isinstance(responses, list) else None
    fixed = responses if queue is None else None

    async def _get(url, params=None, **kwargs):
        calls.append((url, params or {}))
        if get_exc is not None:
            raise get_exc
        response = MagicMock()
        response.status_code = status_code
        payload = queue.pop(0) if queue else fixed
        response.json = MagicMock(return_value=payload if payload is not None else [])
        return response

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=_get)
    client.calls = calls
    return client


def _make_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _verdict(db, cid=CID, probability=0.7, price=0.55, model="gpt-4", resolved=False, **extra):
    row = Assessment(
        trade_history_id=None,
        condition_id=cid,
        task="opportunity_scan",
        ai_score=70,
        reasoning="r",
        recommendation="buy",
        risk_level="unknown",
        confidence=80,
        ai_probability=probability,
        market_price_at_assessment=price,
        model_name=model,
        provider="openai",
        **extra,
    )
    if resolved:
        row.resolved_outcome = "YES"
        row.brier_score = brier_score(probability, "YES")
        row.resolved_at = utc_now()
    db.add(row)
    db.commit()
    return row


class WinningOutcomeTests(unittest.TestCase):
    def test_yes_wins(self):
        market = _gamma_market(CID, ["Yes", "No"], ["1", "0"])
        self.assertEqual(winning_outcome(market), "YES")

    def test_no_wins(self):
        market = _gamma_market(CID, ["Yes", "No"], ["0", "1"])
        self.assertEqual(winning_outcome(market), "NO")

    def test_reversed_outcome_mapping_resolves_to_the_winning_label(self):
        """Gamma may list the outcomes the other way round.

        ``["No", "Yes"]`` with the prices ``["1", "0"]`` means NO won:
        the label must follow the price, not the position.
        """
        market = _gamma_market(CID, ["No", "Yes"], ["1", "0"])
        self.assertEqual(winning_outcome(market), "NO")

    def test_reversed_outcome_mapping_resolves_to_the_other_winner(self):
        market = _gamma_market(CID, ["No", "Yes"], ["0", "1"])
        self.assertEqual(winning_outcome(market), "YES")

    def test_multi_outcome_market_is_not_scored_as_binary_no(self):
        market = _gamma_market(CID, ["Alice", "Bob"], ["0", "1"])
        self.assertIsNone(winning_outcome(market))

    def test_no_outcome_reaching_threshold_returns_none(self):
        market = _gamma_market(CID, ["Yes", "No"], ["0", "0"])
        self.assertIsNone(winning_outcome(market))

    def test_malformed_payloads_return_none(self):
        self.assertIsNone(winning_outcome({}))
        self.assertIsNone(winning_outcome({"outcomes": "not json", "outcomePrices": "[]"}))
        self.assertIsNone(winning_outcome({"outcomes": '["Yes"]', "outcomePrices": '["1", "0"]'}))
        self.assertIsNone(
            winning_outcome({"outcomes": '["Yes", "No"]', "outcomePrices": '["x", "y"]'}),
        )

    def test_already_parsed_lists_are_accepted(self):
        market = _gamma_market(CID, ["Yes", "No"], [1.0, 0.0])
        market["outcomes"] = ["Yes", "No"]
        market["outcomePrices"] = [1.0, 0.0]
        self.assertEqual(winning_outcome(market), "YES")

    def test_closed_without_verified_resolution_does_not_count(self):
        market = _gamma_market(CID, ["Yes", "No"], ["1", "0"])
        market["umaResolutionStatus"] = "proposed"
        self.assertIsNone(winning_outcome(market))

    def test_ambiguous_equal_outcome_values_does_not_count(self):
        market = _gamma_market(CID, ["Yes", "No"], ["0.5", "0.5"])
        self.assertIsNone(winning_outcome(market))

    def test_99_percent_quote_is_not_final_resolution(self):
        market = _gamma_market(CID, ["Yes", "No"], ["0.99", "0.01"])
        self.assertIsNone(winning_outcome(market))


class ExclusionReasonTests(unittest.TestCase):
    """Every unusable settlement state has its own reason."""

    def _reason(self, market) -> ExclusionReason:
        return forecast_resolution.classify_market(market).reason

    def test_disputed_market_is_reported_as_disputed(self):
        """Gamma emits "disputed" for a challenged resolution."""
        market = _gamma_market(CID, ["Yes", "No"], ["1", "0"], umaResolutionStatus="disputed")
        self.assertEqual(self._reason(market), ExclusionReason.DISPUTED_OR_CHALLENGED)
        self.assertIsNone(winning_outcome(market))

    def test_challenged_market_is_reported_as_disputed(self):
        market = _gamma_market(CID, ["Yes", "No"], ["1", "0"], umaResolutionStatus="challenged")
        self.assertEqual(self._reason(market), ExclusionReason.DISPUTED_OR_CHALLENGED)

    def test_voided_market_is_reported_as_void(self):
        market = _gamma_market(CID, ["Yes", "No"], ["1", "0"], umaResolutionStatus="voided")
        self.assertEqual(self._reason(market), ExclusionReason.VOID)

    def test_closed_but_not_finalized_market_has_its_own_reason(self):
        market = _gamma_market(CID, ["Yes", "No"], ["1", "0"], umaResolutionStatus="proposed")
        self.assertEqual(self._reason(market), ExclusionReason.CLOSED_NOT_FINALIZED)

    def test_multi_outcome_market_has_its_own_reason(self):
        market = _gamma_market(CID, ["Alice", "Bob"], ["0", "1"])
        self.assertEqual(self._reason(market), ExclusionReason.NON_BINARY_OUTCOME)

    def test_three_outcome_market_has_its_own_reason(self):
        market = _gamma_market(CID, ["Yes", "No", "Maybe"], ["1", "0", "0"])
        self.assertEqual(self._reason(market), ExclusionReason.MULTI_OUTCOME)

    def test_ambiguous_settlement_has_its_own_reason(self):
        market = _gamma_market(CID, ["Yes", "No"], ["0", "0"])
        self.assertEqual(self._reason(market), ExclusionReason.AMBIGUOUS_SETTLEMENT)

    def test_changed_rules_produce_a_different_outcome(self):
        """The same market settled the other way round scores the other way.

        A market that resolves NO instead of YES must produce the
        mirrored Brier score — the label follows the settlement, never
        the previous answer.
        """
        yes_market = _gamma_market(CID, ["Yes", "No"], ["1", "0"])
        no_market = _gamma_market(CID, ["Yes", "No"], ["0", "1"])
        self.assertEqual(winning_outcome(yes_market), "YES")
        self.assertEqual(winning_outcome(no_market), "NO")
        self.assertAlmostEqual(brier_score(0.8, "YES"), 0.04)
        self.assertAlmostEqual(brier_score(0.8, "NO"), 0.64)


class BinaryOutcomeTests(unittest.TestCase):
    def test_is_binary_outcome_accepts_the_binary_labels(self):
        for label in ("YES", "yes", "NO", "no", "Yes", "No"):
            with self.subTest(label=label):
                self.assertTrue(is_binary_outcome(label))

    def test_is_binary_outcome_rejects_legacy_and_missing_labels(self):
        for label in ("ALICE", "BOB", "", None, "maybe"):
            with self.subTest(label=label):
                self.assertFalse(is_binary_outcome(label))

    def test_verified_resolution_needs_a_score_and_a_binary_label(self):
        self.assertTrue(is_verified_resolution(0.04, "YES"))
        self.assertFalse(is_verified_resolution(None, "YES"))
        self.assertFalse(is_verified_resolution(0.04, "ALICE"))


class LogLossTests(unittest.TestCase):
    def test_log_loss_is_finite_for_a_stated_certainty(self):
        """p = 0 and p = 1 are clipped for the computation only."""
        for probability in (0.0, 1.0):
            with self.subTest(probability=probability):
                value = log_loss(probability, "YES")
                self.assertTrue(math.isfinite(value))
                self.assertGreater(value, 0.0)

    def test_log_loss_penalises_a_wrong_certainty(self):
        self.assertGreater(log_loss(1.0, "NO"), log_loss(0.5, "NO"))

    def test_log_loss_rejects_an_unusable_probability(self):
        with self.assertRaises(ValueError):
            log_loss(1.5, "YES")
        with self.assertRaises(ValueError):
            log_loss(0.5, "ALICE")


class BrierScoreTests(unittest.TestCase):
    def test_yes_resolution(self):
        self.assertAlmostEqual(brier_score(0.8, "YES"), 0.04)

    def test_no_resolution(self):
        self.assertAlmostEqual(brier_score(0.8, "NO"), 0.64)

    def test_coin_flip_scores_baseline(self):
        self.assertAlmostEqual(brier_score(0.5, "NO"), 0.25)
        self.assertAlmostEqual(brier_score(0.5, "YES"), 0.25)

    def test_outcome_value_mapping(self):
        self.assertEqual(outcome_value("YES"), 1.0)
        self.assertEqual(outcome_value("yes"), 1.0)
        self.assertEqual(outcome_value("NO"), 0.0)
        with self.assertRaises(ValueError):
            outcome_value("ALICE")
        with self.assertRaises(ValueError):
            outcome_value(None)

    def test_brier_rejects_out_of_range_or_unverified(self):
        for p, label in [(float("nan"), "YES"), (1.2, "NO"), (0.8, "BOB")]:
            with self.subTest(p=p, label=label), self.assertRaises(ValueError):
                brier_score(p, label)


class FetchResolutionsTests(unittest.IsolatedAsyncioTestCase):
    def _patch_client(self, client):
        patcher = patch(
            "app.services.intelligence.forecast_resolution.httpx.AsyncClient",
            lambda **kwargs: client,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_empty_input_makes_no_request(self):
        client = _mock_httpx_client([])
        self._patch_client(client)
        self.assertEqual(await fetch_resolutions([]), {})
        self.assertEqual(await fetch_resolutions(None), {})
        client.get.assert_not_awaited()

    async def test_maps_winning_outcome_per_condition_id(self):
        yes_market = _gamma_market(CID, ["Yes", "No"], ["1", "0"])
        no_market = _gamma_market(CID_2, ["Yes", "No"], ["0", "1"])
        client = _mock_httpx_client([[yes_market], [no_market]])
        self._patch_client(client)
        result = await fetch_resolutions([CID, CID_2])
        self.assertEqual(
            {cid: item.outcome for cid, item in result.items()}, {CID: "YES", CID_2: "NO"}
        )

    async def test_wrong_condition_id_from_upstream_is_rejected(self):
        wrong = _gamma_market(CID_2, ["Yes", "No"], ["1", "0"])
        client = _mock_httpx_client([[wrong]])
        self._patch_client(client)
        result = await fetch_resolutions([CID])
        self.assertIsNone(result[CID].outcome)
        self.assertEqual(result[CID].reason, ExclusionReason.CONDITION_ID_MISMATCH)

    async def test_closed_but_not_finalized_is_excluded(self):
        market = _gamma_market(CID, ["Yes", "No"], ["1", "0"])
        market["umaResolutionStatus"] = "challenged"
        client = _mock_httpx_client([[market]])
        self._patch_client(client)
        result = await fetch_resolutions([CID])
        self.assertIsNone(result[CID].outcome)
        self.assertEqual(result[CID].reason, ExclusionReason.DISPUTED_OR_CHALLENGED)

    async def test_disputed_market_is_excluded(self):
        """Gamma reports a challenged resolution as "disputed"."""
        market = _gamma_market(CID, ["Yes", "No"], ["1", "0"])
        market["umaResolutionStatus"] = "disputed"
        client = _mock_httpx_client([[market]])
        self._patch_client(client)
        result = await fetch_resolutions([CID])
        self.assertIsNone(result[CID].outcome)
        self.assertEqual(result[CID].reason, ExclusionReason.DISPUTED_OR_CHALLENGED)

    async def test_voided_market_is_excluded(self):
        market = _gamma_market(CID, ["Yes", "No"], ["1", "0"])
        market["umaResolutionStatus"] = "voided"
        client = _mock_httpx_client([[market]])
        self._patch_client(client)
        result = await fetch_resolutions([CID])
        self.assertIsNone(result[CID].outcome)
        self.assertEqual(result[CID].reason, ExclusionReason.VOID)

    async def test_reversed_outcome_mapping_is_resolved(self):
        market = _gamma_market(CID, ["No", "Yes"], ["1", "0"])
        client = _mock_httpx_client([[market]])
        self._patch_client(client)
        result = await fetch_resolutions([CID])
        self.assertEqual(result[CID].outcome, "NO")

    async def test_market_close_time_is_reported(self):
        closed = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        market = _gamma_market(CID, ["Yes", "No"], ["1", "0"], closedTime=closed.timestamp())
        client = _mock_httpx_client([[market]])
        self._patch_client(client)
        result = await fetch_resolutions([CID])
        self.assertEqual(result[CID].closed_at, closed)

    async def test_uses_condition_ids_param_and_closed_filter(self):
        market = _gamma_market(CID, ["Yes", "No"], ["1", "0"])
        client = _mock_httpx_client([[market]])
        self._patch_client(client)
        await fetch_resolutions([CID])
        url, params = client.calls[0]
        self.assertTrue(url.endswith("/markets"))
        self.assertEqual(params, {"closed": "true", "condition_ids": CID})

    async def test_duplicate_ids_are_deduped(self):
        market = _gamma_market(CID, ["Yes", "No"], ["1", "0"])
        client = _mock_httpx_client([[market]])
        self._patch_client(client)
        result = await fetch_resolutions([CID, CID, CID])
        self.assertEqual({cid: item.outcome for cid, item in result.items()}, {CID: "YES"})
        self.assertEqual(client.get.await_count, 1)

    async def test_empty_result_is_skipped(self):
        client = _mock_httpx_client([[]])
        self._patch_client(client)
        result = await fetch_resolutions([CID])
        self.assertEqual(result[CID].reason, ExclusionReason.MARKET_NOT_FOUND)

    async def test_non_200_is_skipped(self):
        client = _mock_httpx_client([[]], status_code=500)
        self._patch_client(client)
        result = await fetch_resolutions([CID])
        self.assertEqual(result[CID].reason, ExclusionReason.LOOKUP_FAILED)

    async def test_network_error_is_skipped(self):
        client = _mock_httpx_client([None], get_exc=RuntimeError("network down"))
        self._patch_client(client)
        result = await fetch_resolutions([CID])
        self.assertEqual(result[CID].reason, ExclusionReason.LOOKUP_FAILED)

    async def test_lookups_are_bounded_per_batch(self):
        """A slow Gamma must not stretch the pass into a serial hour.

        20 pending ids are walked in concurrent chunks of at most
        ``_RESOLUTION_CONCURRENCY`` (8), which bounds the wall clock of
        one resolution pass instead of letting it run request by request.
        """
        ids = [f"0x{i:064x}" for i in range(20)]
        in_flight = 0
        peak = 0

        async def _get(url, params=None, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            response = MagicMock()
            response.status_code = 200
            response.json = MagicMock(return_value=[])
            return response

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(side_effect=_get)
        self._patch_client(client)

        result = await fetch_resolutions(ids)
        self.assertEqual(len(result), 20)
        self.assertEqual(client.get.await_count, 20)
        self.assertEqual(peak, forecast_resolution._RESOLUTION_CONCURRENCY)


class ResolvePendingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine, self.session_factory = _make_db()
        self.db = self.session_factory()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    async def test_scores_pending_rows_with_brier(self):
        row = _verdict(self.db, probability=0.8, price=0.55)
        with patch.object(
            forecast_resolution,
            "fetch_resolutions",
            AsyncMock(return_value={CID: _resolved("YES")}),
        ):
            run = await resolve_pending(self.db)
        self.assertIsInstance(run, ResolutionRun)
        self.assertEqual(run.checked, 1)
        self.assertEqual(run.resolved, 1)
        self.assertAlmostEqual(run.mean_brier, 0.04)
        self.db.refresh(row)
        self.assertEqual(row.resolved_outcome, "YES")
        self.assertAlmostEqual(row.brier_score, 0.04)
        self.assertIsNotNone(row.resolved_at)

    async def test_scored_row_carries_the_scoring_version(self):
        """A change of scoring rule must be visible on the row."""
        row = _verdict(self.db, probability=0.8)
        with patch.object(
            forecast_resolution,
            "fetch_resolutions",
            AsyncMock(return_value={CID: _resolved("YES")}),
        ):
            await resolve_pending(self.db)
        self.db.refresh(row)
        self.assertEqual(row.scoring_version, SCORING_VERSION)
        self.assertIsNone(row.exclusion_reason)

    async def test_invalid_outcome_leaves_forecast_unscored(self):
        row = _verdict(self.db, probability=0.8)
        with patch.object(
            forecast_resolution,
            "fetch_resolutions",
            AsyncMock(
                return_value={CID: MarketResolution(reason=ExclusionReason.NON_BINARY_OUTCOME)}
            ),
        ):
            run = await resolve_pending(self.db)
        self.assertEqual(run.resolved, 0)
        self.db.refresh(row)
        self.assertIsNone(row.brier_score)
        self.assertIsNone(row.resolved_at)
        self.assertIsNone(row.resolved_outcome)
        self.assertEqual(row.exclusion_reason, ExclusionReason.NON_BINARY_OUTCOME.value)

    async def test_exclusion_reasons_are_counted_per_reason(self):
        """The distinct causes must not collapse into one bucket."""
        _verdict(self.db, probability=0.8)
        _verdict(self.db, cid=CID_2, probability=0.6)
        with patch.object(
            forecast_resolution,
            "fetch_resolutions",
            AsyncMock(
                return_value={
                    CID: MarketResolution(reason=ExclusionReason.DISPUTED_OR_CHALLENGED),
                    CID_2: MarketResolution(reason=ExclusionReason.VOID),
                }
            ),
        ):
            run = await resolve_pending(self.db)
        self.assertEqual(run.resolved, 0)
        self.assertEqual(
            run.exclusions,
            {
                ExclusionReason.DISPUTED_OR_CHALLENGED.value: 1,
                ExclusionReason.VOID.value: 1,
            },
        )
        self.assertEqual(run.to_dict()["excluded"], 2)
        self.assertEqual(run.to_dict()["unresolved"], 2)

    async def test_forecast_recorded_after_the_close_is_not_scored(self):
        """A forecast made after the market closed already knew the answer."""
        closed = utc_now() - timedelta(hours=1)
        row = _verdict(self.db, probability=0.8)
        row.created_at = closed + timedelta(hours=1)
        self.db.commit()
        with patch.object(
            forecast_resolution,
            "fetch_resolutions",
            AsyncMock(return_value={CID: _resolved("YES", closed_at=closed)}),
        ):
            run = await resolve_pending(self.db)
        self.assertEqual(run.resolved, 0)
        self.assertEqual(run.exclusions, {ExclusionReason.FORECAST_AFTER_CLOSE.value: 1})
        self.db.refresh(row)
        self.assertIsNone(row.brier_score)
        self.assertEqual(row.exclusion_reason, ExclusionReason.FORECAST_AFTER_CLOSE.value)

    async def test_forecast_recorded_before_the_close_is_scored(self):
        closed = utc_now() - timedelta(hours=1)
        row = _verdict(self.db, probability=0.8)
        row.created_at = closed - timedelta(hours=2)
        self.db.commit()
        with patch.object(
            forecast_resolution,
            "fetch_resolutions",
            AsyncMock(return_value={CID: _resolved("YES", closed_at=closed)}),
        ):
            run = await resolve_pending(self.db)
        self.assertEqual(run.resolved, 1)
        self.db.refresh(row)
        self.assertIsNone(row.exclusion_reason)

    async def test_two_rows_for_one_market_count_as_one_effective_sample(self):
        """A replayed verdict must not inflate the effective sample count."""
        _verdict(self.db, probability=0.8, price=0.55)
        _verdict(self.db, probability=0.4, price=0.60, model="claude")
        with patch.object(
            forecast_resolution,
            "fetch_resolutions",
            AsyncMock(return_value={CID: _resolved("YES")}),
        ):
            run = await resolve_pending(self.db)
        self.assertEqual(run.resolved, 2)
        self.assertEqual(run.effective_resolved, 1)
        self.assertAlmostEqual(run.effective_mean_brier, 0.04)

    async def test_no_resolution_leaves_row_untouched(self):
        row = _verdict(self.db)
        with patch.object(forecast_resolution, "fetch_resolutions", AsyncMock(return_value={})):
            run = await resolve_pending(self.db)
        self.assertEqual(run.resolved, 0)
        self.assertEqual(run.unresolved_condition_ids, [CID])
        self.db.refresh(row)
        self.assertIsNone(row.resolved_outcome)
        self.assertIsNone(row.brier_score)
        self.assertIsNone(row.resolved_at)

    async def test_second_run_is_a_noop(self):
        _verdict(self.db, probability=0.8)
        with patch.object(
            forecast_resolution,
            "fetch_resolutions",
            AsyncMock(return_value={CID: _resolved("YES")}),
        ):
            first = await resolve_pending(self.db)
            second = await resolve_pending(self.db)
        self.assertEqual(first.resolved, 1)
        self.assertEqual(second.checked, 0)
        self.assertEqual(second.resolved, 0)

    async def test_rows_without_probability_or_condition_id_are_skipped(self):
        legacy = _verdict(self.db)
        legacy.ai_probability = None
        self.db.commit()
        no_cid = _verdict(self.db, cid=CID_2)
        no_cid.condition_id = None
        self.db.commit()
        fetch = AsyncMock(return_value={})
        with patch.object(forecast_resolution, "fetch_resolutions", fetch):
            run = await resolve_pending(self.db)
        self.assertEqual(run.checked, 0)
        fetch.assert_not_awaited()

    async def test_limit_caps_the_batch(self):
        for i in range(3):
            _verdict(self.db, cid=f"0x{i:064x}")
        fetch = AsyncMock(return_value={})
        with patch.object(forecast_resolution, "fetch_resolutions", fetch):
            run = await resolve_pending(self.db, limit=2)
        self.assertEqual(run.checked, 2)
        self.assertEqual(len(fetch.await_args.args[0]), 2)

    async def test_non_hex_condition_ids_never_enter_the_batch(self):
        """Market-id fallbacks can never resolve, so they must not queue.

        A verdict whose ``condition_id`` is the market id (markets that
        never exposed one) is unresolvable: leaving it in the oldest 200
        would block every newer verdict behind one doomed HTTP lookup.
        """
        _verdict(self.db, cid="market-123")
        _verdict(self.db, cid="market-456")
        _verdict(self.db, cid=CID)
        fetch = AsyncMock(return_value={})
        with patch.object(forecast_resolution, "fetch_resolutions", fetch):
            run = await resolve_pending(self.db)
        self.assertEqual(run.checked, 1)
        self.assertEqual(fetch.await_args.args[0], [CID])

    async def test_no_pending_rows_skips_fetch(self):
        fetch = AsyncMock(return_value={})
        with patch.object(forecast_resolution, "fetch_resolutions", fetch):
            run = await resolve_pending(self.db)
        self.assertEqual(run.checked, 0)
        fetch.assert_not_awaited()


class LoopLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.lock = patch.object(forecast_resolution, "acquire_scheduler_lock")
        self.release = patch.object(forecast_resolution, "release_scheduler_lock")
        self.heartbeat = patch.object(forecast_resolution, "scheduler_heartbeat")
        self.session_local = patch.object(forecast_resolution, "SessionLocal")
        self.mock_lock = self.lock.start()
        self.mock_release = self.release.start()
        self.mock_heartbeat = self.heartbeat.start()
        self.mock_session_local = self.session_local.start()
        for patcher in (self.lock, self.release, self.heartbeat, self.session_local):
            self.addCleanup(patcher.stop)
        self.mock_lock.return_value = True

        engine, session_factory = _make_db()
        self.addCleanup(engine.dispose)
        self.db = session_factory()
        self.addCleanup(self.db.close)
        self.mock_session_local.return_value = self.db

    async def test_loop_runs_one_pass_then_stops_on_cancel(self):
        resolve = AsyncMock(return_value=ResolutionRun(checked=2, resolved=1, brier_sum=0.04))
        with (
            patch.object(forecast_resolution, "resolve_pending", resolve),
            patch.object(forecast_resolution, "_resolution_interval", lambda: 3600),
        ):
            task = asyncio.create_task(forecast_resolution._resolution_loop())
            for _ in range(100):
                if resolve.await_count:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.mock_lock.assert_called_once_with("forecast_resolution")
        self.assertEqual(resolve.await_count, 1)
        self.mock_heartbeat.assert_called_with("forecast_resolution")
        self.mock_release.assert_called_once_with("forecast_resolution")

    async def test_loop_survives_a_failing_pass(self):
        resolve = AsyncMock(side_effect=RuntimeError("db down"))
        with (
            patch.object(forecast_resolution, "resolve_pending", resolve),
            patch.object(forecast_resolution, "_resolution_interval", lambda: 3600),
        ):
            task = asyncio.create_task(forecast_resolution._resolution_loop())
            for _ in range(100):
                if resolve.await_count:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.assertEqual(resolve.await_count, 1)
        self.mock_release.assert_called_once_with("forecast_resolution")

    async def test_start_skips_work_when_lock_held(self):
        self.mock_lock.return_value = False
        resolve = AsyncMock(return_value=ResolutionRun())
        with patch.object(forecast_resolution, "resolve_pending", resolve):
            await forecast_resolution.start_forecast_resolution()
            for _ in range(100):
                if forecast_resolution._resolution_task is not None and (
                    forecast_resolution._resolution_task.done()
                ):
                    break
                await asyncio.sleep(0.01)
            await forecast_resolution.stop_forecast_resolution()
        resolve.assert_not_awaited()
        self.mock_release.assert_not_called()
        self.assertIsNone(forecast_resolution._resolution_task)

    async def test_start_and_stop_manage_the_background_task(self):
        resolve = AsyncMock(return_value=ResolutionRun())
        with (
            patch.object(forecast_resolution, "resolve_pending", resolve),
            patch.object(forecast_resolution, "_resolution_interval", lambda: 3600),
        ):
            await forecast_resolution.start_forecast_resolution()
            task = forecast_resolution._resolution_task
            self.assertIsNotNone(task)
            await forecast_resolution.start_forecast_resolution()
            self.assertIs(forecast_resolution._resolution_task, task)
            await forecast_resolution.stop_forecast_resolution()
        self.assertTrue(task.done())
        self.assertIsNone(forecast_resolution._resolution_task)


if __name__ == "__main__":
    unittest.main()
