"""Additional coverage tests for the CTF events whale monitor.

Complements tests/test_ctf_events_service.py (which covers decoding,
candidate expansion, whale filtering, dedup and WS failover) by
exercising the remaining surface:

* the periodic refresh jobs (market maps, whale set, followed /
  watchlist maps) including their failure isolation,
* block-timestamp backfill (cache hit, RPC fetch, non-200, missing
  timestamp, RPC failure, DB failure),
* alert dispatch (unavailable service, sync result, coroutine result,
  dispatch failure),
* the auto-copy routing path (disabled, no config, cond: tokens,
  scheduling, copy-trade execution and its failure modes),
* ``_handle_decoded`` edges (no candidates, block-timestamp task,
  DB rollback),
* ``_handle_ws_message`` edges (non-dict result, undecodable log),
* ``_ws_listen`` success path (subscribe handshake, message loop,
  stop mid-stream), subscribe error replies, backoff doubling/clamping
  and cancellation,
* ``_periodic_refresh`` loop including the suppressed TimeoutError,
* the monitor start/stop lifecycle (already running, lock held,
  task cancellation, task stop errors).
"""

import asyncio
import contextlib
import json
import sys
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import eth_abi
from web3 import Web3

from app.models.followed_trader import FollowedTrader
from app.models.whale_config import WhaleConfig
from app.services import ctf_events_service as svc

WHALE = "0x" + "1" * 40
OTHER = "0x" + "2" * 40
OPERATOR = "0x" + "3" * 40
COLLATERAL = "0x" + "4" * 40

TRANSFER_SINGLE_TOPIC = Web3.keccak(
    text="TransferSingle(address,address,address,uint256,uint256)"
).hex()
TRANSFER_BATCH_TOPIC = Web3.keccak(
    text="TransferBatch(address,address,address,uint256[],uint256[])"
).hex()
POSITION_SPLIT_TOPIC = Web3.keccak(
    text="PositionSplit(address,address,bytes32,bytes32,uint256[],uint256)"
).hex()
POSITION_MERGE_TOPIC = Web3.keccak(
    text="PositionMerge(address,address,bytes32,bytes32,uint256[],uint256)"
).hex()
REDEMPTION_TOPIC = Web3.keccak(
    text="Redemption(address,address,bytes32,bytes32,uint256[],uint256)"
).hex()

CONDITION_ID = bytes.fromhex("ab" * 32)
PARENT_COLLECTION_ID = bytes.fromhex("cd" * 32)


def _padded(address: str) -> str:
    return "0x" + "0" * 24 + address[2:].lower()


def _base_log(topics, data, tx_hash="0xabc", log_index=0):
    return {
        "address": svc.CONDITIONAL_TOKENS_ADDRESS,
        "topics": topics,
        "data": data,
        "blockNumber": None,
        "blockHash": "0x" + "11" * 32,
        "transactionHash": tx_hash,
        "transactionIndex": hex(0),
        "logIndex": hex(log_index),
    }


def _transfer_single_log(from_wallet, to_wallet, token_id, value, **kwargs):
    topics = [
        TRANSFER_SINGLE_TOPIC,
        _padded(OPERATOR),
        _padded(from_wallet),
        _padded(to_wallet),
    ]
    data = "0x" + eth_abi.encode(["uint256", "uint256"], [token_id, value]).hex()
    return _base_log(topics, data, **kwargs)


def _transfer_batch_log(from_wallet, to_wallet, ids, values, **kwargs):
    topics = [
        TRANSFER_BATCH_TOPIC,
        _padded(OPERATOR),
        _padded(from_wallet),
        _padded(to_wallet),
    ]
    data = "0x" + eth_abi.encode(["uint256[]", "uint256[]"], [ids, values]).hex()
    return _base_log(topics, data, **kwargs)


class _ServiceStateTestCase(unittest.IsolatedAsyncioTestCase):
    """Snapshot and restore the module's mutable global state."""

    def setUp(self):
        self._saved = (
            svc._running,
            svc._monitor_task,
            svc._refresh_task,
            svc._stop_event,
        )
        self._reset_state()

    def tearDown(self):
        (
            svc._running,
            svc._monitor_task,
            svc._refresh_task,
            svc._stop_event,
        ) = self._saved
        self._reset_state()

    @staticmethod
    def _reset_state():
        svc._whale_wallets = set()
        svc._followed_map = {}
        svc._watchlist_map = {}
        svc._token_market_map = {}
        svc._condition_market_map = {}
        svc.invalidate_config_cache()
        with contextlib.suppress(Exception):
            svc._cache.clear()


# ── Decoding / candidate edge cases ───────────────────────
class DecodingEdgeTests(unittest.TestCase):
    """Branches the base test file does not reach."""

    def test_load_abi(self):
        abi = svc._load_abi("conditional_tokens.json")
        self.assertIsInstance(abi, list)
        self.assertTrue(abi)

    def test_event_topic(self):
        entry = {
            "name": "TransferSingle",
            "inputs": [{"type": "address"}, {"type": "uint256"}],
        }
        self.assertEqual(
            svc._event_topic(entry),
            Web3.keccak(text="TransferSingle(address,uint256)").hex(),
        )

    def test_decode_log_empty_topics(self):
        self.assertIsNone(svc.decode_log({"topics": [], "data": "0x"}))
        self.assertIsNone(svc.decode_log({"topics": None, "data": "0x"}))

    def test_decode_log_missing_optional_fields(self):
        log = _transfer_single_log(OTHER, WHALE, 123, 5000)
        log["transactionHash"] = None
        log["logIndex"] = None
        log["blockNumber"] = None
        decoded = svc.decode_log(log)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["tx_hash"], "")
        self.assertIsNone(decoded["log_index"])
        self.assertIsNone(decoded["block_number"])

    def test_to_hex_string_bytearray(self):
        self.assertEqual(svc._to_hex_string(bytearray(b"\xab\xcd")), "0xabcd")

    def test_hex_to_int_type_error(self):
        self.assertIsNone(svc._hex_to_int([1, 2]))

    def test_transfer_batch_candidates(self):
        decoded = {
            "event": "TransferBatch",
            "args": {"from": OTHER, "to": WHALE, "ids": [1, 2], "values": [10, 20]},
        }
        candidates = svc._whale_candidates(decoded)
        self.assertEqual(len(candidates), 4)
        buys = [c for c in candidates if c["wallet"] == WHALE]
        sells = [c for c in candidates if c["wallet"] == OTHER]
        self.assertEqual([c["token_id"] for c in buys], ["1", "2"])
        self.assertEqual([c["size"] for c in buys], [10.0, 20.0])
        self.assertEqual({c["side"] for c in buys}, {"buy"})
        self.assertEqual({c["side"] for c in sells}, {"sell"})

    def test_transfer_batch_mismatched_lengths_zips_shortest(self):
        decoded = {
            "event": "TransferBatch",
            "args": {"from": OTHER, "to": WHALE, "ids": [1, 2, 3], "values": [10]},
        }
        candidates = svc._whale_candidates(decoded)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["token_id"], "1")

    def test_transfer_batch_empty_payloads(self):
        decoded = {
            "event": "TransferBatch",
            "args": {"from": OTHER, "to": WHALE, "ids": None, "values": None},
        }
        self.assertEqual(svc._whale_candidates(decoded), [])

    def test_transfer_single_missing_value_defaults_to_zero(self):
        decoded = {
            "event": "TransferSingle",
            "args": {"from": OTHER, "to": WHALE, "id": 7, "value": None},
        }
        candidates = svc._whale_candidates(decoded)
        self.assertEqual(candidates[0]["size"], 0.0)

    def test_transfer_with_empty_wallets_yields_no_candidates(self):
        decoded = {"event": "TransferSingle", "args": {"from": "", "to": "", "id": 1, "value": 5}}
        self.assertEqual(svc._whale_candidates(decoded), [])

    def test_position_merge_candidate(self):
        decoded = {
            "event": "PositionMerge",
            "args": {
                "stakeOwner": WHALE,
                "conditionId": CONDITION_ID,
                "amount": 1_000_000,
            },
        }
        candidates = svc._whale_candidates(decoded)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["side"], "close")
        self.assertEqual(candidates[0]["event_type"], "position_merge")
        self.assertEqual(candidates[0]["size"], 1.0)

    def test_position_split_without_owner_or_condition(self):
        decoded = {
            "event": "PositionSplit",
            "args": {"stakeOwner": "", "conditionId": None, "amount": 100},
        }
        self.assertEqual(svc._whale_candidates(decoded), [])

    def test_redemption_without_owner(self):
        decoded = {
            "event": "Redemption",
            "args": {"stakeOwner": None, "conditionId": CONDITION_ID, "payout": 100},
        }
        self.assertEqual(svc._whale_candidates(decoded), [])

    def test_redemption_with_string_condition_id(self):
        decoded = {
            "event": "Redemption",
            "args": {"stakeOwner": WHALE, "conditionId": "0x" + "ab" * 32, "payout": 100},
        }
        candidates = svc._whale_candidates(decoded)
        self.assertEqual(candidates[0]["token_id"], "cond:0x" + "ab" * 32)


# ── Periodic refresh jobs ─────────────────────────────────
class RefreshMarketMapsTests(_ServiceStateTestCase):
    async def test_refresh_market_maps_happy_path(self):
        markets = [
            {
                "id": "m1",
                "question": "Q1",
                "condition_id": "0xAA",
                "outcomePrices": ["0.6"],
                "tokens": [{"token_id": "t1"}],
            },
            {
                "condition_id": "0xbb",
                "groupItemTitle": "G2",
                "outcomePrices": [],
                "tokens": [{"tokenId": "t2"}],
            },
            {
                "_event_title": "E3",
                "conditionId": "0xCC",
                "outcomePrices": ["not-a-float"],
            },
            {"id": "m4"},
        ]
        active = AsyncMock(return_value=markets[:2])
        newest = AsyncMock(return_value=markets[2:])
        service = MagicMock()
        service.get_active_markets = active
        service.get_newest_markets = newest
        with patch(
            "app.services.polymarket_service.get_polymarket_service",
            return_value=service,
        ):
            await svc._refresh_market_maps()

        active.assert_awaited_once_with(limit=500)
        newest.assert_awaited_once_with(limit=500)

        self.assertEqual(svc._token_market_map["t1"]["market_id"], "m1")
        self.assertEqual(svc._token_market_map["t1"]["question"], "Q1")
        self.assertAlmostEqual(svc._token_market_map["t1"]["price"], 0.6)
        self.assertEqual(svc._token_market_map["t2"]["question"], "G2")
        self.assertAlmostEqual(svc._token_market_map["t2"]["price"], 0.5)
        self.assertEqual(svc._condition_market_map["0xaa"]["market_id"], "m1")
        self.assertEqual(svc._condition_market_map["0xbb"]["question"], "G2")
        self.assertAlmostEqual(svc._condition_market_map["0xcc"]["price"], 0.5)
        self.assertEqual(svc._condition_market_map["0xcc"]["question"], "E3")

    async def test_refresh_market_maps_empty_response(self):
        service = MagicMock()
        service.get_active_markets = AsyncMock(return_value=[])
        service.get_newest_markets = AsyncMock(return_value=[])
        with patch(
            "app.services.polymarket_service.get_polymarket_service",
            return_value=service,
        ):
            await svc._refresh_market_maps()
        self.assertEqual(svc._token_market_map, {})
        self.assertEqual(svc._condition_market_map, {})

    async def test_refresh_market_maps_failure_isolated(self):
        svc._token_market_map = {"keep": {"market_id": "k"}}
        service = MagicMock()
        service.get_active_markets = AsyncMock(side_effect=RuntimeError("api down"))
        service.get_newest_markets = AsyncMock(return_value=[])
        with patch(
            "app.services.polymarket_service.get_polymarket_service",
            return_value=service,
        ):
            await svc._refresh_market_maps()
        self.assertEqual(svc._token_market_map, {"keep": {"market_id": "k"}})


class RefreshWhaleSetTests(_ServiceStateTestCase):
    async def test_refresh_whale_set(self):
        entries = [
            {"address": WHALE},
            {"address": OTHER},
            {"address": None},
            {},
        ]
        with patch(
            "app.services.leaderboard_service.fetch_leaderboard",
            AsyncMock(return_value=entries),
        ) as fetch:
            await svc._refresh_whale_set()
        fetch.assert_awaited_once_with(limit=svc.WHALE_SET_SIZE, period="all_time")
        self.assertEqual(svc._whale_wallets, {WHALE.lower(), OTHER.lower()})

    async def test_refresh_whale_set_failure_isolated(self):
        svc._whale_wallets = {"0xkeep"}
        with patch(
            "app.services.leaderboard_service.fetch_leaderboard",
            AsyncMock(side_effect=RuntimeError("leaderboard down")),
        ):
            await svc._refresh_whale_set()
        self.assertEqual(svc._whale_wallets, {"0xkeep"})


class RefreshFollowedMapTests(_ServiceStateTestCase):
    def test_refresh_followed_map(self):
        db = MagicMock()
        followed_chain = MagicMock()
        followed_chain.filter.return_value.all.return_value = [
            (42, WHALE),
            (43, WHALE),
            (44, OTHER),
        ]
        whale_chain = MagicMock()
        whale_chain.all.return_value = [
            MagicMock(user_id=7, watchlist=[WHALE, OTHER]),
            MagicMock(user_id=8, watchlist=None),
        ]

        def _query(*entities):
            if entities[0] is FollowedTrader.user_id:
                return followed_chain
            return whale_chain

        db.query.side_effect = _query

        with patch.object(svc, "SessionLocal", return_value=db):
            svc._refresh_followed_map()

        self.assertEqual(svc._followed_map, {WHALE: {42, 43}, OTHER: {44}})
        self.assertEqual(svc._watchlist_map, {WHALE: {7}, OTHER: {7}})
        db.close.assert_called_once()

    def test_refresh_followed_map_failure_closes_db(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        with patch.object(svc, "SessionLocal", return_value=db):
            svc._refresh_followed_map()
        db.close.assert_called_once()
        self.assertEqual(svc._followed_map, {})
        self.assertEqual(svc._watchlist_map, {})


class GetUserConfigTests(_ServiceStateTestCase):
    def test_get_user_config_returns_db_row(self):
        db = MagicMock()
        row = WhaleConfig(user_id=5, min_notional=250.0)
        db.query.return_value.filter.return_value.first.return_value = row
        config = svc.get_user_config(5, db)
        self.assertIs(config, row)
        self.assertIn(5, svc._config_cache)


# ── Block timestamp backfill ──────────────────────────────
class UpdateBlockTsTests(_ServiceStateTestCase):
    def _httpx_patch(self, response):
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=False)
        return patch("httpx.AsyncClient", return_value=context), client

    def _db(self, event):
        db = MagicMock()
        db.get.return_value = event
        return db

    async def test_update_block_ts_uses_cached_timestamp(self):
        svc._cache.set(
            "block_ts:999",
            "2024-01-01T00:00:00+00:00",
            ttl_seconds=svc.BLOCK_TS_TTL_SECONDS,
        )
        event = MagicMock()
        event.block_ts = None
        db = self._db(event)
        httpx_patch, client = self._httpx_patch(MagicMock())
        with (
            patch.object(svc, "SessionLocal", return_value=db),
            httpx_patch,
        ):
            await svc._update_block_ts(1, 999)
        client.post.assert_not_awaited()
        self.assertEqual(event.block_ts, datetime.fromisoformat("2024-01-01T00:00:00+00:00"))
        db.commit.assert_called_once()
        db.close.assert_called_once()

    async def test_update_block_ts_fetches_and_caches(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"result": {"timestamp": hex(1700000000)}}
        httpx_patch, client = self._httpx_patch(response)
        event = MagicMock()
        event.block_ts = None
        db = self._db(event)
        with (
            patch.object(svc, "SessionLocal", return_value=db),
            httpx_patch,
        ):
            await svc._update_block_ts(1, 12345)
        client.post.assert_awaited_once()
        payload = client.post.call_args.kwargs["json"]
        self.assertEqual(payload["method"], "eth_getBlockByNumber")
        self.assertEqual(payload["params"], [hex(12345), False])
        expected_ts = datetime.fromtimestamp(1700000000, tz=UTC)
        self.assertEqual(event.block_ts, expected_ts)
        self.assertEqual(svc._cache.get("block_ts:12345"), expected_ts.isoformat())
        db.commit.assert_called_once()

    async def test_update_block_ts_non_200_returns(self):
        response = MagicMock()
        response.status_code = 500
        httpx_patch, _ = self._httpx_patch(response)
        with (
            patch.object(svc, "SessionLocal", return_value=MagicMock()) as session_local,
            httpx_patch,
        ):
            await svc._update_block_ts(1, 12345)
        session_local.assert_not_called()

    async def test_update_block_ts_missing_timestamp_returns(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"result": {}}
        httpx_patch, _ = self._httpx_patch(response)
        with (
            patch.object(svc, "SessionLocal", return_value=MagicMock()) as session_local,
            httpx_patch,
        ):
            await svc._update_block_ts(1, 12345)
        session_local.assert_not_called()

    async def test_update_block_ts_rpc_failure_returns(self):
        httpx_patch, client = self._httpx_patch(MagicMock())
        client.post.side_effect = RuntimeError("rpc down")
        with (
            patch.object(svc, "SessionLocal", return_value=MagicMock()) as session_local,
            httpx_patch,
        ):
            await svc._update_block_ts(1, 12345)
        session_local.assert_not_called()

    async def test_update_block_ts_event_already_has_ts(self):
        svc._cache.set(
            "block_ts:1",
            "2024-01-01T00:00:00+00:00",
            ttl_seconds=svc.BLOCK_TS_TTL_SECONDS,
        )
        event = MagicMock()
        event.block_ts = datetime.now(UTC)
        db = self._db(event)
        with (
            patch.object(svc, "SessionLocal", return_value=db),
            patch("httpx.AsyncClient", MagicMock()),
        ):
            await svc._update_block_ts(1, 1)
        db.commit.assert_not_called()
        db.close.assert_called_once()

    async def test_update_block_ts_event_missing(self):
        svc._cache.set(
            "block_ts:1",
            "2024-01-01T00:00:00+00:00",
            ttl_seconds=svc.BLOCK_TS_TTL_SECONDS,
        )
        db = self._db(None)
        with (
            patch.object(svc, "SessionLocal", return_value=db),
            patch("httpx.AsyncClient", MagicMock()),
        ):
            await svc._update_block_ts(1, 1)
        db.commit.assert_not_called()
        db.close.assert_called_once()

    async def test_update_block_ts_db_failure_rolls_back(self):
        svc._cache.set(
            "block_ts:1",
            "2024-01-01T00:00:00+00:00",
            ttl_seconds=svc.BLOCK_TS_TTL_SECONDS,
        )
        db = MagicMock()
        db.get.side_effect = RuntimeError("db down")
        with (
            patch.object(svc, "SessionLocal", return_value=db),
            patch("httpx.AsyncClient", MagicMock()),
        ):
            await svc._update_block_ts(1, 1)
        db.rollback.assert_called_once()
        db.close.assert_called_once()


# ── Alert dispatch ────────────────────────────────────────
class DispatchAlertTests(_ServiceStateTestCase):
    def test_dispatch_alert_service_unavailable(self):
        with patch.dict(sys.modules, {"app.services.alert_service": None}):
            svc._dispatch_alert(1, {"wallet": WHALE})

    def test_dispatch_alert_sync_result(self):
        with patch("app.services.alert_service.dispatch", MagicMock()) as dispatch:
            svc._dispatch_alert(1, {"wallet": WHALE})
        dispatch.assert_called_once_with("whale_alert", 1, {"wallet": WHALE}, background=True)

    async def test_dispatch_alert_schedules_coroutine(self):
        async def _noop(*args, **kwargs):
            return "done"

        def _dispatch(*args, **kwargs):
            return _noop(*args, **kwargs)

        with patch("app.services.alert_service.dispatch", _dispatch):
            svc._dispatch_alert(1, {"wallet": WHALE})
        await asyncio.sleep(0)

    def test_dispatch_alert_dispatch_failure(self):
        with patch(
            "app.services.alert_service.dispatch",
            MagicMock(side_effect=RuntimeError("boom")),
        ):
            svc._dispatch_alert(1, {"wallet": WHALE})


# ── Auto-copy routing ─────────────────────────────────────
class RunAutoCopyTests(_ServiceStateTestCase):
    async def test_run_auto_copy_service_unavailable(self):
        with patch.dict(sys.modules, {"app.services.copy_trade_service": None}):
            await svc._run_auto_copy(
                1,
                WHALE,
                {"market_id": "m1"},
                {"token_id": "t1", "side": "buy"},
                0.5,
                100.0,
                "0xabc",
            )

    async def test_run_auto_copy_buy_side(self):
        db = MagicMock()
        execute = AsyncMock()
        candidate = {"token_id": "t1", "side": "buy"}
        with (
            patch.object(svc, "SessionLocal", return_value=db),
            patch("app.services.copy_trade_service.execute_copy_trade", execute),
        ):
            await svc._run_auto_copy(
                1,
                WHALE,
                {"market_id": "m1"},
                candidate,
                0.5,
                100.0,
                "0xabc",
            )
        execute.assert_awaited_once_with(
            db=db,
            user_id=1,
            trader_wallet=WHALE,
            market_id="m1",
            token_id="t1",
            side="buy",
            price=0.5,
            trader_amount=100.0,
            idempotency_key=f"whale:0xabc:{WHALE}:t1",
        )
        db.close.assert_called_once()

    async def test_run_auto_copy_open_side_maps_to_buy(self):
        db = MagicMock()
        execute = AsyncMock()
        candidate = {"token_id": "t1", "side": "open"}
        with (
            patch.object(svc, "SessionLocal", return_value=db),
            patch("app.services.copy_trade_service.execute_copy_trade", execute),
        ):
            await svc._run_auto_copy(
                1,
                WHALE,
                {"market_id": "m1"},
                candidate,
                0.5,
                100.0,
                "0xabc",
            )
        self.assertEqual(execute.await_args.kwargs["side"], "buy")

    async def test_run_auto_copy_sell_side(self):
        db = MagicMock()
        execute = AsyncMock()
        candidate = {"token_id": "t1", "side": "sell"}
        with (
            patch.object(svc, "SessionLocal", return_value=db),
            patch("app.services.copy_trade_service.execute_copy_trade", execute),
        ):
            await svc._run_auto_copy(
                1,
                WHALE,
                {"market_id": "m1"},
                candidate,
                0.5,
                100.0,
                "0xabc",
            )
        self.assertEqual(execute.await_args.kwargs["side"], "sell")

    async def test_run_auto_copy_failure_closes_db(self):
        db = MagicMock()
        execute = AsyncMock(side_effect=RuntimeError("copy failed"))
        with (
            patch.object(svc, "SessionLocal", return_value=db),
            patch("app.services.copy_trade_service.execute_copy_trade", execute),
        ):
            await svc._run_auto_copy(
                1,
                WHALE,
                {"market_id": "m1"},
                {"token_id": "t1", "side": "buy"},
                0.5,
                100.0,
                "0xabc",
            )
        db.close.assert_called_once()


class MaybeAutoCopyTests(_ServiceStateTestCase):
    def test_maybe_auto_copy_disabled_globally(self):
        svc._config_cache[1] = WhaleConfig(user_id=1, auto_copy=True)
        with patch.object(svc, "_run_auto_copy", new=AsyncMock()) as run_copy:
            svc._maybe_auto_copy(
                1,
                WHALE,
                {},
                {"token_id": "t1", "side": "buy"},
                0.5,
                1.0,
                "0xabc",
            )
        run_copy.assert_not_awaited()

    def test_maybe_auto_copy_no_config(self):
        with (
            patch.object(svc, "WHALE_AUTO_COPY", True),
            patch.object(svc, "_run_auto_copy", new=AsyncMock()) as run_copy,
        ):
            svc._maybe_auto_copy(
                1,
                WHALE,
                {},
                {"token_id": "t1", "side": "buy"},
                0.5,
                1.0,
                "0xabc",
            )
        run_copy.assert_not_awaited()

    def test_maybe_auto_copy_config_copy_disabled(self):
        svc._config_cache[1] = WhaleConfig(user_id=1, auto_copy=False)
        with (
            patch.object(svc, "WHALE_AUTO_COPY", True),
            patch.object(svc, "_run_auto_copy", new=AsyncMock()) as run_copy,
        ):
            svc._maybe_auto_copy(
                1,
                WHALE,
                {},
                {"token_id": "t1", "side": "buy"},
                0.5,
                1.0,
                "0xabc",
            )
        run_copy.assert_not_awaited()

    def test_maybe_auto_copy_skips_condition_references(self):
        svc._config_cache[1] = WhaleConfig(user_id=1, auto_copy=True)
        candidate = {"token_id": "cond:0x" + "ab" * 32, "side": "open"}
        with (
            patch.object(svc, "WHALE_AUTO_COPY", True),
            patch.object(svc, "_run_auto_copy", new=AsyncMock()) as run_copy,
        ):
            svc._maybe_auto_copy(1, WHALE, {}, candidate, 0.5, 1.0, "0xabc")
        run_copy.assert_not_awaited()

    async def test_maybe_auto_copy_schedules_copy(self):
        svc._config_cache[1] = WhaleConfig(user_id=1, auto_copy=True)
        market_info = {"market_id": "m1"}
        candidate = {"token_id": "t1", "side": "buy"}
        with (
            patch.object(svc, "WHALE_AUTO_COPY", True),
            patch.object(svc, "_run_auto_copy", new=AsyncMock()) as run_copy,
        ):
            svc._maybe_auto_copy(1, WHALE, market_info, candidate, 0.5, 100.0, "0xabc")
            await asyncio.sleep(0)
        run_copy.assert_awaited_once_with(1, WHALE, market_info, candidate, 0.5, 100.0, "0xabc")

    def test_maybe_auto_copy_schedule_failure(self):
        svc._config_cache[1] = WhaleConfig(user_id=1, auto_copy=True)
        with (
            patch.object(svc, "WHALE_AUTO_COPY", True),
            patch.object(svc.asyncio, "get_event_loop", side_effect=RuntimeError("no loop")),
            patch.object(svc, "_run_auto_copy", new=AsyncMock()) as run_copy,
        ):
            svc._maybe_auto_copy(
                1,
                WHALE,
                {},
                {"token_id": "t1", "side": "buy"},
                0.5,
                1.0,
                "0xabc",
            )
        run_copy.assert_not_awaited()


# ── _handle_decoded edges ─────────────────────────────────
class HandleDecodedEdgeTests(_ServiceStateTestCase):
    def _mock_db(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        return db

    async def test_handle_decoded_no_candidates(self):
        decoded = {
            "event": "PositionSplit",
            "args": {"stakeOwner": "", "conditionId": None, "amount": 0},
            "tx_hash": "0xabc",
            "log_index": 0,
            "block_number": None,
        }
        with patch.object(svc, "SessionLocal", return_value=MagicMock()) as session_local:
            await svc._handle_decoded(decoded)
        session_local.assert_not_called()

    async def test_handle_decoded_schedules_block_ts_backfill(self):
        svc._whale_wallets = {WHALE}
        svc._token_market_map = {"123": {"market_id": "m1", "question": "Q", "price": 0.5}}
        decoded = {
            "event": "TransferSingle",
            "args": {"from": OTHER, "to": WHALE, "id": 123, "value": 30000},
            "tx_hash": "0xabc",
            "log_index": 0,
            "block_number": 12345,
        }
        db = self._mock_db()
        with (
            patch.object(svc, "SessionLocal", return_value=db),
            patch.object(svc, "_update_block_ts", new=AsyncMock()) as update_ts,
        ):
            await svc._handle_decoded(decoded)
            await asyncio.sleep(0)
        db.add.assert_called_once()
        update_ts.assert_awaited_once()
        self.assertEqual(update_ts.await_args.args, (None, 12345))

    async def test_handle_decoded_db_error_rolls_back(self):
        svc._whale_wallets = {WHALE}
        svc._token_market_map = {"123": {"market_id": "m1", "question": "Q", "price": 0.5}}
        decoded = svc.decode_log(_transfer_single_log(OTHER, WHALE, 123, 30000))
        db = self._mock_db()
        db.commit.side_effect = RuntimeError("db down")
        with patch.object(svc, "SessionLocal", return_value=db):
            await svc._handle_decoded(decoded)
        db.rollback.assert_called_once()
        db.close.assert_called_once()

    async def test_handle_decoded_skips_unmapped_candidate_in_batch(self):
        svc._whale_wallets = {WHALE}
        svc._token_market_map = {"111": {"market_id": "m1", "question": "Q", "price": 0.5}}
        decoded = svc.decode_log(_transfer_batch_log(OTHER, WHALE, [111, 222], [30000, 30000]))
        db = self._mock_db()
        with patch.object(svc, "SessionLocal", return_value=db):
            await svc._handle_decoded(decoded)
        db.add.assert_called_once()
        event = db.add.call_args.args[0]
        self.assertEqual(event.token_id, "111")

    async def test_handle_decoded_dedup_skips_second_candidate(self):
        svc._whale_wallets = {WHALE}
        svc._token_market_map = {"123": {"market_id": "m1", "question": "Q", "price": 0.5}}
        decoded = svc.decode_log(_transfer_single_log(OTHER, WHALE, 123, 30000))
        db = self._mock_db()
        with patch.object(svc, "SessionLocal", return_value=db):
            await svc._handle_decoded(decoded)
            await svc._handle_decoded(decoded)
        db.add.assert_called_once()


# ── WS message handling edges ─────────────────────────────
class WsMessageEdgeTests(_ServiceStateTestCase):
    async def test_ws_message_result_not_a_dict(self):
        message = json.dumps({"method": "eth_subscription", "params": {"result": "0xnotadict"}})
        with patch.object(svc, "_handle_decoded", new=AsyncMock()) as handler:
            await svc._handle_ws_message(message)
        handler.assert_not_awaited()

    async def test_ws_message_params_missing(self):
        message = json.dumps({"method": "eth_subscription"})
        with patch.object(svc, "_handle_decoded", new=AsyncMock()) as handler:
            await svc._handle_ws_message(message)
        handler.assert_not_awaited()

    async def test_ws_message_undecodable_log(self):
        log = {"topics": ["0x" + "ee" * 32], "data": "0x"}
        message = json.dumps({"method": "eth_subscription", "params": {"result": log}})
        with patch.object(svc, "_handle_decoded", new=AsyncMock()) as handler:
            await svc._handle_ws_message(message)
        handler.assert_not_awaited()

    async def test_ws_message_none_payload(self):
        with patch.object(svc, "_handle_decoded", new=AsyncMock()) as handler:
            await svc._handle_ws_message(None)
        handler.assert_not_awaited()


# ── WS listen lifecycle ───────────────────────────────────
class _FakeWebSocket:
    """Minimal websockets connect() return double."""

    def __init__(self, messages, stop_event, reply=None):
        self._messages = messages
        self._stop_event = stop_event
        self._reply = reply if reply is not None else {"result": "0xsubid"}
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def recv(self):
        return json.dumps(self._reply)

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for message in self._messages:
            yield message
        self._stop_event.set()


class WsListenTests(_ServiceStateTestCase):
    async def test_ws_listen_already_stopped(self):
        stop_event = asyncio.Event()
        stop_event.set()
        with patch("websockets.connect", side_effect=AssertionError("must not connect")):
            await svc._ws_listen(stop_event)

    async def test_ws_listen_subscribes_and_processes_messages(self):
        stop_event = asyncio.Event()
        log = _transfer_single_log(OTHER, WHALE, 123, 30000)
        message = json.dumps({"method": "eth_subscription", "params": {"result": log}})
        ws = _FakeWebSocket([message], stop_event)
        with (
            patch("websockets.connect", return_value=ws),
            patch.object(svc, "_handle_decoded", new=AsyncMock()) as handler,
        ):
            await svc._ws_listen(stop_event)
        self.assertEqual(len(ws.sent), 1)
        subscribe = ws.sent[0]
        self.assertEqual(subscribe["method"], "eth_subscribe")
        self.assertEqual(subscribe["params"][0], "logs")
        self.assertEqual(
            subscribe["params"][1]["address"],
            [svc.CTF_EXCHANGE_ADDRESS, svc.CONDITIONAL_TOKENS_ADDRESS],
        )
        handler.assert_awaited_once()
        self.assertEqual(handler.await_args.args[0]["event"], "TransferSingle")

    async def test_ws_listen_subscribe_error_rotates(self):
        stop_event = asyncio.Event()
        ws = _FakeWebSocket([], stop_event, reply={"error": {"code": -32000, "message": "nope"}})
        sleeps = []

        async def _fake_sleep(delay):
            sleeps.append(delay)
            stop_event.set()

        with (
            patch("websockets.connect", return_value=ws),
            patch.object(svc.asyncio, "sleep", _fake_sleep),
        ):
            await svc._ws_listen(stop_event)
        self.assertEqual(sleeps, [1.0])

    async def test_ws_listen_stops_mid_message_loop(self):
        stop_event = asyncio.Event()
        log = _transfer_single_log(OTHER, WHALE, 123, 30000)
        message = json.dumps({"method": "eth_subscription", "params": {"result": log}})
        ws = _FakeWebSocket([message, message], stop_event)

        def _handler(decoded):
            stop_event.set()

        with (
            patch("websockets.connect", return_value=ws),
            patch.object(svc, "_handle_decoded", new=AsyncMock(side_effect=_handler)),
        ):
            await svc._ws_listen(stop_event)

    async def test_ws_listen_cancelled_propagates(self):
        stop_event = asyncio.Event()

        def _connect(*args, **kwargs):
            raise asyncio.CancelledError()

        with (
            patch("websockets.connect", _connect),
            self.assertRaises(asyncio.CancelledError),
        ):
            await svc._ws_listen(stop_event)

    async def test_ws_listen_backoff_doubles(self):
        stop_event = asyncio.Event()
        sleeps = []

        async def _fake_sleep(delay):
            sleeps.append(delay)
            if len(sleeps) >= 3:
                stop_event.set()

        with (
            patch("websockets.connect", side_effect=RuntimeError("rpc down")),
            patch.object(svc.asyncio, "sleep", _fake_sleep),
        ):
            await svc._ws_listen(stop_event)
        self.assertEqual(sleeps, [1.0, 2.0, 4.0])

    async def test_ws_listen_backoff_clamped(self):
        stop_event = asyncio.Event()
        sleeps = []

        async def _fake_sleep(delay):
            sleeps.append(delay)
            if len(sleeps) >= 3:
                stop_event.set()

        with (
            patch("websockets.connect", side_effect=RuntimeError("rpc down")),
            patch.object(svc.asyncio, "sleep", _fake_sleep),
            patch.object(svc, "MAX_BACKOFF_SECONDS", 2.0),
        ):
            await svc._ws_listen(stop_event)
        self.assertEqual(sleeps, [1.0, 2.0, 2.0])


# ── Periodic refresh loop ─────────────────────────────────
class PeriodicRefreshTests(_ServiceStateTestCase):
    async def test_periodic_refresh_runs_once_then_stops(self):
        stop_event = asyncio.Event()

        def _followed():
            stop_event.set()

        with (
            patch.object(svc, "_refresh_followed_map", MagicMock(side_effect=_followed)),
            patch.object(svc, "_refresh_whale_set", AsyncMock()) as whale_set,
            patch.object(svc, "_refresh_market_maps", AsyncMock()) as market_maps,
            patch.object(svc, "scheduler_heartbeat", MagicMock()) as heartbeat,
        ):
            await svc._periodic_refresh(stop_event)
        whale_set.assert_awaited_once()
        market_maps.assert_awaited_once()
        heartbeat.assert_called_once_with("ctf_events")

    async def test_periodic_refresh_suppresses_timeout(self):
        stop_event = asyncio.Event()
        iterations = []

        def _followed():
            iterations.append(1)
            if len(iterations) >= 2:
                stop_event.set()

        with (
            patch.object(svc, "_refresh_followed_map", MagicMock(side_effect=_followed)),
            patch.object(svc, "_refresh_whale_set", AsyncMock()),
            patch.object(svc, "_refresh_market_maps", AsyncMock()),
            patch.object(svc, "scheduler_heartbeat", MagicMock()),
            patch.object(svc, "REFRESH_INTERVAL_SECONDS", 0.01),
        ):
            await svc._periodic_refresh(stop_event)
        self.assertEqual(len(iterations), 2)


# ── Monitor lifecycle ─────────────────────────────────────
class LifecycleTests(_ServiceStateTestCase):
    async def test_start_monitor_already_running(self):
        svc._running = True
        with patch.object(svc, "acquire_scheduler_lock", MagicMock()) as acquire:
            await svc.start_ctf_events_monitor()
        acquire.assert_not_called()
        self.assertTrue(svc._running)

    async def test_start_monitor_lock_held_elsewhere(self):
        with patch.object(svc, "acquire_scheduler_lock", return_value=False):
            await svc.start_ctf_events_monitor()
        self.assertFalse(svc._running)
        self.assertIsNone(svc._monitor_task)
        self.assertIsNone(svc._refresh_task)
        self.assertIsNone(svc._stop_event)

    async def test_start_monitor(self):
        with (
            patch.object(svc, "acquire_scheduler_lock", return_value=True),
            patch.object(svc.asyncio, "create_task", MagicMock()) as create_task,
        ):
            await svc.start_ctf_events_monitor()
        self.assertTrue(svc._running)
        self.assertIsNotNone(svc._stop_event)
        self.assertFalse(svc._stop_event.is_set())
        self.assertEqual(create_task.call_count, 2)
        for call in create_task.call_args_list:
            call.args[0].close()

    async def test_stop_monitor_not_running(self):
        svc._running = False
        with patch.object(svc, "release_scheduler_lock", MagicMock()) as release:
            await svc.stop_ctf_events_monitor()
        release.assert_not_called()

    async def test_stop_monitor_cancels_tasks(self):
        svc._running = True
        svc._stop_event = asyncio.Event()
        svc._monitor_task = asyncio.create_task(asyncio.sleep(100))
        svc._refresh_task = asyncio.create_task(asyncio.sleep(100))
        with patch.object(svc, "release_scheduler_lock", MagicMock()) as release:
            await svc.stop_ctf_events_monitor()
        self.assertFalse(svc._running)
        self.assertIsNone(svc._monitor_task)
        self.assertIsNone(svc._refresh_task)
        self.assertIsNone(svc._stop_event)
        release.assert_called_once_with("ctf_events")

    async def test_stop_monitor_task_stop_error_logged(self):
        async def _evil():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                raise ValueError("stop failed") from None

        svc._running = True
        svc._stop_event = asyncio.Event()
        svc._monitor_task = asyncio.create_task(_evil())
        svc._refresh_task = None
        await asyncio.sleep(0)  # let the task start so cancel lands inside it
        with patch.object(svc, "release_scheduler_lock", MagicMock()) as release:
            await svc.stop_ctf_events_monitor()
        self.assertFalse(svc._running)
        self.assertIsNone(svc._monitor_task)
        release.assert_called_once_with("ctf_events")

    async def test_stop_monitor_without_stop_event_or_tasks(self):
        svc._running = True
        svc._stop_event = None
        svc._monitor_task = None
        svc._refresh_task = None
        with patch.object(svc, "release_scheduler_lock", MagicMock()) as release:
            await svc.stop_ctf_events_monitor()
        self.assertFalse(svc._running)
        release.assert_called_once_with("ctf_events")


# ── Dedup helper ──────────────────────────────────────────
class AlreadySeenTests(_ServiceStateTestCase):
    def test_already_seen_dedup(self):
        self.assertFalse(svc._already_seen("0xt1", 0, WHALE, "123"))
        self.assertTrue(svc._already_seen("0xt1", 0, WHALE, "123"))
        self.assertFalse(svc._already_seen("0xt1", 1, WHALE, "123"))
        self.assertFalse(svc._already_seen("0xt1", 0, OTHER, "123"))
        self.assertFalse(svc._already_seen("0xt2", 0, WHALE, "123"))


# ── Module-level ABI scan ─────────────────────────────────
class ModuleInitTests(unittest.TestCase):
    """The import-time ABI scan skips non-event entries.

    Both shipped ABIs contain only events, so the defensive
    ``continue`` for non-event entries is exercised by reloading
    the module against ABI files patched with a function entry.
    """

    def test_non_event_abi_entries_are_skipped(self):
        import importlib
        from pathlib import Path

        original_read_text = Path.read_text

        def _read_text_with_function_entry(path, encoding=None):
            text = original_read_text(path, encoding=encoding)
            if "abi" in str(path) and text.lstrip().startswith("["):
                body = text.rstrip()
                assert body.endswith("]")
                text = body[:-1] + ', {"type": "function", "name": "noop"}]'
            return text

        try:
            with patch.object(Path, "read_text", _read_text_with_function_entry):
                importlib.reload(svc)
        finally:
            importlib.reload(svc)

        self.assertTrue(svc._TOPIC_MAP)
        decoded = svc.decode_log(_transfer_single_log(OTHER, WHALE, 123, 5000))
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["event"], "TransferSingle")


if __name__ == "__main__":
    unittest.main()
