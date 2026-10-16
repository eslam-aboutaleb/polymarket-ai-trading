"""Tests for app/services/research_quality.py (plan 06).

Covers the deterministic credibility model (freshness / diversity /
corroboration and their weights), the evidence builders that map raw
backend data onto evidence sources, the free-text parsers for the two
research prompts, and the batch orchestration's degrade-to-baseline
contract: every LLM layer is optional and a failure must leave the
deterministic score — never raise, never fail a scan.
"""

import unittest
from unittest.mock import AsyncMock, patch

from app.services.analytics import research_quality as rq
from app.services.analytics.research_quality import (
    BATCH_KEY,
    FRESHNESS_STALE_HOURS,
    GroupEvidence,
    build_scan_sources,
    credibility_from_sources,
    diversity_score,
    evaluate_source,
    parse_source_rating,
    parse_synthesis_report,
    score_batch_credibility,
    source_freshness,
)


def _source(domain="polymarket.clob", age_hours=0.0, stance=None, name="src"):
    return rq.EvidenceSource(
        name=name, domain=domain, summary="summary", age_hours=age_hours, stance=stance
    )


class _FakeClient:
    """Analysis-client double recording every smuggled prompt call.

    Mirrors the two coordinated client contracts:
    ``synthesize_research`` returns ``{"report", "success"}`` and
    ``analyze_market`` (the smuggling vehicle for the source-evaluation
    prompt) returns ``{"analysis": <free text>}``.
    """

    def __init__(self, report="KEY CONSENSUS: ok", success=True, rating=None):
        self.rating = "Overall: 8/10 — credible" if rating is None else rating
        self.analyze_market = AsyncMock(return_value={"analysis": self.rating})
        self.synthesize_research = AsyncMock(return_value={"report": report, "success": success})

    @property
    def synthesis_calls(self):
        return self.synthesize_research.await_count

    @property
    def source_calls(self):
        return self.analyze_market.await_count


class FreshnessTests(unittest.TestCase):
    def test_unknown_age_is_neutral(self):
        self.assertEqual(source_freshness(None), 0.5)

    def test_age_at_or_below_full_hours_is_fresh(self):
        self.assertEqual(source_freshness(0.0), 1.0)
        self.assertEqual(source_freshness(rq.FRESHNESS_FULL_HOURS), 1.0)

    def test_age_at_or_beyond_stale_hours_is_stale(self):
        self.assertEqual(source_freshness(FRESHNESS_STALE_HOURS), 0.0)
        self.assertEqual(source_freshness(FRESHNESS_STALE_HOURS * 2), 0.0)

    def test_decay_is_linear_between_the_bounds(self):
        midpoint = (rq.FRESHNESS_FULL_HOURS + FRESHNESS_STALE_HOURS) / 2
        self.assertAlmostEqual(source_freshness(midpoint), 0.5)

    def test_freshness_score_is_the_mean(self):
        sources = [_source(age_hours=0.0, domain="a"), _source(age_hours=24.0, domain="b")]
        expected = (source_freshness(0.0) + source_freshness(24.0)) / 2
        self.assertAlmostEqual(rq.freshness_score(sources), expected)

    def test_freshness_score_without_sources_is_neutral(self):
        self.assertEqual(rq.freshness_score([]), 0.5)


class DiversityTests(unittest.TestCase):
    def test_no_sources(self):
        self.assertEqual(diversity_score([]), 0.0)

    def test_saturates_at_four_domains(self):
        self.assertAlmostEqual(diversity_score([_source(domain="a")]), 0.25)
        self.assertAlmostEqual(diversity_score([_source(domain="a"), _source(domain="b")]), 0.5)
        six = [_source(domain=f"d{i}") for i in range(6)]
        self.assertEqual(diversity_score(six), 1.0)

    def test_repeats_do_not_count(self):
        sources = [_source(domain="a", name=f"s{i}") for i in range(3)]
        self.assertAlmostEqual(diversity_score(sources), 0.25)


class CorroborationTests(unittest.TestCase):
    def test_no_stances_is_neutral(self):
        self.assertEqual(rq.corroboration_score([_source(), _source(domain="b")]), 0.5)

    def test_single_stream_cannot_corroborate(self):
        sources = [_source(domain="a", stance="YES")]
        self.assertEqual(rq.corroboration_score(sources), 0.5)

    def test_two_domains_agreeing_is_full_corroboration(self):
        sources = [_source(domain="a", stance="YES"), _source(domain="b", stance="YES")]
        self.assertEqual(rq.corroboration_score(sources), 1.0)

    def test_two_domains_disagreeing_is_neutral(self):
        sources = [_source(domain="a", stance="YES"), _source(domain="b", stance="NO")]
        self.assertEqual(rq.corroboration_score(sources), 0.5)

    def test_majority_share_is_used(self):
        sources = [
            _source(domain="a", stance="YES"),
            _source(domain="b", stance="YES"),
            _source(domain="c", stance="NO"),
        ]
        self.assertAlmostEqual(rq.corroboration_score(sources), 2 / 3)

    def test_same_domain_agreement_is_not_independent(self):
        sources = [_source(domain="a", stance="YES"), _source(domain="a", stance="YES")]
        self.assertEqual(rq.corroboration_score(sources), 0.5)


class CredibilityFromSourcesTests(unittest.TestCase):
    def test_no_evidence_uses_the_documented_baseline(self):
        self.assertEqual(credibility_from_sources([]), rq.NO_EVIDENCE_CREDIBILITY)

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(rq.CREDIBILITY_WEIGHTS.values()), 1.0)

    def test_fresh_diverse_corroborated_evidence_scores_high(self):
        sources = [
            _source(domain="a", age_hours=0.0, stance="YES"),
            _source(domain="b", age_hours=1.0, stance="YES"),
            _source(domain="c", age_hours=2.0, stance="YES"),
            _source(domain="d", age_hours=3.0, stance="YES"),
        ]
        self.assertEqual(credibility_from_sources(sources), 100)

    def test_stale_undiverse_evidence_scores_low(self):
        sources = [
            _source(domain="a", age_hours=FRESHNESS_STALE_HOURS),
            _source(domain="a", age_hours=FRESHNESS_STALE_HOURS),
        ]
        # No freshness, a single source family and nothing to corroborate.
        self.assertEqual(credibility_from_sources(sources), 25)

    def test_score_stays_inside_0_100(self):
        self.assertGreaterEqual(credibility_from_sources([_source()]), 0)
        self.assertLessEqual(credibility_from_sources([_source()]), 100)


class EvidenceBuilderTests(unittest.TestCase):
    def test_whale_and_market_streams_are_independent(self):
        sources = build_scan_sources(
            [{"volume24hr": 1000, "liquidityNum": 500, "oneDayPriceChange": 0.04}],
            {"whale_bias": "YES", "whale_count": 3, "context_text": "book snapshot"},
        )
        self.assertEqual(
            [s.domain for s in sources],
            ["polymarket.clob", "polymarket.market-data", "polymarket.price-action"],
        )
        self.assertEqual(sources[0].stance, "YES")
        self.assertEqual(sources[0].age_hours, 0.0)
        self.assertEqual(sources[2].stance, "YES")
        self.assertIn("+4.00%", sources[2].summary)

    def test_momentum_can_disagree_with_whales(self):
        sources = build_scan_sources(
            [{"volume24hr": 10, "oneDayPriceChange": -0.05}],
            {"whale_bias": "YES"},
        )
        stances = {s.domain: s.stance for s in sources}
        self.assertEqual(stances["polymarket.clob"], "YES")
        self.assertEqual(stances["polymarket.price-action"], "NO")

    def test_flat_momentum_carries_no_stance(self):
        sources = build_scan_sources([{"oneDayPriceChange": 0.001}])
        momentum = [s for s in sources if s.domain == "polymarket.price-action"][0]
        self.assertIsNone(momentum.stance)

    def test_markets_without_24h_volume_have_unknown_age(self):
        sources = build_scan_sources([{"liquidityNum": 0, "volume24hr": 0}])
        market_data = [s for s in sources if s.domain == "polymarket.market-data"][0]
        self.assertIsNone(market_data.age_hours)
        self.assertIn("No 24h volume", market_data.summary)

    def test_no_markets_yields_only_the_whale_stream(self):
        self.assertEqual(
            build_scan_sources([], {"whale_bias": "YES"}),
            [build_scan_sources(None, {"whale_bias": "YES"})[0]],
        )
        self.assertEqual(build_scan_sources([], None), [])

    def test_garbage_values_are_ignored(self):
        sources = build_scan_sources(
            [{"volume24hr": "bad", "liquidity": "worse", "oneDayPriceChange": "nope"}]
        )
        self.assertEqual([s.domain for s in sources], ["polymarket.market-data"])

    def test_mixed_whale_bias_is_not_a_stance(self):
        sources = build_scan_sources([{"volume24hr": 5}], {"whale_bias": "MIXED"})
        whale = [s for s in sources if s.domain == "polymarket.clob"][0]
        self.assertIsNone(whale.stance)

    def test_empty_smart_money_payload_adds_nothing(self):
        domains = [s.domain for s in build_scan_sources([{"volume24hr": 1}], {})]
        self.assertEqual(domains, ["polymarket.market-data"])
        self.assertEqual(build_scan_sources([], {}), [])


class ParsingTests(unittest.TestCase):
    def test_parses_explicit_overall_rating(self):
        self.assertEqual(parse_source_rating("Overall: 8/10 — credible"), 8.0)

    def test_falls_back_to_the_first_number(self):
        self.assertEqual(parse_source_rating("The source rates 7 on credibility"), 7.0)

    def test_clamps_out_of_range_ratings(self):
        self.assertEqual(parse_source_rating("Overall: 42"), 10.0)
        self.assertEqual(parse_source_rating("Overall: 0"), 1.0)

    def test_unparseable_answers(self):
        self.assertIsNone(parse_source_rating(None))
        self.assertIsNone(parse_source_rating("no numbers here"))
        self.assertIsNone(parse_source_rating("v1.2.3 release notes"))

    def test_per_group_lines_win(self):
        report = (
            "KEY CONSENSUS: streams agree\n"
            "2. DISAGREEMENTS: none\n"
            "- CREDIBILITY: event-a 72\n"
            "- CREDIBILITY: event-b 40\n"
        )
        parsed, summary = parse_synthesis_report(report, ["event-a", "event-b"])
        self.assertEqual(parsed, {"event-a": 72, "event-b": 40})
        self.assertEqual(summary, "streams agree")

    def test_unknown_group_keys_are_dropped(self):
        report = "CREDIBILITY: not-a-group 90\nCREDIBILITY: event-a 60\n"
        parsed, _ = parse_synthesis_report(report, ["event-a"])
        self.assertEqual(parsed, {"event-a": 60})

    def test_batch_level_fallback(self):
        parsed, _ = parse_synthesis_report("Calibrated credibility: 64 for the batch.", [])
        self.assertEqual(parsed, {BATCH_KEY: 64})

    def test_no_number_at_all_degrades_to_empty(self):
        parsed, summary = parse_synthesis_report("the sources are mixed", ["event-a"])
        self.assertEqual(parsed, {})
        self.assertEqual(summary, "the sources are mixed")

    def test_summary_prefers_the_consensus_section(self):
        report = "Preamble.\nKEY CONSENSUS: two streams agree.\n3. BASE RATE ANCHOR: 50%"
        _, summary = parse_synthesis_report(report, [])
        self.assertEqual(summary, "two streams agree.")


class ScoreBatchCredibilityTests(unittest.IsolatedAsyncioTestCase):
    def _groups(self, count=1):
        return [
            GroupEvidence(
                key=f"event-{i}",
                question=f"Question {i}?",
                markets=[{"volume24hr": 1000, "liquidityNum": 500}],
                smart_money={"whale_bias": "YES", "context_text": "book"},
                priority=float(i),
            )
            for i in range(count)
        ]

    async def test_no_groups_or_client(self):
        self.assertEqual(await score_batch_credibility(None, self._groups()), {})
        self.assertEqual(await score_batch_credibility(_FakeClient(), []), {})

    async def test_deterministic_baseline_when_nothing_parses(self):
        client = _FakeClient(report="no numbers", rating="unparseable answer")
        result = await score_batch_credibility(client, self._groups())
        self.assertEqual(len(result), 1)
        sources = build_scan_sources(self._groups()[0].markets, self._groups()[0].smart_money)
        self.assertEqual(result["event-0"].credibility_score, credibility_from_sources(sources))
        # Batch-level synthesis still supplies the summary prose.
        self.assertEqual(result["event-0"].search_summary, "no numbers")

    async def test_synthesis_failure_keeps_the_baseline(self):
        class Broken(_FakeClient):
            def __init__(self):
                super().__init__(rating="unparseable")
                self.synthesize_research = AsyncMock(side_effect=RuntimeError("model down"))

        group = self._groups()[0]
        client = Broken()
        result = await score_batch_credibility(client, [group])
        baseline = credibility_from_sources(build_scan_sources(group.markets, group.smart_money))
        self.assertEqual(result["event-0"].credibility_score, baseline)
        self.assertEqual(result["event-0"].search_summary, "")

    async def test_unsuccessful_synthesis_reply_keeps_the_baseline(self):
        client = _FakeClient(report="ignored", success=False, rating="unparseable")
        group = self._groups()[0]
        result = await score_batch_credibility(client, [group])
        baseline = credibility_from_sources(build_scan_sources(group.markets, group.smart_money))
        self.assertEqual(result["event-0"].credibility_score, baseline)

    async def test_one_synthesis_call_per_batch_not_per_group(self):
        client = _FakeClient(report="KEY CONSENSUS: ok\nCREDIBILITY: event-0 60")
        await score_batch_credibility(client, self._groups(count=4))
        self.assertEqual(client.synthesis_calls, 1)

    async def test_source_evaluation_does_not_request_web_research(self):
        client = _FakeClient(rating="Overall: 10/10")
        await score_batch_credibility(client, self._groups(), max_source_evaluations=1)
        kwargs = client.analyze_market.await_args.kwargs
        self.assertFalse(kwargs["include_research"])
        self.assertEqual(kwargs["llm_config"], None)

    async def test_per_group_synthesis_adjustment_is_blended(self):
        group = self._groups()[0]
        client = _FakeClient(report="CREDIBILITY: event-0 100", rating="unparseable")
        result = await score_batch_credibility(client, [group])
        baseline = credibility_from_sources(build_scan_sources(group.markets, group.smart_money))
        self.assertEqual(
            result["event-0"].credibility_score,
            rq._blend(baseline, 1.0, rq.SYNTHESIS_WEIGHT),
        )

    async def test_batch_level_value_applies_to_every_group(self):
        client = _FakeClient(
            report="KEY CONSENSUS: all groups\ncredibility 100", rating="unparseable"
        )
        groups = self._groups(count=3)
        result = await score_batch_credibility(client, groups)
        for group in groups:
            baseline = credibility_from_sources(
                build_scan_sources(group.markets, group.smart_money)
            )
            self.assertEqual(
                result[group.key].credibility_score,
                rq._blend(baseline, 1.0, rq.SYNTHESIS_WEIGHT),
            )

    async def test_source_ratings_are_capped(self):
        client = _FakeClient(report="no numbers", rating="Overall: 10/10")
        await score_batch_credibility(client, self._groups(count=12))
        self.assertEqual(client.source_calls, rq.MAX_SOURCE_EVALUATIONS)

    async def test_only_the_highest_priority_groups_are_evaluated(self):
        client = _FakeClient(report="no numbers", rating="Overall: 10/10")
        groups = self._groups(count=4)
        groups.reverse()
        await score_batch_credibility(client, groups, max_source_evaluations=2)
        evaluated = {
            call.kwargs["market_description"] for call in client.analyze_market.await_args_list
        }
        self.assertEqual(len(evaluated), 2)
        self.assertTrue(all("Question 2?" in d or "Question 3?" in d for d in evaluated))

    async def test_source_ratings_lift_the_baseline(self):
        group = self._groups()[0]
        client = _FakeClient(report="no numbers", rating="Overall: 10/10")
        result = await score_batch_credibility(client, [group], max_source_evaluations=1)
        baseline = credibility_from_sources(build_scan_sources(group.markets, group.smart_money))
        self.assertEqual(
            result["event-0"].credibility_score,
            rq._blend(baseline, 1.0, rq.SOURCE_EVALUATION_WEIGHT),
        )

    async def test_summary_is_shared_by_every_group_in_the_batch(self):
        client = _FakeClient(report="KEY CONSENSUS: streams agree", rating="unparseable")
        result = await score_batch_credibility(client, self._groups(count=3))
        self.assertEqual({v.search_summary for v in result.values()}, {"streams agree"})

    async def test_every_group_gets_a_result_even_without_evidence(self):
        client = _FakeClient(report="no numbers", rating=None)
        groups = [GroupEvidence(key="solo", question="Q?", markets=[])]
        result = await score_batch_credibility(client, groups)
        self.assertEqual(result["solo"].credibility_score, rq.NO_EVIDENCE_CREDIBILITY)


class EvaluateSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_renders_the_real_source_evaluation_template(self):
        client = _FakeClient(rating="Overall: 8")
        source = rq.EvidenceSource(
            name="Order-book whale positioning",
            domain="polymarket.clob",
            summary="YES whale volume dominates",
            age_hours=0.0,
        )
        rating = await evaluate_source(client, source, "Will it happen?")
        self.assertEqual(rating, 8.0)
        description = client.analyze_market.await_args.kwargs["market_description"]
        # Every placeholder of research.source_evaluation is filled with
        # real data, which is what the prompt-contract test enforces.
        self.assertIn("Will it happen?", description)
        self.assertIn("Order-book whale positioning", description)
        self.assertIn("YES whale volume dominates", description)
        self.assertIn("minutes ago", description)

    async def test_template_fallback_when_the_store_is_unreadable(self):
        client = _FakeClient(rating="Overall: 5")
        source = rq.EvidenceSource(name="src", domain="d", summary="s")
        with patch.object(rq, "_load_prompts", return_value={}):
            rating = await evaluate_source(client, source, "Q?")
        self.assertEqual(rating, 5.0)
        self.assertIn(
            "Rate the credibility", client.analyze_market.await_args.kwargs["market_description"]
        )

    async def test_grpc_failure_degrades_to_none(self):
        client = _FakeClient()
        client.analyze_market = AsyncMock(side_effect=RuntimeError("grpc down"))
        source = rq.EvidenceSource(name="src", domain="d", summary="s")
        self.assertIsNone(await evaluate_source(client, source, "Q?"))

    async def test_non_dict_answer_degrades_to_none(self):
        client = _FakeClient()
        client.analyze_market = AsyncMock(return_value=None)
        source = rq.EvidenceSource(name="src", domain="d", summary="s")
        self.assertIsNone(await evaluate_source(client, source, "Q?"))

    async def test_unknown_age_is_described_not_guessed(self):
        self.assertIn("unknown", rq._describe_age(None))
        self.assertIn("hours", rq._describe_age(12.0))
        self.assertIn("days", rq._describe_age(72.0))


class BlendTests(unittest.TestCase):
    def test_blend_pulls_toward_the_observed_signal(self):
        self.assertEqual(rq._blend(50, 0.5, 0.0), 50)
        self.assertEqual(rq._blend(50, 1.0, 1.0), 100)
        self.assertEqual(rq._blend(50, 0.0, 1.0), 0)

    def test_blend_stays_inside_the_score_range(self):
        self.assertEqual(rq._blend(95, 1.0, 1.0), 100)
        self.assertEqual(rq._blend(5, 0.0, 1.0), 0)


if __name__ == "__main__":
    unittest.main()
