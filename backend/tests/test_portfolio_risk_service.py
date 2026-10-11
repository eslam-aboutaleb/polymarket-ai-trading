"""Tests for the deterministic half of the portfolio risk report.

Covers position normalisation and capping, the concentration/loss-bound math
(the portfolio-value definition must match Kelly's bankroll), clustering by
condition id and shared title tokens, the catalyst window, UserSettings
passthrough, prose/JSON narrative parsing, and the degrade-never-500 path where
the LLM fails but the metrics still come back.
"""

import asyncio
import hashlib
import json
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.services import portfolio_risk_service
from app.services.portfolio_risk_service import (
    build_positions_payload,
    cluster_positions,
    compute_metrics,
    parse_narrative,
    prepare_positions,
    risk_limits_from_settings,
)
from app.utils.cache import InMemoryCache

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def asyncio_result(coro):
    """Run a coroutine to completion on a fresh event loop."""
    return asyncio.new_event_loop().run_until_complete(coro)


def _row(**overrides):
    row = {
        "market_title": "Will the price be up?",
        "condition_id": "0xabc",
        "outcome": "Yes",
        "size": 100.0,
        "avg_price": 0.4,
        "current_price": 0.5,
        "value": 50.0,
        "cost_basis": 40.0,
        "loss_bound": 40.0,
        "weight": 0.0,
        "concentration_flag": False,
    }
    row.update(overrides)
    return row


class _FakeSettings:
    """Stand-in for a UserSettings row with the risk-protection fields."""

    def __init__(self, **fields):
        self.max_drawdown_pct = 25.0
        self.total_loss_halt_pct = 40.0
        self.monthly_loss_limit = None
        self.max_position_size = 100.0
        self.daily_loss_limit = 500.0
        self.peak_capital = None
        self.initial_capital = None
        self.trading_halted = False
        self.halt_reason = None
        self.cooldown_until = None
        self.simulation_mode = False
        self.paper_balance = 1000.0
        self.__dict__.update(fields)


class _FakeService:
    """PolymarketService double whose fetches can be told to fail."""

    def __init__(self, positions=None, usdc_balance=0.0, fail_balance=False, fail_positions=False):
        self._positions = positions or []
        self._usdc_balance = usdc_balance
        self._fail_balance = fail_balance
        self._fail_positions = fail_positions

    async def get_wallet_balance(self, wallet_address, private_key=None, clob_creds=None):
        if self._fail_balance:
            raise RuntimeError("balance boom")
        return {"usdc_balance": self._usdc_balance}

    async def get_positions(self, wallet_address, private_key=None, clob_creds=None):
        if self._fail_positions:
            raise RuntimeError("positions boom")
        return self._positions


class PreparePositionsTests(unittest.TestCase):
    def test_positions_ranked_by_market_value_and_capped(self):
        rows = prepare_positions(
            [
                {"title": "small", "condition_id": "0x1", "size": 10, "avgPrice": 1, "curPrice": 1},
                {"title": "big", "condition_id": "0x2", "size": 100, "avgPrice": 1, "curPrice": 1},
                {"title": "mid", "condition_id": "0x3", "size": 50, "avgPrice": 1, "curPrice": 1},
            ],
            max_positions=2,
        )
        self.assertEqual([row["market_title"] for row in rows], ["big", "mid"])

    def test_camel_and_snake_case_price_fields_are_accepted(self):
        rows = prepare_positions(
            [
                {
                    "title": "camel",
                    "conditionId": "0xc",
                    "size": 10,
                    "avgPrice": 0.25,
                    "curPrice": 0.5,
                },
                {
                    "title": "snake",
                    "condition_id": "0xs",
                    "size": 20,
                    "avg_price": 0.5,
                    "price": 0.75,
                },
            ]
        )
        self.assertEqual(rows[0]["avg_price"], 0.5)
        self.assertEqual(rows[0]["current_price"], 0.75)
        self.assertEqual(rows[1]["avg_price"], 0.25)
        self.assertEqual(rows[1]["current_price"], 0.5)

    def test_missing_current_price_falls_back_to_entry_price(self):
        rows = prepare_positions(
            [{"title": "stale", "condition_id": "0x1", "size": 10, "avgPrice": 0.4}]
        )
        row = rows[0]
        self.assertEqual(row["current_price"], 0.4)
        self.assertEqual(row["value"], row["cost_basis"])

    def test_dust_and_malformed_rows_are_dropped(self):
        rows = prepare_positions(
            [
                {
                    "title": "dust",
                    "condition_id": "0x1",
                    "size": 0,
                    "avgPrice": 0.5,
                    "curPrice": 0.5,
                },
                {"title": "bad", "condition_id": "0x2", "size": "oops", "avgPrice": 0.5},
                "not-a-dict",
                {
                    "title": "real",
                    "condition_id": "0x3",
                    "size": 5,
                    "avgPrice": 0.5,
                    "curPrice": 0.5,
                },
            ]
        )
        self.assertEqual([row["market_title"] for row in rows], ["real"])


class ComputeMetricsTests(unittest.TestCase):
    def test_portfolio_value_matches_the_kelly_bankroll_definition(self):
        rows = [_row(size=10, avg_price=0.4, current_price=0.5, value=5.0, cost_basis=4.0)]
        metrics = compute_metrics(rows, 100.0)
        # usdc_balance + Σ size*curPrice, the same formula as compute_bankroll.
        self.assertEqual(metrics["portfolio_value"], 105.0)
        self.assertEqual(metrics["usdc_balance"], 100.0)
        self.assertEqual(metrics["unrealized_pnl"], 1.0)
        self.assertEqual(metrics["loss_bound"], 4.0)

    def test_weight_and_concentration_flag_above_twenty_percent(self):
        rows = [
            _row(market_title="dominant", condition_id="0x1", value=90.0, cost_basis=90.0),
            _row(market_title="small", condition_id="0x2", value=10.0, cost_basis=10.0),
        ]
        metrics = compute_metrics(rows, 0.0)
        self.assertEqual(metrics["positions_count"], 2)
        self.assertEqual(metrics["max_position_weight"], 0.9)
        self.assertEqual(metrics["concentration_flags"], 1)
        flagged = {row["market_title"]: row["concentration_flag"] for row in metrics["positions"]}
        self.assertTrue(flagged["dominant"])
        self.assertFalse(flagged["small"])

    def test_exactly_twenty_percent_is_not_flagged(self):
        rows = [
            _row(market_title="half a", condition_id="0x1", value=20.0, cost_basis=20.0),
            _row(market_title="half b", condition_id="0x2", value=20.0, cost_basis=20.0),
        ]
        metrics = compute_metrics(rows, 60.0)
        self.assertEqual(metrics["concentration_flags"], 0)
        self.assertEqual(metrics["max_position_weight"], 0.2)

    def test_empty_portfolio_is_defined_not_a_division_error(self):
        metrics = compute_metrics([], 250.0)
        self.assertEqual(metrics["portfolio_value"], 250.0)
        self.assertEqual(metrics["positions_count"], 0)
        self.assertEqual(metrics["loss_bound"], 0.0)
        self.assertEqual(metrics["max_position_weight"], 0.0)
        self.assertEqual(metrics["concentration_flags"], 0)
        self.assertEqual(metrics["clusters"], [])
        self.assertEqual(metrics["catalysts"], [])

    def test_zero_balance_with_positions_still_weights_correctly(self):
        metrics = compute_metrics([_row(value=50.0, cost_basis=50.0)], 0.0)
        self.assertEqual(metrics["max_position_weight"], 1.0)


class ClusterTests(unittest.TestCase):
    def test_yes_and_no_legs_of_one_market_cluster(self):
        rows = [
            _row(
                market_title="Election winner",
                condition_id="0xsame",
                outcome="Yes",
                value=30.0,
                weight=0.3,
            ),
            _row(
                market_title="Election winner",
                condition_id="0xsame",
                outcome="No",
                value=20.0,
                weight=0.2,
            ),
        ]
        clusters = cluster_positions(rows)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["label"], "Election winner")
        self.assertEqual(
            clusters[0]["members"],
            ["Election winner (Yes)", "Election winner (No)"],
        )
        self.assertEqual(clusters[0]["weight"], 0.5)

    def test_shared_title_tokens_cluster_across_markets(self):
        rows = [
            _row(
                market_title="Fed rate cut in March 2026",
                condition_id="0xa",
                value=40.0,
                weight=0.4,
            ),
            _row(
                market_title="Fed rate hike in March 2026",
                condition_id="0xb",
                value=30.0,
                weight=0.3,
            ),
        ]
        for row in rows:
            row["concentration_flag"] = True
        clusters = cluster_positions(rows)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]["members"]), 2)

    def test_unrelated_titles_do_not_cluster(self):
        rows = [
            _row(
                market_title="Champions league winner",
                condition_id="0xa",
                value=40.0,
                weight=0.4,
            ),
            _row(market_title="Bitcoin above 100k", condition_id="0xb", value=30.0, weight=0.3),
        ]
        self.assertEqual(cluster_positions(rows), [])

    def test_single_position_forms_no_cluster(self):
        self.assertEqual(cluster_positions([_row()]), [])


class CatalystTests(unittest.TestCase):
    END_DATES = {
        "0xsoon": "2030-01-04",
        "0xlater": "2030-01-30",
        "0xfuture": "2030-06-01",
        "0xpast": "2029-12-01",
        "0xbroken": "not-a-date",
    }

    def test_catalysts_window_sorted_and_filtered(self):
        rows = [
            _row(market_title="later market", condition_id="0xlater"),
            _row(market_title="soon market", condition_id="0xsoon"),
            _row(market_title="far market", condition_id="0xfuture"),
            _row(market_title="resolved market", condition_id="0xpast"),
            _row(market_title="broken market", condition_id="0xbroken"),
            _row(market_title="no date", condition_id="0xmissing"),
        ]
        catalysts = compute_metrics(rows, 100.0, end_dates=self.END_DATES, now=NOW)["catalysts"]
        self.assertEqual(
            [item["market_title"] for item in catalysts], ["soon market", "later market"]
        )
        self.assertEqual([item["days_to_expiry"] for item in catalysts], [3, 29])
        self.assertEqual(catalysts[0]["end_date"], "2030-01-04")

    def test_catalysts_dedupe_yes_and_no_legs(self):
        rows = [
            _row(market_title="Dual leg", condition_id="0xdual", outcome="Yes"),
            _row(market_title="Dual leg", condition_id="0xdual", outcome="No"),
        ]
        catalysts = compute_metrics(rows, 100.0, end_dates={"0xdual": "2030-01-11"}, now=NOW)[
            "catalysts"
        ]
        self.assertEqual(len(catalysts), 1)
        self.assertEqual(catalysts[0]["end_date"], "2030-01-11")
        self.assertEqual(catalysts[0]["days_to_expiry"], 10)


class RiskLimitsTests(unittest.TestCase):
    def test_no_settings_yields_empty_limits(self):
        self.assertEqual(risk_limits_from_settings(None), {})

    def test_live_limits_are_echoed(self):
        limits = risk_limits_from_settings(_FakeSettings())
        self.assertEqual(limits["max_drawdown_pct"], 25.0)
        self.assertEqual(limits["total_loss_halt_pct"], 40.0)
        self.assertEqual(limits["daily_loss_limit"], 500.0)
        self.assertEqual(limits["trading_halted"], False)
        self.assertEqual(limits["simulation_mode"], False)
        self.assertEqual(limits["mode"], "live")
        self.assertEqual(limits["concentration_threshold_pct"], 20)
        self.assertNotIn("cooldown_until", limits)
        # Unset optional caps are dropped rather than echoed as a zero cap.
        self.assertNotIn("monthly_loss_limit", limits)
        self.assertNotIn("peak_capital", limits)
        self.assertNotIn("initial_capital", limits)

    def test_zero_paper_balance_is_still_reported(self):
        limits = risk_limits_from_settings(_FakeSettings(paper_balance=0.0))
        self.assertEqual(limits["paper_balance"], 0.0)

    def test_paper_mode_and_cooldown_are_reported(self):
        cooldown = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
        limits = risk_limits_from_settings(
            _FakeSettings(simulation_mode=True, cooldown_until=cooldown, monthly_loss_limit=250.0)
        )
        self.assertEqual(limits["mode"], "paper")
        self.assertTrue(limits["simulation_mode"])
        self.assertEqual(limits["paper_balance"], 1000.0)
        self.assertEqual(limits["monthly_loss_limit"], 250.0)
        self.assertEqual(limits["cooldown_until"], cooldown.isoformat())


class NarrativeParsingTests(unittest.TestCase):
    def test_empty_report_has_no_narrative(self):
        self.assertIsNone(parse_narrative(""))
        self.assertIsNone(parse_narrative(None))

    def test_prose_sections_are_split_into_fields(self):
        text = (
            "Overall this portfolio is moderately risky.\n\n"
            "1. Total portfolio VaR (95%) - about $40 in the worst 5% of weeks.\n"
            "2. Concentration risks\n"
            "- Alpha is 55% of the book\n"
            "- Beta is 30% of the book\n"
            "3. Correlated position clusters\n"
            "- Both Fed markets resolve on the same date\n"
            "4. Event calendar risks\n"
            "- CPI print next week\n"
            "5. Tail risk scenarios\n"
            "- A surprise hike reprices everything\n"
            "6. Recommended hedges or position adjustments\n"
            "- Trim Alpha by half\n"
        )
        narrative = parse_narrative(text)
        self.assertEqual(
            narrative["overall_risk_rating"], "Overall this portfolio is moderately risky."
        )
        self.assertEqual(
            narrative["var_estimate"],
            "Total portfolio VaR (95%) - about $40 in the worst 5% of weeks.",
        )
        self.assertEqual(
            narrative["concentration_flags"],
            ["Alpha is 55% of the book", "Beta is 30% of the book"],
        )
        self.assertEqual(
            narrative["correlated_clusters"], ["Both Fed markets resolve on the same date"]
        )
        self.assertEqual(narrative["tail_scenarios"], ["A surprise hike reprices everything"])
        self.assertEqual(narrative["hedges"], ["Trim Alpha by half"])

    def test_markdown_headers_are_detected(self):
        text = (
            "## Concentration risks\n"
            "- One market dominates the book\n"
            "**Recommended hedges**\n"
            "- Reduce the largest leg\n"
        )
        narrative = parse_narrative(text)
        self.assertEqual(narrative["concentration_flags"], ["One market dominates the book"])
        self.assertEqual(narrative["hedges"], ["Reduce the largest leg"])

    def test_json_answer_is_mapped(self):
        text = (
            '{"overall_risk_rating": "high", "var_estimate": "$120", '
            '"concentration_flags": ["a", "b"], '
            '"correlated_clusters": "fed pair", "tail_scenarios": ["black swan"], '
            '"hedges": ["trim"]}'
        )
        narrative = parse_narrative(text)
        self.assertEqual(narrative["overall_risk_rating"], "high")
        self.assertEqual(narrative["var_estimate"], "$120")
        self.assertEqual(narrative["concentration_flags"], ["a", "b"])
        self.assertEqual(narrative["correlated_clusters"], ["fed pair"])
        self.assertEqual(narrative["tail_scenarios"], ["black swan"])
        self.assertEqual(narrative["hedges"], ["trim"])

    def test_unstructured_prose_becomes_the_rating(self):
        narrative = parse_narrative("The book is small and diversified.")
        self.assertEqual(narrative["overall_risk_rating"], "The book is small and diversified.")
        self.assertEqual(narrative["concentration_flags"], [])


class FetchMarketEndDatesTests(unittest.TestCase):
    """Catalyst dates come from the Gamma markets feed, never the DB."""

    @staticmethod
    def _client_factory(handler):
        real_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs.pop("timeout", None)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        return factory

    def test_no_positions_means_no_request(self):
        self.assertEqual(asyncio_result(portfolio_risk_service.fetch_market_end_dates([])), {})

    def test_gamma_response_is_mapped_by_condition_id(self):
        seen_params = []

        def handler(request):
            seen_params.extend(request.url.params.get_list("condition_ids"))
            return httpx.Response(
                200,
                json=[
                    {"conditionId": "0xa", "endDate": "2030-01-04T00:00:00Z"},
                    {"condition_id": "0xb", "end_date_iso": "2030-02-01"},
                    {"conditionId": "0xc"},
                ],
            )

        with patch(
            "httpx.AsyncClient",
            self._client_factory(handler),
        ):
            end_dates = asyncio_result(
                portfolio_risk_service.fetch_market_end_dates(["0xa", "0xb", "0xc"])
            )

        self.assertEqual(seen_params, ["0xa", "0xb", "0xc"])
        self.assertEqual(end_dates, {"0xa": "2030-01-04T00:00:00Z", "0xb": "2030-02-01"})

    def test_upstream_failures_return_an_empty_map(self):
        for status in (500, 429):
            responder = lambda request, status=status: httpx.Response(status, json={})  # noqa: E731
            with patch("httpx.AsyncClient", self._client_factory(responder)):
                self.assertEqual(
                    asyncio_result(portfolio_risk_service.fetch_market_end_dates(["0xa"])), {}
                )

        def boom(request):
            raise httpx.ConnectError("unreachable")

        with patch("httpx.AsyncClient", self._client_factory(boom)):
            self.assertEqual(
                asyncio_result(portfolio_risk_service.fetch_market_end_dates(["0xa"])), {}
            )


class NarrativeCacheKeyTests(unittest.TestCase):
    """The narrative cache key must survive a price tick — and not leak users."""

    def test_the_same_hour_reuses_one_key(self):
        start = datetime(2030, 1, 1, 10, 0, 0, tzinfo=UTC)
        later = datetime(2030, 1, 1, 10, 59, 0, tzinfo=UTC)
        first = portfolio_risk_service._narrative_cache_key("payload", 7, now=start)
        second = portfolio_risk_service._narrative_cache_key("payload", 7, now=later)
        self.assertEqual(first, second)

    def test_a_new_hour_gets_a_new_key(self):
        start = datetime(2030, 1, 1, 10, 0, 0, tzinfo=UTC)
        later = datetime(2030, 1, 1, 11, 0, 0, tzinfo=UTC)
        self.assertNotEqual(
            portfolio_risk_service._narrative_cache_key("payload", 7, now=start),
            portfolio_risk_service._narrative_cache_key("payload", 7, now=later),
        )

    def test_the_user_id_is_part_of_the_key(self):
        moment = datetime(2030, 1, 1, 10, 0, 0, tzinfo=UTC)
        self.assertNotEqual(
            portfolio_risk_service._narrative_cache_key("payload", 7, now=moment),
            portfolio_risk_service._narrative_cache_key("payload", 8, now=moment),
        )
        # An anonymous caller is keyed too, so it cannot collide with user 0.
        self.assertNotEqual(
            portfolio_risk_service._narrative_cache_key("payload", None, now=moment),
            portfolio_risk_service._narrative_cache_key("payload", 0, now=moment),
        )

    def test_a_re_priced_but_unchanged_book_keeps_its_key(self):
        """The tick-derived columns must not reach the key."""
        priced = build_positions_payload(
            {
                "portfolio_value": 150.0,
                "unrealized_pnl": 20.0,
                "positions_count": 1,
                "positions": [
                    {
                        "market_title": "Fed cut",
                        "condition_id": "0xfed",
                        "outcome": "Yes",
                        "size": 100.0,
                        "value": 50.0,
                        "weight": 0.33,
                        "current_price": 0.5,
                    }
                ],
                "clusters": [],
                "catalysts": [],
                "risk_limits": {"mode": "live"},
            }
        )
        moved = json.loads(priced)
        moved["portfolio_value"] = 210.0
        moved["unrealized_pnl"] = 80.0
        moved["positions"][0]["current_price"] = 0.9
        moved["positions"][0]["value"] = 110.0
        moved["positions"][0]["weight"] = 0.52
        moved_moved = json.dumps(moved)

        self.assertEqual(
            portfolio_risk_service._payload_fingerprint(priced),
            portfolio_risk_service._payload_fingerprint(moved_moved),
        )

    def test_a_materially_different_book_gets_its_own_key(self):
        one_book = json.dumps(
            {"positions_count": 1, "positions": [{"market_title": "A", "size": 1}], "clusters": []}
        )
        two_books = json.dumps(
            {
                "positions_count": 2,
                "positions": [{"market_title": "A", "size": 1}, {"market_title": "B", "size": 2}],
                "clusters": [],
            }
        )

        self.assertNotEqual(
            portfolio_risk_service._payload_fingerprint(one_book),
            portfolio_risk_service._payload_fingerprint(two_books),
        )

    def test_a_non_json_payload_falls_back_to_a_plain_hash(self):
        raw = "not json at all"
        self.assertEqual(
            portfolio_risk_service._payload_fingerprint(raw),
            hashlib.sha256(raw.encode()).hexdigest(),
        )


class BuildReportTests(unittest.TestCase):
    def setUp(self):
        self.cache = InMemoryCache("test_risk_report")
        cache_patcher = patch.object(portfolio_risk_service, "narrative_cache", self.cache)
        cache_patcher.start()
        self.addCleanup(cache_patcher.stop)

    def _client(self, report="1. Concentration risks\n- one leg dominates", success=True):
        client = MagicMock()
        client.generate_risk_report = AsyncMock(return_value={"report": report, "success": success})
        return client

    def _patch_client(self, client):
        return patch(
            "app.grpc_clients.analysis_client.get_analysis_client",
            return_value=client,
        )

    async def _report(self, service, **kwargs):
        return await portfolio_risk_service.build_risk_report(
            service,
            "0xwallet",
            private_key="0xkey",
            clob_creds={"api_key": "k"},
            **kwargs,
        )

    def test_metrics_and_narrative_for_a_funded_wallet(self):
        service = _FakeService(
            positions=[
                {
                    "title": "Fed cut",
                    "condition_id": "0xfed",
                    "outcome": "Yes",
                    "size": 100,
                    "avgPrice": 0.3,
                    "curPrice": 0.5,
                }
            ],
            usdc_balance=100.0,
        )
        with (
            self._patch_client(self._client()),
            patch.object(
                portfolio_risk_service,
                "fetch_market_end_dates",
                AsyncMock(return_value={}),
            ),
        ):
            report = asyncio_result(self._report(service, user_settings=_FakeSettings()))

        metrics = report["metrics"]
        self.assertEqual(metrics["portfolio_value"], 150.0)
        self.assertEqual(metrics["positions_count"], 1)
        self.assertEqual(metrics["loss_bound"], 30.0)
        self.assertEqual(metrics["risk_limits"]["mode"], "live")
        self.assertEqual(report["narrative"]["concentration_flags"], ["one leg dominates"])
        self.assertTrue(report["generated_at"].startswith("2"))

    def test_empty_portfolio_returns_metrics_without_calling_the_llm(self):
        client = self._client()
        with (
            self._patch_client(client),
            patch.object(
                portfolio_risk_service,
                "fetch_market_end_dates",
                AsyncMock(return_value={}),
            ),
        ):
            report = asyncio_result(self._report(_FakeService(usdc_balance=75.0)))

        self.assertEqual(report["metrics"]["portfolio_value"], 75.0)
        self.assertEqual(report["metrics"]["positions_count"], 0)
        self.assertIsNone(report["narrative"])
        client.generate_risk_report.assert_not_awaited()

    def test_llm_failure_degrades_to_null_narrative(self):
        client = MagicMock()
        client.generate_risk_report = AsyncMock(side_effect=RuntimeError("grpc down"))
        service = _FakeService(
            positions=[
                {"title": "x", "condition_id": "0x1", "size": 10, "avgPrice": 1, "curPrice": 1}
            ],
            usdc_balance=10.0,
        )
        with (
            self._patch_client(client),
            patch.object(
                portfolio_risk_service,
                "fetch_market_end_dates",
                AsyncMock(return_value={}),
            ),
        ):
            report = asyncio_result(self._report(service))

        self.assertIsNone(report["narrative"])
        self.assertEqual(report["metrics"]["portfolio_value"], 20.0)

    def test_llm_failure_flag_degrades_to_null_narrative(self):
        service = _FakeService(
            positions=[
                {"title": "x", "condition_id": "0x1", "size": 10, "avgPrice": 1, "curPrice": 1}
            ],
            usdc_balance=10.0,
        )
        with (
            self._patch_client(self._client(report="", success=False)),
            patch.object(
                portfolio_risk_service,
                "fetch_market_end_dates",
                AsyncMock(return_value={}),
            ),
        ):
            report = asyncio_result(self._report(service))

        self.assertIsNone(report["narrative"])
        self.assertEqual(report["metrics"]["positions_count"], 1)

    def test_wallet_failures_degrade_to_zero_not_an_exception(self):
        service = _FakeService(
            usdc_balance=0.0,
            fail_balance=True,
            fail_positions=True,
        )
        with patch.object(
            portfolio_risk_service,
            "fetch_market_end_dates",
            AsyncMock(return_value={}),
        ):
            report = asyncio_result(
                self._report(service, include_narrative=False, user_settings=None)
            )

        self.assertEqual(report["metrics"]["portfolio_value"], 0.0)
        self.assertEqual(report["metrics"]["positions_count"], 0)
        self.assertEqual(report["metrics"]["risk_limits"], {})

    def test_paper_mode_is_visible_in_the_metrics(self):
        with patch.object(
            portfolio_risk_service,
            "fetch_market_end_dates",
            AsyncMock(return_value={}),
        ):
            report = asyncio_result(
                self._report(
                    _FakeService(usdc_balance=100.0),
                    include_narrative=False,
                    user_settings=_FakeSettings(simulation_mode=True),
                )
            )

        self.assertEqual(report["metrics"]["risk_limits"]["mode"], "paper")

    def test_narrative_is_cached_per_payload(self):
        client = self._client()
        service = _FakeService(
            positions=[
                {"title": "x", "condition_id": "0x1", "size": 10, "avgPrice": 1, "curPrice": 1}
            ],
            usdc_balance=10.0,
        )
        with (
            self._patch_client(client),
            patch.object(
                portfolio_risk_service,
                "fetch_market_end_dates",
                AsyncMock(return_value={}),
            ),
        ):
            first = asyncio_result(self._report(service))
            second = asyncio_result(self._report(service))

        self.assertEqual(first["narrative"], second["narrative"])
        self.assertEqual(client.generate_risk_report.await_count, 1)

    def test_a_price_tick_does_not_rebill_the_model(self):
        """The payload embeds live prices, so hashing it alone expired per tick.

        With the hour bucket in the key, a refresh inside the same hour
        reuses the cached narrative: the LLM is called once, not once per
        page load.
        """
        client = self._client()
        positions = [
            {"title": "x", "condition_id": "0x1", "size": 10, "avgPrice": 1, "curPrice": 1}
        ]
        service = _FakeService(positions=positions, usdc_balance=10.0)
        with (
            self._patch_client(client),
            patch.object(
                portfolio_risk_service,
                "fetch_market_end_dates",
                AsyncMock(return_value={}),
            ),
            patch.object(portfolio_risk_service, "utc_now", return_value=NOW),
        ):
            asyncio_result(self._report(service, user_id=7))
            # The market moved: a new payload hash, same hour, same user.
            positions[0]["curPrice"] = 1.4
            asyncio_result(self._report(service, user_id=7))

        self.assertEqual(client.generate_risk_report.await_count, 1)

    def test_two_users_never_share_a_cached_narrative(self):
        client = self._client()
        service = _FakeService(
            positions=[
                {"title": "x", "condition_id": "0x1", "size": 10, "avgPrice": 1, "curPrice": 1}
            ],
            usdc_balance=10.0,
        )
        with (
            self._patch_client(client),
            patch.object(
                portfolio_risk_service,
                "fetch_market_end_dates",
                AsyncMock(return_value={}),
            ),
            patch.object(portfolio_risk_service, "utc_now", return_value=NOW),
        ):
            asyncio_result(self._report(service, user_id=7))
            asyncio_result(self._report(service, user_id=8))

        self.assertEqual(client.generate_risk_report.await_count, 2)


class BuildPositionsPayloadTests(unittest.TestCase):
    def test_payload_carries_metrics_clusters_and_risk_limits(self):
        metrics = {
            "portfolio_value": 150.0,
            "usdc_balance": 100.0,
            "unrealized_pnl": 20.0,
            "positions_count": 1,
            "loss_bound": 30.0,
            "max_position_weight": 0.33,
            "concentration_flags": 0,
            "positions": [_row()],
            "clusters": [{"label": "Fed cut", "members": ["Fed cut (Yes)"], "weight": 0.33}],
            "catalysts": [
                {"market_title": "Fed cut", "end_date": "2030-01-10", "days_to_expiry": 3}
            ],
            "risk_limits": {"mode": "live"},
        }
        payload = json.loads(build_positions_payload(metrics))
        self.assertEqual(payload["portfolio_value"], 150.0)
        self.assertEqual(payload["loss_bound"], 30.0)
        self.assertEqual(payload["risk_limits"], {"mode": "live"})
        self.assertEqual(payload["clusters"][0]["label"], "Fed cut")
        self.assertEqual(len(payload["positions"]), 1)


if __name__ == "__main__":
    unittest.main()
