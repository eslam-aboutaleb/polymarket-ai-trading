"""Coverage tests for ``app.services.leaderboard_service``.

All external I/O is mocked: ``httpx.AsyncClient`` is replaced with a mock
whose ``get`` returns canned responses, and the module-level caches are
cleared between tests. Covers parsing, normalisation, pagination, ranking,
merge/enrichment logic, win-rate computation, persistence and the
background refresh loop.
"""

import asyncio
import json
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import leaderboard_service as lbs

FALLBACK_IMG = "https://polymarket-upload.s3.us-east-2.amazonaws.com/fallback-image.png"


def _resp(status=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=json_data)
    r.text = text
    return r


def _mock_client(responses):
    client = MagicMock()
    client.get = AsyncMock(side_effect=list(responses))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _entry(wallet, pnl=100.0, vol=50.0, rank=1, name="Trader"):
    return {
        "address": wallet,
        "display_name": name,
        "profit_loss": pnl,
        "volume": vol,
        "rank": rank,
        "pnl_24h": 0.0,
        "pnl_7d": 0.0,
        "pnl_30d": 0.0,
        "volume_24h": 0.0,
        "trade_count": 0,
        "markets_traded": 0,
        "win_rate": 0.0,
        "positions_value": 0.0,
    }


def _api_item(wallet, pnl=100.0, vol=50.0, rank=1, name="Trader"):
    return {
        "proxyWallet": wallet,
        "userName": name,
        "pnl": pnl,
        "vol": vol,
        "rank": rank,
        "profileImage": "https://img.png",
        "xUsername": "xuser",
        "verifiedBadge": True,
    }


def _profile_html(queries):
    payload = json.dumps({"props": {"pageProps": {"dehydratedState": {"queries": queries}}}})
    return f'<script id="__NEXT_DATA__" type="application/json">{payload}</script>'


async def _cancel_sleep(interval):
    raise asyncio.CancelledError()


class LeaderboardTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        lbs._leaderboard_cache.clear()
        lbs._enrichment_cache.clear()
        lbs._trader_trades_cache.clear()
        lbs._profile_stats_cache.clear()


class TestParseNextData(LeaderboardTestCase):
    def test_empty_html(self):
        self.assertIsNone(lbs._parse_next_data(""))

    def test_no_marker(self):
        self.assertIsNone(lbs._parse_next_data("<html><body>hi</body></html>"))

    def test_no_script_start(self):
        self.assertIsNone(lbs._parse_next_data('id="__NEXT_DATA__"'))

    def test_no_tag_end(self):
        self.assertIsNone(lbs._parse_next_data('<script id="__NEXT_DATA__"'))

    def test_no_script_end(self):
        self.assertIsNone(lbs._parse_next_data('<script id="__NEXT_DATA__">'))

    def test_empty_payload(self):
        self.assertIsNone(lbs._parse_next_data('<script id="__NEXT_DATA__"></script>'))

    def test_invalid_json(self):
        html = '<script id="__NEXT_DATA__">{broken</script>'
        self.assertIsNone(lbs._parse_next_data(html))

    def test_valid_json(self):
        html = '<script id="__NEXT_DATA__">{"a": 1}</script>'
        self.assertEqual(lbs._parse_next_data(html), {"a": 1})


class TestExtractLeaderboard(LeaderboardTestCase):
    def test_extract_all_datasets(self):
        next_data = {
            "props": {
                "pageProps": {
                    "dehydratedState": {
                        "queries": [
                            {
                                "queryKey": ["/leaderboard", "volume", "30d", 1, "overall", None],
                                "state": {"data": [{"v": 1}]},
                            },
                            {
                                "queryKey": ["/leaderboard", "profit", "30d", 1, "overall", None],
                                "state": {"data": [{"p": 1}]},
                            },
                            {
                                "queryKey": ["/leaderboard", "biggestWins", "30d", 20, "overall"],
                                "state": {"data": [{"w": 1}]},
                            },
                            {"queryKey": ["/other"], "state": {"data": [{"x": 1}]}},
                            {"queryKey": [], "state": {"data": []}},
                            {
                                "queryKey": ["/leaderboard"],
                                "state": {"data": "not-a-list"},
                            },
                            {
                                "queryKey": ["/leaderboard", "volume", "30d", 1, "overall", None],
                                "state": {"data": "not-a-list"},
                            },
                        ]
                    }
                }
            }
        }
        result = lbs._extract_leaderboard_from_next_data(next_data)
        self.assertEqual(result["volume"], [{"v": 1}])
        self.assertEqual(result["profit"], [{"p": 1}])
        self.assertEqual(result["biggestWins"], [{"w": 1}])

    def test_extract_attribute_error(self):
        result = lbs._extract_leaderboard_from_next_data({"props": "not-a-dict"})
        self.assertEqual(result, {"volume": [], "profit": [], "biggestWins": []})

    def test_extract_empty(self):
        result = lbs._extract_leaderboard_from_next_data({})
        self.assertEqual(result, {"volume": [], "profit": [], "biggestWins": []})


class TestNormalizeApiEntry(LeaderboardTestCase):
    def test_basic(self):
        item = _api_item("0xABC", pnl="100.5", vol="50.25", rank=3, name="Alice")
        entry = lbs._normalize_api_entry(item, rank=0, period="all_time")
        self.assertEqual(entry["address"], "0xabc")
        self.assertEqual(entry["display_name"], "Alice")
        self.assertEqual(entry["profit_loss"], 100.5)
        self.assertEqual(entry["volume"], 50.25)
        self.assertEqual(entry["rank"], 3)
        self.assertEqual(entry["profile_image"], "https://img.png")
        self.assertEqual(entry["x_username"], "xuser")
        self.assertTrue(entry["verified_badge"])
        self.assertEqual(entry["pnl_24h"], 0.0)
        self.assertEqual(entry["pnl_7d"], 0.0)
        self.assertEqual(entry["pnl_30d"], 0.0)
        self.assertEqual(entry["trade_count"], 0)
        self.assertEqual(entry["win_rate"], 0.0)

    def test_rank_override(self):
        self.assertEqual(lbs._normalize_api_entry({}, rank=7)["rank"], 7)

    def test_name_cleanup(self):
        entry = lbs._normalize_api_entry({"userName": "0xAbC123456789-1769439463256"})
        self.assertEqual(entry["display_name"], "0xAbC1234567...")

    def test_name_fallbacks(self):
        self.assertEqual(lbs._normalize_api_entry({"name": "Bob"})["display_name"], "Bob")
        self.assertEqual(lbs._normalize_api_entry({"pseudonym": "Carol"})["display_name"], "Carol")

    def test_fallback_image_nulled(self):
        entry = lbs._normalize_api_entry({"profileImage": FALLBACK_IMG})
        self.assertIsNone(entry["profile_image"])

    def test_optimized_image(self):
        entry = lbs._normalize_api_entry({"profileImageOptimized": "https://img.png"})
        self.assertEqual(entry["profile_image"], "https://img.png")

    def test_period_24h(self):
        entry = lbs._normalize_api_entry({"pnl": 10}, period="24h")
        self.assertEqual(entry["pnl_24h"], 10.0)
        self.assertEqual(entry["pnl_7d"], 10.0)
        self.assertEqual(entry["pnl_30d"], 0.0)

    def test_period_7d(self):
        entry = lbs._normalize_api_entry({"pnl": 10}, period="7d")
        self.assertEqual(entry["pnl_24h"], 0.0)
        self.assertEqual(entry["pnl_7d"], 10.0)

    def test_period_30d(self):
        entry = lbs._normalize_api_entry({"pnl": 10}, period="30d")
        self.assertEqual(entry["pnl_30d"], 10.0)
        self.assertEqual(entry["pnl_24h"], 0.0)

    def test_vol_fallback_to_volume(self):
        self.assertEqual(lbs._normalize_api_entry({"volume": "77.0"})["volume"], 77.0)


class TestFetchFromApi(LeaderboardTestCase):
    async def test_success_sorted_and_ranked(self):
        client = _mock_client(
            [_resp(200, [_api_item("0xabc", pnl=100.0), _api_item("0xdef", pnl=200.0)])]
        )
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_fetch_volume_rankings", AsyncMock(return_value=[])),
        ):
            result = await lbs._fetch_from_api(limit=10, period="all_time")
        self.assertEqual(result[0]["address"], "0xdef")
        self.assertEqual(result[0]["rank"], 1)
        self.assertEqual(result[1]["address"], "0xabc")
        self.assertEqual(result[1]["rank"], 2)

    async def test_pagination(self):
        page1 = [_api_item(f"0x{i:040x}", pnl=float(i)) for i in range(50)]
        page2 = [_api_item(f"0x{i:040x}", pnl=float(i)) for i in range(50, 60)]
        client = _mock_client([_resp(200, page1), _resp(200, page2)])
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_fetch_volume_rankings", AsyncMock(return_value=[])),
        ):
            result = await lbs._fetch_from_api(limit=60, period="all_time")
        self.assertEqual(len(result), 60)
        self.assertEqual(client.get.call_count, 2)

    async def test_non_200_breaks(self):
        client = _mock_client([_resp(500, [])])
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_fetch_volume_rankings", AsyncMock(return_value=[])),
        ):
            result = await lbs._fetch_from_api(limit=10)
        self.assertEqual(result, [])

    async def test_non_list_data_breaks(self):
        client = _mock_client([_resp(200, {"not": "a list"})])
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_fetch_volume_rankings", AsyncMock(return_value=[])),
        ):
            result = await lbs._fetch_from_api(limit=10)
        self.assertEqual(result, [])

    async def test_empty_data_breaks(self):
        client = _mock_client([_resp(200, [])])
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_fetch_volume_rankings", AsyncMock(return_value=[])),
        ):
            result = await lbs._fetch_from_api(limit=10)
        self.assertEqual(result, [])

    async def test_exception_breaks(self):
        client = _mock_client([])
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_fetch_volume_rankings", AsyncMock(return_value=[])),
        ):
            result = await lbs._fetch_from_api(limit=10)
        self.assertEqual(result, [])

    async def test_empty_address_filtered(self):
        client = _mock_client([_resp(200, [_api_item(""), _api_item("0xabc")])])
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_fetch_volume_rankings", AsyncMock(return_value=[])),
        ):
            result = await lbs._fetch_from_api(limit=10)
        self.assertEqual(len(result), 1)

    async def test_volume_merge(self):
        client = _mock_client([_resp(200, [_api_item("0xabc", pnl=100.0, vol=50.0)])])
        vol_data = [{"address": "0xabc", "volume": 999.0}]
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_fetch_volume_rankings", AsyncMock(return_value=vol_data)),
        ):
            result = await lbs._fetch_from_api(limit=10)
        self.assertEqual(result[0]["volume"], 999.0)

    async def test_volume_merge_skips_zero_and_missing(self):
        client = _mock_client(
            [_resp(200, [_api_item("0xabc", vol=50.0), _api_item("0xdef", vol=60.0)])]
        )
        vol_data = [{"address": "0xabc", "volume": 0}]
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_fetch_volume_rankings", AsyncMock(return_value=vol_data)),
        ):
            result = await lbs._fetch_from_api(limit=10)
        by_addr = {e["address"]: e for e in result}
        self.assertEqual(by_addr["0xabc"]["volume"], 50.0)
        self.assertEqual(by_addr["0xdef"]["volume"], 60.0)

    async def test_limit_truncation(self):
        items = [_api_item(f"0x{i:040x}", pnl=float(100 - i)) for i in range(10)]
        client = _mock_client([_resp(200, items)])
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_fetch_volume_rankings", AsyncMock(return_value=[])),
        ):
            result = await lbs._fetch_from_api(limit=5)
        self.assertEqual(len(result), 5)

    async def test_period_mapping(self):
        client = _mock_client([_resp(200, [])])
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_fetch_volume_rankings", AsyncMock(return_value=[])),
        ):
            await lbs._fetch_from_api(limit=10, period="24h")
        params = client.get.call_args.kwargs["params"]
        self.assertEqual(params["timePeriod"], "day")
        self.assertEqual(params["orderBy"], "PNL")
        self.assertEqual(params["category"], "overall")

    async def test_unknown_period_maps_to_all(self):
        client = _mock_client([_resp(200, [])])
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_fetch_volume_rankings", AsyncMock(return_value=[])),
        ):
            await lbs._fetch_from_api(limit=10, period="unknown")
        self.assertEqual(client.get.call_args.kwargs["params"]["timePeriod"], "all")


class TestFetchVolumeRankings(LeaderboardTestCase):
    async def test_success(self):
        items = [
            {"proxyWallet": "0xABC", "vol": "100.5"},
            {"proxyWallet": "0xDEF", "vol": 200},
            {"vol": 50},
        ]
        client = _mock_client([_resp(200, items)])
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            result = await lbs._fetch_volume_rankings(
                [{"address": "a"}, {"address": "b"}], "month", {}
            )
        self.assertEqual(
            result,
            [{"address": "0xabc", "volume": 100.5}, {"address": "0xdef", "volume": 200.0}],
        )

    async def test_non_200_breaks(self):
        client = _mock_client([_resp(500, [])])
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            result = await lbs._fetch_volume_rankings([{"address": "a"}], "month", {})
        self.assertEqual(result, [])

    async def test_empty_json_breaks(self):
        client = _mock_client([_resp(200, [])])
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            result = await lbs._fetch_volume_rankings([{"address": "a"}], "month", {})
        self.assertEqual(result, [])

    async def test_non_list_data_breaks(self):
        client = _mock_client([_resp(200, {"a": 1})])
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            result = await lbs._fetch_volume_rankings([{"address": "a"}], "month", {})
        self.assertEqual(result, [])

    async def test_exception_breaks(self):
        client = _mock_client([])
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            result = await lbs._fetch_volume_rankings([{"address": "a"}], "month", {})
        self.assertEqual(result, [])

    async def test_pagination(self):
        pnl_entries = [{"address": f"0x{i:040x}"} for i in range(60)]
        page1 = [{"proxyWallet": f"0x{i:040x}", "vol": 1} for i in range(50)]
        page2 = [{"proxyWallet": f"0x{i:040x}", "vol": 1} for i in range(50, 60)]
        client = _mock_client([_resp(200, page1), _resp(200, page2)])
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            result = await lbs._fetch_volume_rankings(pnl_entries, "month", {})
        self.assertEqual(len(result), 60)
        self.assertEqual(client.get.call_count, 2)

    async def test_short_page_breaks(self):
        pnl_entries = [{"address": f"0x{i:040x}"} for i in range(10)]
        items = [{"proxyWallet": f"0x{i:040x}", "vol": 1} for i in range(5)]
        client = _mock_client([_resp(200, items)])
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            result = await lbs._fetch_volume_rankings(pnl_entries, "month", {})
        self.assertEqual(len(result), 5)
        self.assertEqual(client.get.call_count, 1)


class TestFetchFromPolymarketPage(LeaderboardTestCase):
    async def test_success(self):
        next_data = {
            "props": {
                "pageProps": {
                    "dehydratedState": {
                        "queries": [
                            {
                                "queryKey": ["/leaderboard", "volume", "30d", 1, "overall", None],
                                "state": {"data": [{"proxyWallet": "0xabc"}]},
                            },
                            {
                                "queryKey": ["/leaderboard", "profit", "30d", 1, "overall", None],
                                "state": {"data": [{"proxyWallet": "0xdef"}]},
                            },
                            {
                                "queryKey": ["/leaderboard", "biggestWins", "30d", 20, "overall"],
                                "state": {"data": [{"proxyWallet": "0xghi"}]},
                            },
                        ]
                    }
                }
            }
        }
        html = (
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script>'
        )
        client = _mock_client([_resp(200, text=html)])
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            result = await lbs._fetch_from_polymarket_page(limit=50, period="7d")
        self.assertEqual(len(result["volume"]), 1)
        self.assertEqual(len(result["profit"]), 1)
        self.assertEqual(len(result["biggestWins"]), 1)
        self.assertIn("/leaderboard/overall/weekly/profit", client.get.call_args.args[0])

    async def test_non_200(self):
        client = _mock_client([_resp(500, text="err")])
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            result = await lbs._fetch_from_polymarket_page(limit=50, period="all_time")
        self.assertEqual(result, {"volume": [], "profit": [], "biggestWins": []})

    async def test_no_next_data(self):
        client = _mock_client([_resp(200, text="<html>no data</html>")])
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            result = await lbs._fetch_from_polymarket_page(limit=50, period="30d")
        self.assertEqual(result, {"volume": [], "profit": [], "biggestWins": []})

    async def test_exception(self):
        client = _mock_client([])
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            result = await lbs._fetch_from_polymarket_page(limit=50)
        self.assertEqual(result, {"volume": [], "profit": [], "biggestWins": []})

    async def test_period_url_mapping(self):
        client = _mock_client([_resp(200, text="<html>x</html>")])
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            await lbs._fetch_from_polymarket_page(limit=50, period="30d")
        self.assertIn("/leaderboard/overall/monthly/profit", client.get.call_args.args[0])


class TestMergeDatasets(LeaderboardTestCase):
    def test_merge_profit_and_volume(self):
        datasets = {
            "profit": [{"proxyWallet": "0xABC", "name": "Alice", "pnl": "100.0", "rank": 1}],
            "volume": [
                {"proxyWallet": "0xABC", "volume": "500.0"},
                {"proxyWallet": "0xDEF", "name": "Bob", "volume": "300.0"},
            ],
            "biggestWins": [],
        }
        result = lbs._merge_datasets(datasets, sort_by="profit", limit=10, period="all_time")
        self.assertEqual(result[0]["address"], "0xabc")
        self.assertEqual(result[0]["display_name"], "Alice")
        self.assertEqual(result[0]["volume"], 500.0)
        self.assertEqual(result[0]["profit_loss"], 100.0)
        self.assertEqual(result[1]["address"], "0xdef")
        self.assertEqual(result[1]["display_name"], "Bob")
        self.assertEqual(result[1]["profit_loss"], 0.0)
        self.assertEqual(result[1]["rank"], 2)

    def test_name_and_image_fallback_from_volume(self):
        datasets = {
            "profit": [{"proxyWallet": "0xabc", "pnl": 10}],
            "volume": [
                {
                    "proxyWallet": "0xabc",
                    "name": "VolName",
                    "profileImage": "https://img.png",
                    "volume": 5,
                }
            ],
        }
        result = lbs._merge_datasets(datasets, sort_by="profit", limit=10)
        self.assertEqual(result[0]["display_name"], "VolName")
        self.assertEqual(result[0]["profile_image"], "https://img.png")

    def test_sort_by_volume(self):
        datasets = {
            "profit": [
                {"proxyWallet": "0xaaa", "pnl": 10, "volume": 100},
                {"proxyWallet": "0xbbb", "pnl": 20, "volume": 50},
            ],
            "volume": [],
        }
        result = lbs._merge_datasets(datasets, sort_by="volume", limit=10)
        self.assertEqual(result[0]["address"], "0xaaa")
        self.assertEqual(result[1]["address"], "0xbbb")

    def test_limit(self):
        datasets = {
            "profit": [{"proxyWallet": f"0x{i:040x}", "pnl": i} for i in range(5)],
            "volume": [],
        }
        result = lbs._merge_datasets(datasets, sort_by="profit", limit=2)
        self.assertEqual(len(result), 2)

    def test_period_24h(self):
        datasets = {"profit": [{"proxyWallet": "0xabc", "pnl": 10}], "volume": []}
        result = lbs._merge_datasets(datasets, sort_by="profit", limit=10, period="24h")
        self.assertEqual(result[0]["pnl_24h"], 10.0)
        self.assertEqual(result[0]["pnl_7d"], 10.0)
        self.assertEqual(result[0]["pnl_30d"], 0.0)

    def test_period_30d(self):
        datasets = {"profit": [{"proxyWallet": "0xabc", "pnl": 10}], "volume": []}
        result = lbs._merge_datasets(datasets, sort_by="profit", limit=10, period="30d")
        self.assertEqual(result[0]["pnl_30d"], 10.0)
        self.assertEqual(result[0]["pnl_24h"], 0.0)

    def test_name_cleanup_and_fallback_image(self):
        datasets = {
            "profit": [
                {
                    "proxyWallet": "0xabc",
                    "name": "0xAbC123456789-12345",
                    "profileImage": FALLBACK_IMG,
                    "pnl": 1,
                }
            ],
            "volume": [],
        }
        result = lbs._merge_datasets(datasets, sort_by="profit", limit=10)
        self.assertEqual(result[0]["display_name"], "0xAbC1234567...")
        self.assertIsNone(result[0]["profile_image"])

    def test_address_and_winrank_fallback(self):
        datasets = {
            "profit": [{"address": "0xABC", "winRank": 4, "pnl": 1}],
            "volume": [],
        }
        result = lbs._merge_datasets(datasets, sort_by="profit", limit=10)
        self.assertEqual(result[0]["address"], "0xabc")
        # entries are re-ranked by position after sorting
        self.assertEqual(result[0]["rank"], 1)

    def test_empty_datasets(self):
        result = lbs._merge_datasets({}, sort_by="profit", limit=10)
        self.assertEqual(result, [])

    def test_entries_without_wallet_skipped(self):
        datasets = {"profit": [{"name": "NoWallet", "pnl": 1}], "volume": []}
        result = lbs._merge_datasets(datasets, sort_by="profit", limit=10)
        self.assertEqual(result, [])


class TestEnrichSingleTrader(LeaderboardTestCase):
    async def test_cache_hit(self):
        wallet = "0x" + "a" * 40
        cached_data = {
            "win_rate": 99.0,
            "markets_traded": 1,
            "positions_value": 1.0,
            "position_count": 1,
        }
        lbs._enrichment_cache[wallet] = {"data": cached_data, "ts": datetime.now(UTC).timestamp()}
        client = MagicMock()
        result = await lbs._enrich_single_trader(client, wallet, {})
        self.assertEqual(result, cached_data)
        client.get.assert_not_called()

    async def test_full_analysis(self):
        wallet = "0x" + "a" * 40
        activity = [
            {"type": "TRADE", "side": "BUY", "usdcSize": 100, "conditionId": "m1"},
            {"type": "REDEEM", "usdcSize": 150, "conditionId": "m1"},
            {"type": "TRADE", "side": "BUY", "usdcSize": 100, "conditionId": "m2"},
            {"type": "MERGE", "conditionId": "m2"},
            {"type": "TRADE", "side": "BUY", "usdcSize": 100, "conditionId": "m3"},
            {"type": "TRADE", "side": "SELL", "usdcSize": 120, "conditionId": "m3"},
            {"type": "TRADE", "side": "BUY", "usdcSize": 100, "conditionId": "m4"},
            {"type": "TRADE", "side": "SELL", "usdcSize": 80, "conditionId": "m4"},
            {"type": "MERGE"},
        ]
        positions = [{"currentValue": 10.5}, {"value": 20.5}]
        client = _mock_client([_resp(200, activity), _resp(200, positions)])
        result = await lbs._enrich_single_trader(client, wallet, {})
        self.assertEqual(result["win_rate"], 50.0)
        self.assertEqual(result["markets_traded"], 4)
        self.assertEqual(result["position_count"], 2)
        self.assertEqual(result["positions_value"], 31.0)
        self.assertIn(wallet, lbs._enrichment_cache)

    async def test_activity_non_200(self):
        wallet = "0x" + "a" * 40
        client = _mock_client([_resp(500, []), _resp(200, [])])
        result = await lbs._enrich_single_trader(client, wallet, {})
        self.assertEqual(result["win_rate"], 0.0)
        self.assertEqual(result["markets_traded"], 0)

    async def test_activity_non_list(self):
        wallet = "0x" + "a" * 40
        client = _mock_client([_resp(200, {"foo": 1}), _resp(200, [])])
        result = await lbs._enrich_single_trader(client, wallet, {})
        self.assertEqual(result["markets_traded"], 0)

    async def test_activity_exception(self):
        wallet = "0x" + "a" * 40
        client = _mock_client([])
        client.get = AsyncMock(side_effect=[RuntimeError("boom"), _resp(200, [])])
        result = await lbs._enrich_single_trader(client, wallet, {})
        self.assertEqual(result["markets_traded"], 0)

    async def test_positions_exception(self):
        wallet = "0x" + "a" * 40
        client = _mock_client([])
        client.get = AsyncMock(side_effect=[_resp(200, []), RuntimeError("boom")])
        result = await lbs._enrich_single_trader(client, wallet, {})
        self.assertEqual(result["position_count"], 0)

    async def test_positions_non_200(self):
        wallet = "0x" + "a" * 40
        client = _mock_client([_resp(200, []), _resp(500, [])])
        result = await lbs._enrich_single_trader(client, wallet, {})
        self.assertEqual(result["position_count"], 0)

    async def test_positions_dict_payload(self):
        wallet = "0x" + "a" * 40
        client = _mock_client([_resp(200, []), _resp(200, {"positions": [{"size": 5}]})])
        result = await lbs._enrich_single_trader(client, wallet, {})
        self.assertEqual(result["position_count"], 1)
        self.assertEqual(result["positions_value"], 5.0)


class TestEnrichEntries(LeaderboardTestCase):
    async def test_empty(self):
        self.assertEqual(await lbs._enrich_entries([]), [])

    async def test_success(self):
        entries = [
            {
                "address": "0x" + "a" * 40,
                "win_rate": 0.0,
                "markets_traded": 0,
                "positions_value": 0.0,
            }
        ]
        client = _mock_client([])
        enrichment = {"win_rate": 50.0, "markets_traded": 3, "positions_value": 10.0}
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_enrich_single_trader", AsyncMock(return_value=enrichment)),
        ):
            result = await lbs._enrich_entries(entries)
        self.assertEqual(result[0]["win_rate"], 50.0)
        self.assertEqual(result[0]["markets_traded"], 3)
        self.assertEqual(result[0]["positions_value"], 10.0)

    async def test_exception_result(self):
        entries = [{"address": "0x" + "a" * 40, "win_rate": 0.0}]
        client = _mock_client([])
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_enrich_single_trader", AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            result = await lbs._enrich_entries(entries)
        self.assertEqual(result[0]["win_rate"], 0.0)

    async def test_zero_enrichment_ignored(self):
        entries = [
            {
                "address": "0x" + "a" * 40,
                "win_rate": 0.0,
                "markets_traded": 0,
                "positions_value": 0.0,
            }
        ]
        client = _mock_client([])
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(
                lbs,
                "_enrich_single_trader",
                AsyncMock(return_value={"win_rate": 0, "markets_traded": 0, "positions_value": 0}),
            ),
        ):
            result = await lbs._enrich_entries(entries)
        self.assertEqual(result[0]["win_rate"], 0.0)

    async def test_max_enrich_limit(self):
        entries = [
            {"address": f"0x{i:040x}", "win_rate": 0.0, "markets_traded": 0, "positions_value": 0.0}
            for i in range(3)
        ]
        client = _mock_client([])
        enrichment = {"win_rate": 50.0, "markets_traded": 3, "positions_value": 10.0}
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_enrich_single_trader", AsyncMock(return_value=enrichment)),
        ):
            result = await lbs._enrich_entries(entries, max_enrich=2)
        self.assertEqual(result[0]["win_rate"], 50.0)
        self.assertEqual(result[1]["win_rate"], 50.0)
        self.assertEqual(result[2]["win_rate"], 0.0)
        self.assertEqual(len(result), 3)

    async def test_entries_without_address(self):
        entries = [{"address": ""}, {"address": "0x" + "a" * 40, "win_rate": 0.0}]
        client = _mock_client([])
        enrichment = {"win_rate": 50.0, "markets_traded": 1, "positions_value": 1.0}
        with (
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
            patch.object(lbs, "_enrich_single_trader", AsyncMock(return_value=enrichment)),
        ):
            result = await lbs._enrich_entries(entries)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["win_rate"], 50.0)


class TestApplyCachedEnrichment(LeaderboardTestCase):
    def test_hit(self):
        wallet = "0x" + "a" * 40
        lbs._enrichment_cache[wallet] = {
            "data": {"win_rate": 50.0, "markets_traded": 3, "positions_value": 10.0},
            "ts": datetime.now(UTC).timestamp(),
        }
        entries = [
            {"address": wallet, "win_rate": 0.0, "markets_traded": 0, "positions_value": 0.0}
        ]
        result = lbs._apply_cached_enrichment(entries)
        self.assertEqual(result[0]["win_rate"], 50.0)
        self.assertEqual(result[0]["markets_traded"], 3)
        self.assertEqual(result[0]["positions_value"], 10.0)

    def test_miss(self):
        entries = [{"address": "0x" + "a" * 40, "win_rate": 0.0}]
        result = lbs._apply_cached_enrichment(entries)
        self.assertEqual(result[0]["win_rate"], 0.0)

    def test_expired(self):
        wallet = "0x" + "a" * 40
        lbs._enrichment_cache[wallet] = {
            "data": {"win_rate": 50.0, "markets_traded": 3, "positions_value": 10.0},
            "ts": datetime.now(UTC).timestamp() - 10000,
        }
        entries = [{"address": wallet, "win_rate": 0.0}]
        result = lbs._apply_cached_enrichment(entries)
        self.assertEqual(result[0]["win_rate"], 0.0)


class TestBackgroundEnrich(LeaderboardTestCase):
    async def test_success_updates_cache(self):
        entries = [_entry("0x" + "a" * 40)]
        with patch.object(lbs, "_enrich_entries", AsyncMock(return_value=entries)):
            await lbs._background_enrich_and_cache(entries, "test:key", 1)
        self.assertIn("test:key", lbs._leaderboard_cache)
        self.assertEqual(lbs._leaderboard_cache["test:key"]["data"], entries)

    async def test_failure_swallowed(self):
        with patch.object(lbs, "_enrich_entries", AsyncMock(side_effect=RuntimeError("boom"))):
            await lbs._background_enrich_and_cache([], "test:key2", 1)
        self.assertNotIn("test:key2", lbs._leaderboard_cache)


class TestFetchLeaderboard(LeaderboardTestCase):
    async def test_cache_hit(self):
        entries = [_entry("0x" + "a" * 40)]
        lbs._leaderboard_cache["leaderboard:all_time:10"] = {
            "data": entries,
            "ts": datetime.now(UTC).timestamp(),
        }
        result = await lbs.fetch_leaderboard(limit=10, period="all_time")
        self.assertEqual(result, entries)

    async def test_api_success(self):
        entries = [_entry("0x" + "a" * 40)]
        with (
            patch.object(lbs, "_fetch_from_api", AsyncMock(return_value=entries)),
            patch.object(lbs, "_background_enrich_and_cache", AsyncMock()),
        ):
            result = await lbs.fetch_leaderboard(limit=10, period="all_time")
            await asyncio.sleep(0)
        self.assertEqual(result, entries)
        self.assertIn("leaderboard:all_time:10", lbs._leaderboard_cache)

    async def test_api_fallback_to_html(self):
        datasets = {"volume": [], "profit": [], "biggestWins": []}
        merged = [_entry("0x" + "a" * 40)]
        merge_mock = MagicMock(return_value=merged)
        with (
            patch.object(lbs, "_fetch_from_api", AsyncMock(return_value=[])),
            patch.object(lbs, "_fetch_from_polymarket_page", AsyncMock(return_value=datasets)),
            patch.object(lbs, "_merge_datasets", merge_mock),
            patch.object(lbs, "_background_enrich_and_cache", AsyncMock()),
        ):
            result = await lbs.fetch_leaderboard(limit=10, period="all_time")
            await asyncio.sleep(0)
        self.assertEqual(result, merged)
        merge_mock.assert_called_once_with(
            datasets, sort_by="all_time", limit=10, period="all_time"
        )

    async def test_both_empty(self):
        with (
            patch.object(lbs, "_fetch_from_api", AsyncMock(return_value=[])),
            patch.object(
                lbs,
                "_fetch_from_polymarket_page",
                AsyncMock(return_value={"volume": [], "profit": [], "biggestWins": []}),
            ),
            patch.object(lbs, "_merge_datasets", MagicMock(return_value=[])),
        ):
            result = await lbs.fetch_leaderboard(limit=10, period="all_time")
        self.assertEqual(result, [])
        self.assertNotIn("leaderboard:all_time:10", lbs._leaderboard_cache)

    async def test_cached_enrichment_applied(self):
        wallet = "0x" + "a" * 40
        lbs._enrichment_cache[wallet] = {
            "data": {"win_rate": 75.0, "markets_traded": 5, "positions_value": 42.0},
            "ts": datetime.now(UTC).timestamp(),
        }
        entries = [
            {
                "address": wallet,
                "profit_loss": 1.0,
                "win_rate": 0.0,
                "markets_traded": 0,
                "positions_value": 0.0,
            }
        ]
        with (
            patch.object(lbs, "_fetch_from_api", AsyncMock(return_value=entries)),
            patch.object(lbs, "_background_enrich_and_cache", AsyncMock()),
        ):
            result = await lbs.fetch_leaderboard(limit=10, period="all_time")
            await asyncio.sleep(0)
        self.assertEqual(result[0]["win_rate"], 75.0)
        self.assertEqual(result[0]["markets_traded"], 5)
        self.assertEqual(result[0]["positions_value"], 42.0)


class TestUpsertWinners(LeaderboardTestCase):
    def test_new_winners(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        entries = [
            {
                "address": "0x" + "a" * 40,
                "display_name": "Alice",
                "profile_image": "https://img.png",
                "profit_loss": 100.0,
                "pnl_24h": 1.0,
                "pnl_7d": 2.0,
                "pnl_30d": 3.0,
                "volume": 50.0,
                "volume_24h": 5.0,
                "trade_count": 10,
                "markets_traded": 4,
                "win_rate": 50.0,
                "positions_value": 20.0,
                "rank": 1,
            },
            {"address": "0x" + "b" * 40, "display_name": "Bob"},
        ]
        count = lbs.upsert_winners(db, entries)
        self.assertEqual(count, 2)
        self.assertEqual(db.add.call_count, 2)
        db.commit.assert_called_once()

    def test_skips_bad_addresses(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        entries = [{"address": ""}, {"address": "0xshort"}, {"address": "0x" + "c" * 40}]
        count = lbs.upsert_winners(db, entries)
        self.assertEqual(count, 1)

    def test_existing_winner_updated(self):
        db = MagicMock()
        existing = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = existing
        entries = [
            {"address": "0x" + "a" * 40, "display_name": "Alice", "profit_loss": 5.0, "rank": 3}
        ]
        count = lbs.upsert_winners(db, entries)
        self.assertEqual(count, 1)
        db.add.assert_not_called()
        self.assertEqual(existing.leaderboard_rank, 3)
        self.assertEqual(existing.total_pnl, 5.0)


class TestFetchTraderProfile(LeaderboardTestCase):
    async def test_with_cached_leaderboard_data(self):
        wallet = "0xABC"
        cached_entry = {
            "address": "0xabc",
            "display_name": "Alice",
            "profit_loss": 100.0,
            "volume": 50.0,
            "markets_traded": 5,
            "win_rate": 60.0,
            "profile_image": "https://img.png",
        }
        lbs._leaderboard_cache["leaderboard:all_time:50"] = {
            "data": [cached_entry],
            "ts": datetime.now(UTC).timestamp(),
        }
        activity = [{"type": "TRADE"}, {"type": "SELL"}, {"type": "BUY"}]
        client = _mock_client([_resp(200, activity)])
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            profile = await lbs.fetch_trader_profile(wallet)
        self.assertEqual(profile["wallet_address"], "0xabc")
        self.assertEqual(profile["display_name"], "Alice")
        self.assertEqual(profile["profit_loss"], 100.0)
        self.assertEqual(profile["volume"], 50.0)
        self.assertEqual(profile["markets_traded"], 5)
        self.assertEqual(profile["win_rate"], 60.0)
        self.assertEqual(profile["profile_image"], "https://img.png")
        self.assertEqual(profile["recent_trades"], activity)

    async def test_no_cached_match(self):
        client = _mock_client([_resp(200, [])])
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            profile = await lbs.fetch_trader_profile("0xabc")
        self.assertIsNone(profile["display_name"])
        self.assertEqual(profile["profit_loss"], 0.0)
        self.assertEqual(profile["recent_trades"], [])

    async def test_activity_non_200(self):
        client = _mock_client([_resp(500, [])])
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            profile = await lbs.fetch_trader_profile("0xabc")
        self.assertEqual(profile["recent_trades"], [])

    async def test_activity_non_list(self):
        client = _mock_client([_resp(200, {"foo": 1})])
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            profile = await lbs.fetch_trader_profile("0xabc")
        self.assertEqual(profile["recent_trades"], [])

    async def test_activity_exception(self):
        client = _mock_client([])
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            profile = await lbs.fetch_trader_profile("0xabc")
        self.assertEqual(profile["recent_trades"], [])


class TestScrapeProfileStats(LeaderboardTestCase):
    async def test_success(self):
        wallet = "0x" + "a" * 40
        queries = [
            {
                "queryKey": ["/api/profile/volume", wallet],
                "state": {"data": {"pnl": "100.5", "amount": "500.25"}},
            },
            {
                "queryKey": ["/api/profile/user-stats", wallet],
                "state": {"data": {"trades": "10", "largestWin": "50", "joinDate": "2024-01-01"}},
            },
            {
                "queryKey": ["/api/profile/marketsTraded", wallet],
                "state": {"data": {"traded": "5"}},
            },
            {"queryKey": ["/other", "x"], "state": {"data": None}},
        ]
        client = _mock_client([_resp(200, text=_profile_html(queries))])
        result = await lbs._scrape_profile_stats(client, wallet)
        self.assertEqual(result["pnl"], 100.5)
        self.assertEqual(result["volume"], 500.25)
        self.assertEqual(result["trades_count"], 10)
        self.assertEqual(result["largest_win"], 50.0)
        self.assertEqual(result["join_date"], "2024-01-01")
        self.assertEqual(result["markets_traded"], 5)
        self.assertIn(wallet, lbs._profile_stats_cache)

    async def test_cache_hit(self):
        wallet = "0x" + "a" * 40
        cached = {"pnl": 1.0}
        lbs._profile_stats_cache[wallet] = {"data": cached, "ts": datetime.now(UTC).timestamp()}
        client = MagicMock()
        result = await lbs._scrape_profile_stats(client, wallet)
        self.assertEqual(result, cached)
        client.get.assert_not_called()

    async def test_non_200(self):
        client = _mock_client([_resp(500, text="err")])
        result = await lbs._scrape_profile_stats(client, "0x" + "a" * 40)
        self.assertEqual(result, {})
        self.assertNotIn("0x" + "a" * 40, lbs._profile_stats_cache)

    async def test_no_next_data(self):
        client = _mock_client([_resp(200, text="<html>no data</html>")])
        result = await lbs._scrape_profile_stats(client, "0x" + "a" * 40)
        self.assertEqual(result, {})

    async def test_exception(self):
        client = _mock_client([])
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        result = await lbs._scrape_profile_stats(client, "0x" + "a" * 40)
        self.assertEqual(result, {})
        self.assertIn("0x" + "a" * 40, lbs._profile_stats_cache)


class TestFetchTraderTrades(LeaderboardTestCase):
    async def test_success_with_profile_overrides(self):
        wallet = "0xABC"
        profile_stats = {
            "pnl": 100.5,
            "volume": 500.25,
            "trades_count": 10,
            "markets_traded": 5,
            "largest_win": 50.0,
            "join_date": "2024-01-01",
        }
        activity = [{"type": "TRADE", "side": "BUY", "usdcSize": 100, "conditionId": "m1"}]
        client = _mock_client([_resp(200, activity)])
        with (
            patch.object(lbs, "_scrape_profile_stats", AsyncMock(return_value=profile_stats)),
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
        ):
            result = await lbs.fetch_trader_trades(wallet)
        self.assertEqual(result["trades"], [])
        self.assertEqual(result["activity"], activity)
        self.assertEqual(result["positions"], [])
        self.assertEqual(result["profile_stats"], profile_stats)
        self.assertEqual(result["stats"]["total_pnl"], 100.5)
        self.assertEqual(result["stats"]["total_volume"], 500.25)
        self.assertEqual(result["stats"]["total_trades"], 10)
        self.assertEqual(result["stats"]["unique_markets"], 5)
        self.assertEqual(result["stats"]["largest_win"], 50.0)
        self.assertEqual(result["stats"]["first_trade_date"], "2024-01-01")
        self.assertIn("TRADER STATISTICS", result["trade_summary"])
        result2 = await lbs.fetch_trader_trades(wallet)
        self.assertEqual(result2, result)

    async def test_empty_profile_stats(self):
        activity = [
            {
                "type": "TRADE",
                "side": "BUY",
                "usdcSize": 100,
                "conditionId": "m1",
                "timestamp": "2024-01-01T00:00:00Z",
            }
        ]
        client = _mock_client([_resp(200, activity)])
        with (
            patch.object(lbs, "_scrape_profile_stats", AsyncMock(return_value={})),
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
        ):
            result = await lbs.fetch_trader_trades("0xabc")
        self.assertEqual(result["stats"]["total_pnl"], 0.0)
        self.assertEqual(result["stats"]["total_trades"], 1)

    async def test_activity_exception(self):
        client = _mock_client([])
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch.object(lbs, "_scrape_profile_stats", AsyncMock(return_value={})),
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
        ):
            result = await lbs.fetch_trader_trades("0xabc")
        self.assertEqual(result["activity"], [])

    async def test_max_trades_cap(self):
        activity = [
            {"type": "TRADE", "side": "BUY", "usdcSize": 1, "conditionId": f"m{i}"}
            for i in range(5)
        ]
        client = _mock_client([_resp(200, activity)])
        with (
            patch.object(lbs, "_scrape_profile_stats", AsyncMock(return_value={})),
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
        ):
            result = await lbs.fetch_trader_trades("0xabc", max_trades=2)
        self.assertEqual(len(result["activity"]), 2)

    async def test_activity_non_200(self):
        client = _mock_client([_resp(500, [])])
        with (
            patch.object(lbs, "_scrape_profile_stats", AsyncMock(return_value={})),
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
        ):
            result = await lbs.fetch_trader_trades("0xabc")
        self.assertEqual(result["activity"], [])

    async def test_activity_non_list(self):
        client = _mock_client([_resp(200, {"foo": 1})])
        with (
            patch.object(lbs, "_scrape_profile_stats", AsyncMock(return_value={})),
            patch.object(lbs.httpx, "AsyncClient", return_value=client),
        ):
            result = await lbs.fetch_trader_trades("0xabc")
        self.assertEqual(result["activity"], [])


class TestComputeTradeStats(LeaderboardTestCase):
    def test_empty(self):
        stats = lbs._compute_trade_stats([], [], [])
        self.assertEqual(stats["total_trades"], 0)
        self.assertEqual(stats["winning_trades"], 0)
        self.assertEqual(stats["win_rate"], 0.0)
        self.assertEqual(stats["total_volume"], 0.0)
        self.assertIsNone(stats["first_trade_date"])
        self.assertEqual(stats["position_count"], 0)

    def test_clob_trades(self):
        trades = [
            {
                "size": "10",
                "price": "0.5",
                "market": "clob1",
                "timestamp": "2024-01-01T00:00:00Z",
                "side": "BUY",
            },
            {
                "size": 5,
                "price": 0,
                "condition_id": "clob1",
                "created_at": "2024-01-02T00:00:00Z",
                "type": "SELL",
            },
            {
                "amount": 3,
                "asset_id": "clob2",
                "match_time": 1700000000,
                "side": "BUY",
                "price": 0.25,
            },
        ]
        stats = lbs._compute_trade_stats(trades, [], [])
        self.assertEqual(stats["total_volume"], 10.75)
        self.assertEqual(stats["avg_trade_size"], 3.58)
        self.assertEqual(stats["largest_trade"], 5.0)
        self.assertEqual(stats["unique_markets"], 2)
        self.assertEqual(stats["avg_buy_price"], 0.375)
        self.assertEqual(stats["total_trades"], 3)
        self.assertEqual(stats["active_days"], 3)
        self.assertEqual(stats["trade_frequency"], "1.0 trades/day")

    def test_activity_analysis(self):
        activity = [
            {
                "type": "TRADE",
                "side": "BUY",
                "usdcSize": "100",
                "price": "0.5",
                "conditionId": "m1",
                "timestamp": "2024-01-01T00:00:00Z",
                "title": "Trump election 2024",
            },
            {
                "type": "TRADE",
                "side": "BUY",
                "usdcSize": 50,
                "conditionId": "m2",
                "timestamp": 1700000000,
                "question": "Bitcoin price prediction",
            },
            {
                "type": "REDEEM",
                "usdcSize": 200,
                "conditionId": "m1",
                "timestamp": "2024-02-01T00:00:00Z",
                "marketTitle": "NBA championship",
            },
            {"type": "MERGE", "conditionId": "m2"},
            {
                "type": "REWARD",
                "usdcSize": 10,
                "conditionId": "m3",
                "title": "Fed interest rate decision",
            },
            {
                "type": "TRADE",
                "side": "SELL",
                "usdcSize": 30,
                "conditionId": "m4",
                "title": "Apple ai news",
            },
        ]
        stats = lbs._compute_trade_stats([], activity, [{"x": 1}])
        self.assertEqual(
            stats["market_categories"],
            {"politics": 1, "crypto": 1, "sports": 1, "economics": 1, "tech": 1},
        )
        self.assertEqual(stats["winning_trades"], 1)
        self.assertEqual(stats["losing_trades"], 1)
        self.assertEqual(stats["win_rate"], 50.0)
        self.assertEqual(stats["total_pnl"], 100.0)
        self.assertEqual(stats["unique_markets"], 4)
        self.assertEqual(stats["position_count"], 1)
        self.assertEqual(stats["total_volume"], 380.0)
        self.assertEqual(stats["avg_trade_size"], 95.0)
        self.assertEqual(stats["largest_trade"], 200.0)
        self.assertEqual(stats["avg_buy_price"], 0.5)
        self.assertEqual(stats["total_trades"], 6)
        self.assertEqual(stats["trade_frequency"], "2.0 trades/day")

    def test_sell_profit_and_loss(self):
        activity = [
            {"type": "TRADE", "side": "BUY", "usdcSize": 100, "conditionId": "m1"},
            {"type": "TRADE", "side": "SELL", "usdcSize": 150, "conditionId": "m1"},
            {"type": "TRADE", "side": "BUY", "usdcSize": 100, "conditionId": "m2"},
            {"type": "TRADE", "side": "SELL", "usdcSize": 80, "conditionId": "m2"},
        ]
        stats = lbs._compute_trade_stats([], activity, [])
        self.assertEqual(stats["winning_trades"], 1)
        self.assertEqual(stats["losing_trades"], 1)
        self.assertEqual(stats["total_pnl"], 30.0)
        self.assertEqual(stats["win_rate"], 50.0)

    def test_date_variants(self):
        activity = [
            {
                "type": "TRADE",
                "side": "BUY",
                "usdcSize": 1,
                "conditionId": "m1",
                "timestamp": 1700000000,
            },
            {
                "type": "TRADE",
                "side": "BUY",
                "usdcSize": 1,
                "conditionId": "m2",
                "timestamp": 1700000000.5,
            },
            {
                "type": "TRADE",
                "side": "BUY",
                "usdcSize": 1,
                "conditionId": "m3",
                "timestamp": "2024-01-01T00:00:00Z",
            },
            {
                "type": "TRADE",
                "side": "BUY",
                "usdcSize": 1,
                "conditionId": "m4",
                "timestamp": "1700000000",
            },
            {
                "type": "TRADE",
                "side": "BUY",
                "usdcSize": 1,
                "conditionId": "m5",
                "timestamp": "not-a-date",
            },
            {
                "type": "TRADE",
                "side": "BUY",
                "usdcSize": 1,
                "conditionId": "m6",
                "createdAt": "2024-03-01T00:00:00Z",
            },
            {
                "type": "TRADE",
                "side": "BUY",
                "usdcSize": 1,
                "conditionId": "m7",
                "created_at": "2024-04-01T00:00:00Z",
            },
        ]
        stats = lbs._compute_trade_stats([], activity, [])
        self.assertEqual(stats["active_days"], 4)
        self.assertIsNotNone(stats["first_trade_date"])
        self.assertIsNotNone(stats["last_trade_date"])

    def test_no_timestamp_ignored(self):
        activity = [
            {"type": "TRADE", "side": "BUY", "usdcSize": 1, "conditionId": "m1", "timestamp": None}
        ]
        stats = lbs._compute_trade_stats([], activity, [])
        self.assertIsNone(stats["first_trade_date"])
        self.assertEqual(stats["trade_frequency"], "")


class TestBuildTradeSummary(LeaderboardTestCase):
    def test_full_summary(self):
        activity = [
            {
                "type": "TRADE",
                "side": "BUY",
                "usdcSize": 100,
                "price": 0.5,
                "conditionId": "m1",
                "timestamp": "2024-01-01T00:00:00Z",
                "title": "Trump election",
                "outcome": "Yes",
            }
        ]
        stats = lbs._compute_trade_stats([], activity, [])
        summary = lbs._build_trade_summary([], activity, stats)
        self.assertIn("=== TRADER STATISTICS (COMPUTED FROM REAL DATA) ===", summary)
        self.assertIn("Total Trades: 1", summary)
        self.assertIn("=== RECENT TRADES (last 1) ===", summary)
        self.assertIn("[TRADE]", summary)
        self.assertIn("Trump election", summary)
        self.assertIn("$100.00", summary)
        self.assertIn("@ $0.500", summary)
        self.assertIn("(outcome: Yes)", summary)

    def test_trades_fallback(self):
        trades = [
            {
                "type": "TRADE",
                "side": "BUY",
                "size": 10,
                "price": 0.5,
                "market": "m1",
                "timestamp": "2024-01-01T00:00:00Z",
                "title": "Market A",
            }
        ]
        stats = lbs._compute_trade_stats(trades, [], [])
        summary = lbs._build_trade_summary(trades, [], stats)
        self.assertIn("=== RECENT TRADES (last 1) ===", summary)
        self.assertIn("Market A", summary)

    def test_extras(self):
        stats = lbs._compute_trade_stats([], [], [])
        stats["first_trade_date"] = "2024-01-01T00:00:00+00:00"
        stats["last_trade_date"] = "2024-06-01T00:00:00+00:00"
        stats["avg_buy_price"] = 0.456
        stats["market_categories"] = {"politics": 3, "crypto": 1}
        summary = lbs._build_trade_summary([], [], stats)
        self.assertIn("First Trade: 2024-01-01", summary)
        self.assertIn("Last Trade: 2024-06-01", summary)
        self.assertIn("Average Buy Price: $0.456", summary)
        self.assertIn("=== MARKET CATEGORY BREAKDOWN ===", summary)
        self.assertIn("Politics: 3 trades", summary)
        self.assertIn("Crypto: 1 trades", summary)


class TestEdgeBranches(LeaderboardTestCase):
    def test_apply_cached_enrichment_zero_values(self):
        wallet = "0x" + "a" * 40
        lbs._enrichment_cache[wallet] = {
            "data": {"win_rate": 0, "markets_traded": 0, "positions_value": 0},
            "ts": datetime.now(UTC).timestamp(),
        }
        entries = [
            {"address": wallet, "win_rate": 0.0, "markets_traded": 0, "positions_value": 0.0}
        ]
        result = lbs._apply_cached_enrichment(entries)
        self.assertEqual(result[0]["win_rate"], 0.0)

    async def test_scrape_profile_stats_non_matching_query(self):
        wallet = "0x" + "a" * 40
        queries = [
            {"queryKey": ["/api/other", wallet], "state": {"data": {"foo": "bar"}}},
        ]
        client = _mock_client([_resp(200, text=_profile_html(queries))])
        result = await lbs._scrape_profile_stats(client, wallet)
        self.assertEqual(result, {})

    async def test_fetch_trader_profile_cached_no_match(self):
        lbs._leaderboard_cache["leaderboard:all_time:50"] = {
            "data": [{"address": "0x" + "b" * 40, "display_name": "Other"}],
            "ts": datetime.now(UTC).timestamp(),
        }
        client = _mock_client([_resp(200, [])])
        with patch.object(lbs.httpx, "AsyncClient", return_value=client):
            profile = await lbs.fetch_trader_profile("0x" + "a" * 40)
        self.assertIsNone(profile["display_name"])

    def test_compute_trade_stats_edge_branches(self):
        trades = [{"size": 5, "price": 0.5, "side": "BUY"}]
        activity = [
            {"type": "MERGE"},
            {"type": "REDEEM", "usdcSize": 10, "conditionId": "m1"},
            {
                "type": "TRADE",
                "side": "BUY",
                "usdcSize": 5,
                "conditionId": "m2",
                "title": "Random unrelated topic",
            },
        ]
        stats = lbs._compute_trade_stats(trades, activity, [])
        self.assertEqual(stats["winning_trades"], 1)
        self.assertEqual(stats["market_categories"], {})
        self.assertEqual(stats["unique_markets"], 2)
        self.assertEqual(stats["avg_buy_price"], 0.5)

    def test_compute_trade_stats_no_trade_sizes(self):
        stats = lbs._compute_trade_stats([], [{"type": "MERGE", "conditionId": "m1"}], [])
        self.assertEqual(stats["total_volume"], 0.0)
        self.assertEqual(stats["avg_trade_size"], 0.0)
        self.assertEqual(stats["largest_trade"], 0.0)
        self.assertEqual(stats["win_rate"], 0.0)

    def test_build_trade_summary_without_timestamp(self):
        stats = lbs._compute_trade_stats([], [], [])
        summary = lbs._build_trade_summary([], [{"type": "TRADE", "title": "Market X"}], stats)
        self.assertIn("[TRADE]", summary)
        self.assertIn("Market X", summary)

    async def test_refresh_background_empty_entries(self):
        with (
            patch.object(lbs, "fetch_leaderboard", AsyncMock(return_value=[])),
            patch.object(lbs.asyncio, "sleep", _cancel_sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await lbs.refresh_leaderboard_background(db=MagicMock(), interval=300)


class TestSafeFloat(LeaderboardTestCase):
    def test_safe_float(self):
        self.assertEqual(lbs._safe_float(None), 0.0)
        self.assertEqual(lbs._safe_float(None, 5.0), 5.0)
        self.assertEqual(lbs._safe_float("3.14"), 3.14)
        self.assertEqual(lbs._safe_float(7), 7.0)
        self.assertEqual(lbs._safe_float("abc"), 0.0)
        self.assertEqual(lbs._safe_float("abc", 1.5), 1.5)


class TestRefreshBackground(LeaderboardTestCase):
    async def test_with_db(self):
        entries = [_entry("0x" + "a" * 40)]
        db = MagicMock()
        upsert_mock = MagicMock(return_value=1)
        with (
            patch.object(lbs, "fetch_leaderboard", AsyncMock(return_value=entries)),
            patch.object(lbs, "upsert_winners", upsert_mock),
            patch.object(lbs.asyncio, "sleep", _cancel_sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await lbs.refresh_leaderboard_background(db=db, interval=300)
        upsert_mock.assert_called_once_with(db, entries)

    async def test_no_db(self):
        entries = [_entry("0x" + "a" * 40)]
        with (
            patch.object(lbs, "fetch_leaderboard", AsyncMock(return_value=entries)),
            patch.object(lbs, "upsert_winners", MagicMock()) as upsert_mock,
            patch.object(lbs.asyncio, "sleep", _cancel_sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await lbs.refresh_leaderboard_background(db=None, interval=300)
        upsert_mock.assert_not_called()

    async def test_db_error_swallowed(self):
        entries = [_entry("0x" + "a" * 40)]
        db = MagicMock()
        with (
            patch.object(lbs, "fetch_leaderboard", AsyncMock(return_value=entries)),
            patch.object(lbs, "upsert_winners", MagicMock(side_effect=RuntimeError("db down"))),
            patch.object(lbs.asyncio, "sleep", _cancel_sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await lbs.refresh_leaderboard_background(db=db, interval=300)

    async def test_fetch_error_swallowed(self):
        with (
            patch.object(lbs, "fetch_leaderboard", AsyncMock(side_effect=RuntimeError("api down"))),
            patch.object(lbs.asyncio, "sleep", _cancel_sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await lbs.refresh_leaderboard_background(db=MagicMock(), interval=300)


if __name__ == "__main__":
    unittest.main()
