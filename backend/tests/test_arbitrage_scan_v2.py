"""Phase 0 (issue #9): scanner output is indicative, versioned and non-executable."""

import json
import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.analytics import arbitrage_service as arb
from app.services.analytics.cross_venue_arb import mismatches_from_rows

GUARANTEE = re.compile(r"(?<!not )guarantee", re.I)


def _pair(question="Will the Fed cut rates at the December meeting?", **right):
    base = {
        "market_id": "m",
        "question": question,
        "yes_price": 0.42,
        "no_price": 0.58,
        "liquidity": 100.0,
        "volume_24h": 10.0,
        "url": "u",
        "is_real_money": True,
        "end_date": "2026-12-10T00:00:00+00:00",
        "fetched_at": "2026-11-01T00:00:00+00:00",
    }
    left = {**base, "venue": "kalshi"}
    other = {**base, "venue": "predictit", "market_id": "p", "yes_price": 0.55, **right}
    return [left, other]


class ComplementSignalTests(unittest.TestCase):
    def setUp(self):
        self.signal = arb._check_complement_arbitrage(
            {"condition_id": "0xc", "question": "Q?", "outcomePrices": [0.45, 0.50]}
        )

    def test_gross_gap_replaces_profit_wording(self):
        s = self.signal
        self.assertEqual(s["schema_version"], "arb-scan-v2")
        self.assertEqual(s["signal_class"], "complement_signal")
        self.assertEqual(s["gross_price_gap_pct_points"], 5.0)
        self.assertEqual(s["label"], "Indicative price gap (not guaranteed profit)")
        self.assertEqual(set(s["badges"]), {"INDICATIVE_ONLY", "NOT_EXECUTABLE", "UNKNOWN_FEES"})

    def test_legacy_field_kept_and_marked_deprecated(self):
        self.assertEqual(self.signal["profit_pct"], self.signal["gross_price_gap_pct_points"])
        self.assertIn("profit_pct", self.signal["deprecated_fields"])
        self.assertIn("not a return on capital", self.signal["deprecated_fields"]["profit_pct"])
        self.assertEqual(self.signal["type"], "complement")  # legacy discriminator unchanged

    def test_no_guaranteed_language(self):
        self.assertIsNone(GUARANTEE.search(json.dumps(self.signal)))


class SpreadSignalTests(unittest.TestCase):
    def test_wide_spread_is_not_labeled_arbitrage(self):
        s = arb._check_spread_arbitrage({"condition_id": "0xc", "bestBid": 0.40, "bestAsk": 0.50})
        self.assertEqual(s["signal_class"], "spread_signal")
        self.assertIn("LIQUIDITY_SIGNAL_NOT_ARBITRAGE", s["badges"])
        self.assertIn("NOT_EXECUTABLE", s["badges"])
        self.assertNotIn("profit_pct", s)
        self.assertIn("not arbitrage", s["label"])
        self.assertEqual(s["type"], "spread")


class ScanCandidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_records_indicative_candidates_only(self):
        arb._arb_candidates.clear()
        markets = [
            {"condition_id": "0xa", "question": "A?", "outcomePrices": [0.45, 0.5]},
            {"condition_id": "0xb", "question": "B?", "outcomePrices": [0.5, 0.5]},
            {"condition_id": "0xc", "question": "C?", "bestBid": 0.3, "bestAsk": 0.5},
        ]
        service = MagicMock()
        service.get_active_markets = AsyncMock(return_value=markets)
        with patch(
            "app.services.market_data.polymarket_service.get_polymarket_service",
            return_value=service,
        ):
            found = await arb.scan_for_arbitrage()
        self.assertEqual({o["signal_class"] for o in found}, {"complement_signal", "spread_signal"})
        candidates = arb.get_recent_candidates()
        self.assertEqual(len(candidates), 1)  # spread signal never becomes a candidate
        self.assertEqual(candidates[0]["state"], "INDICATIVE")
        self.assertEqual(candidates[0]["condition_ids"], ["0xa"])
        self.assertIsNone(candidates[0]["executable_profit"])
        self.assertIsNone(GUARANTEE.search(json.dumps([found, candidates])))
        self.assertIs(arb.get_candidate(candidates[0]["candidate_id"]) is not None, True)
        arb._arb_candidates.clear()

    def test_candidate_store_is_bounded(self):
        arb._arb_candidates.clear()
        for i in range(arb.MAX_CANDIDATES + 25):
            m = {"condition_id": f"0x{i:04x}", "question": "q", "outcomePrices": [0.4, 0.5]}
            arb.record_indicative_candidate(arb._check_complement_arbitrage(m))
        self.assertEqual(len(arb._arb_candidates), arb.MAX_CANDIDATES)
        arb._arb_candidates.clear()

    def test_candidate_without_condition_is_skipped(self):
        arb._arb_candidates.clear()
        m = {"question": "q", "outcomePrices": [0.4, 0.5]}
        arb.record_indicative_candidate(arb._check_complement_arbitrage(m))
        self.assertEqual(arb.get_recent_candidates(), [])


class CrossVenueReviewOnlyTests(unittest.TestCase):
    def test_title_match_stays_review_only_with_both_rules(self):
        mismatch = mismatches_from_rows(_pair())[0]
        self.assertEqual(mismatch["classification"], "REVIEW_ONLY")
        self.assertFalse(mismatch["executable"])
        self.assertIn("REVIEW_ONLY", mismatch["badges"])
        self.assertEqual(mismatch["equivalence"]["status"], "REVIEW_ONLY")
        self.assertIn("ORACLE_UNVERIFIED", mismatch["equivalence"]["reasons"])
        sides = {s["venue"]: s for s in mismatch["rules_for_review"]}
        self.assertEqual(set(sides), {"kalshi", "predictit"})
        self.assertTrue(all(s["question"] and s["end_date"] for s in sides.values()))
        self.assertEqual(mismatch["gross_price_gap_pct_points"], mismatch["edge_pct"])
        self.assertIsNone(GUARANTEE.search(json.dumps(mismatch)))

    def test_play_money_leg_non_executable_class(self):
        rows = _pair(is_real_money=False)
        mismatch = mismatches_from_rows(rows, include_play_money=True)[0]
        self.assertEqual(mismatch["classification"], "NON_EXECUTABLE")
        self.assertEqual(mismatch["equivalence"]["status"], "NON_EXECUTABLE")
        self.assertFalse(mismatch["executable"])
        self.assertFalse(mismatch["executable_signal"])


if __name__ == "__main__":
    unittest.main()
