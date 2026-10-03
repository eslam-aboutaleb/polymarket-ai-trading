"""Tests for the on-chain whale monitoring service.

Decoding tests exercise the real web3/eth-abi path against
synthetic logs built from the pinned ABIs. Filter, dedup and
failover tests mock the database, cache state and the
websockets transport.
"""

import asyncio
import contextlib
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import eth_abi
from web3 import Web3

from app.services import ctf_events_service as svc

WHALE = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"
OPERATOR = "0x3333333333333333333333333333333333333333"
COLLATERAL = "0x4444444444444444444444444444444444444444"

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


def _position_split_log(owner, amount, **kwargs):
    topics = [POSITION_SPLIT_TOPIC, _padded(owner)]
    data = (
        "0x"
        + eth_abi.encode(
            ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
            [COLLATERAL, PARENT_COLLECTION_ID, CONDITION_ID, [1], amount],
        ).hex()
    )
    return _base_log(topics, data, **kwargs)


class DecodeTests(unittest.TestCase):
    """Raw log → normalized event decoding."""

    def test_decode_transfer_single(self):
        log = _transfer_single_log(OTHER, WHALE, 12345, 5000)
        decoded = svc.decode_log(log)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["event"], "TransferSingle")
        self.assertEqual(decoded["args"]["from"].lower(), OTHER)
        self.assertEqual(decoded["args"]["to"].lower(), WHALE)
        self.assertEqual(int(decoded["args"]["id"]), 12345)
        self.assertEqual(int(decoded["args"]["value"]), 5000)
        self.assertEqual(decoded["tx_hash"], "0xabc")
        self.assertEqual(decoded["log_index"], 0)

    def test_decode_transfer_batch(self):
        log = _transfer_batch_log(OTHER, WHALE, [1, 2], [100, 200])
        decoded = svc.decode_log(log)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["event"], "TransferBatch")
        self.assertEqual([int(i) for i in decoded["args"]["ids"]], [1, 2])
        self.assertEqual([int(v) for v in decoded["args"]["values"]], [100, 200])

    def test_decode_position_split(self):
        log = _position_split_log(WHALE, 5_000_000)
        decoded = svc.decode_log(log)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["event"], "PositionSplit")
        self.assertEqual(decoded["args"]["stakeOwner"].lower(), WHALE)
        self.assertEqual(int(decoded["args"]["amount"]), 5_000_000)

    def test_unknown_topic_returns_none(self):
        log = _base_log(["0x" + "de" * 32], "0x")
        self.assertIsNone(svc.decode_log(log))

    def test_undecodable_payload_returns_none(self):
        topics = [
            TRANSFER_SINGLE_TOPIC,
            _padded(OPERATOR),
            _padded(OTHER),
            _padded(WHALE),
        ]
        log = _base_log(topics, "0x1234")
        self.assertIsNone(svc.decode_log(log))

    def test_hex_to_int(self):
        self.assertEqual(svc._hex_to_int("0x64"), 100)
        self.assertEqual(svc._hex_to_int(100), 100)
        self.assertIsNone(svc._hex_to_int(None))
        self.assertIsNone(svc._hex_to_int("0xzz"))

    def test_to_hex_string(self):
        self.assertEqual(svc._to_hex_string(bytes.fromhex("ab" * 32)), "0x" + "ab" * 32)
        self.assertEqual(svc._to_hex_string(None), "")
        self.assertEqual(svc._to_hex_string("0xabc"), "0xabc")


class CandidateTests(unittest.TestCase):
    """Decoded events → per-wallet whale candidates."""

    def test_transfer_candidates_buy_and_sell(self):
        decoded = {
            "event": "TransferSingle",
            "args": {"from": OTHER, "to": WHALE, "id": 1, "value": 10},
        }
        candidates = svc._whale_candidates(decoded)
        sides = {c["wallet"]: c["side"] for c in candidates}
        self.assertEqual(sides[WHALE], "buy")
        self.assertEqual(sides[OTHER], "sell")

    def test_mint_transfer_is_open(self):
        decoded = {
            "event": "TransferSingle",
            "args": {"from": svc.ZERO_ADDRESS, "to": WHALE, "id": 1, "value": 10},
        }
        candidates = svc._whale_candidates(decoded)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["wallet"], WHALE)
        self.assertEqual(candidates[0]["side"], "open")

    def test_burn_transfer_is_close(self):
        decoded = {
            "event": "TransferSingle",
            "args": {"from": WHALE, "to": svc.ZERO_ADDRESS, "id": 1, "value": 10},
        }
        candidates = svc._whale_candidates(decoded)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["wallet"], WHALE)
        self.assertEqual(candidates[0]["side"], "close")

    def test_position_split_candidate_uses_condition_reference(self):
        decoded = {
            "event": "PositionSplit",
            "args": {
                "stakeOwner": WHALE,
                "conditionId": CONDITION_ID,
                "amount": 5_000_000,
            },
        }
        candidates = svc._whale_candidates(decoded)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["token_id"], "cond:0x" + "ab" * 32)
        self.assertEqual(candidates[0]["size"], 5.0)
        self.assertEqual(candidates[0]["side"], "open")
        self.assertEqual(candidates[0]["event_type"], "position_split")

    def test_redemption_candidate(self):
        decoded = {
            "event": "Redemption",
            "args": {
                "stakeOwner": WHALE,
                "conditionId": CONDITION_ID,
                "payout": 2_500_000,
            },
        }
        candidates = svc._whale_candidates(decoded)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["size"], 2.5)
        self.assertEqual(candidates[0]["side"], "redeem")


class MarketMapTests(unittest.TestCase):
    """token_id / condition_id → market resolution."""

    def setUp(self):
        svc._token_market_map = {"123": {"market_id": "m1", "question": "Q1", "price": 0.5}}
        svc._condition_market_map = {
            "0x" + "ab" * 32: {"market_id": "m2", "question": "Q2", "price": 0.4}
        }

    def tearDown(self):
        svc._token_market_map = {}
        svc._condition_market_map = {}

    def test_resolve_token(self):
        info = svc._resolve_market("123")
        self.assertEqual(info["market_id"], "m1")

    def test_resolve_condition_reference(self):
        info = svc._resolve_market("cond:0x" + "ab" * 32)
        self.assertEqual(info["market_id"], "m2")

    def test_resolve_unmapped(self):
        self.assertIsNone(svc._resolve_market("999"))


class WhaleFilterTests(unittest.TestCase):
    """Whale-set membership, notional threshold, dedup, persistence."""

    def setUp(self):
        svc._whale_wallets = {WHALE}
        svc._followed_map = {}
        svc._watchlist_map = {}
        svc._token_market_map = {"123": {"market_id": "m1", "question": "Q1", "price": 0.5}}
        svc._condition_market_map = {}
        svc.invalidate_config_cache()
        with contextlib.suppress(Exception):
            svc._cache.clear()

    def tearDown(self):
        svc._whale_wallets = set()
        svc._followed_map = {}
        svc._watchlist_map = {}
        svc._token_market_map = {}
        svc._condition_market_map = {}
        svc.invalidate_config_cache()

    def _mock_db(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        return db

    def test_non_whale_wallet_skipped(self):
        decoded = svc.decode_log(_transfer_single_log(OTHER, OPERATOR, 123, 1_000_000))
        db = self._mock_db()
        with patch.object(svc, "SessionLocal", return_value=db):
            asyncio.run(svc._handle_decoded(decoded))
        db.add.assert_not_called()

    def test_below_threshold_skipped(self):
        decoded = svc.decode_log(_transfer_single_log(OTHER, WHALE, 123, 1000))
        db = self._mock_db()
        with patch.object(svc, "SessionLocal", return_value=db):
            asyncio.run(svc._handle_decoded(decoded))
        db.add.assert_not_called()

    def test_unmapped_token_skipped(self):
        decoded = svc.decode_log(_transfer_single_log(OTHER, WHALE, 999, 1_000_000))
        db = self._mock_db()
        with patch.object(svc, "SessionLocal", return_value=db):
            asyncio.run(svc._handle_decoded(decoded))
        db.add.assert_not_called()

    def test_whale_event_persisted(self):
        decoded = svc.decode_log(_transfer_single_log(OTHER, WHALE, 123, 30000))
        db = self._mock_db()
        with patch.object(svc, "SessionLocal", return_value=db):
            asyncio.run(svc._handle_decoded(decoded))
        db.add.assert_called_once()
        event = db.add.call_args.args[0]
        self.assertEqual(event.wallet, WHALE)
        self.assertEqual(event.market_id, "m1")
        self.assertEqual(event.side, "buy")
        self.assertAlmostEqual(event.size, 30000.0)
        self.assertAlmostEqual(event.price, 0.5)
        self.assertAlmostEqual(event.notional, 15000.0)
        db.commit.assert_called()

    def test_dedup_same_event_twice(self):
        decoded = svc.decode_log(_transfer_single_log(OTHER, WHALE, 123, 30000))
        db = self._mock_db()
        with patch.object(svc, "SessionLocal", return_value=db):
            asyncio.run(svc._handle_decoded(decoded))
            asyncio.run(svc._handle_decoded(decoded))
        db.add.assert_called_once()

    def test_followed_trader_alert_dispatched(self):
        svc._followed_map = {WHALE: {42}}
        decoded = svc.decode_log(_transfer_single_log(OTHER, WHALE, 123, 30000))
        db = self._mock_db()
        with (
            patch.object(svc, "SessionLocal", return_value=db),
            patch.object(svc, "_dispatch_alert") as alert_mock,
        ):
            asyncio.run(svc._handle_decoded(decoded))
        alert_mock.assert_called_once()
        user_id, payload = alert_mock.call_args.args
        self.assertEqual(user_id, 42)
        self.assertEqual(payload["wallet"], WHALE)
        self.assertEqual(payload["market_id"], "m1")
        self.assertAlmostEqual(payload["notional"], 15000.0)

    def test_watchlist_alert_dispatched(self):
        svc._watchlist_map = {WHALE: {7}}
        decoded = svc.decode_log(_transfer_single_log(OTHER, WHALE, 123, 30000))
        db = self._mock_db()
        with (
            patch.object(svc, "SessionLocal", return_value=db),
            patch.object(svc, "_dispatch_alert") as alert_mock,
        ):
            asyncio.run(svc._handle_decoded(decoded))
        alert_mock.assert_called_once()
        self.assertEqual(alert_mock.call_args.args[0], 7)

    def test_per_user_threshold_overrides_default(self):
        svc._followed_map = {WHALE: {42}}
        config = svc.WhaleConfig(user_id=42, min_notional=100.0)
        svc._config_cache[42] = config
        decoded = svc.decode_log(_transfer_single_log(OTHER, WHALE, 123, 1000))
        db = self._mock_db()
        with patch.object(svc, "SessionLocal", return_value=db):
            asyncio.run(svc._handle_decoded(decoded))
        db.add.assert_called_once()


class ConfigTests(unittest.TestCase):
    """Per-user whale config defaults and cache invalidation."""

    def setUp(self):
        svc.invalidate_config_cache()

    def tearDown(self):
        svc.invalidate_config_cache()

    def test_defaults(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        config = svc.get_user_config(1, db)
        self.assertAlmostEqual(config.min_notional, 10000.0)
        self.assertFalse(config.auto_copy)
        self.assertEqual(config.watchlist, [])
        self.assertEqual(config.whale_set_size, 50)

    def test_config_cached(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        first = svc.get_user_config(1, db)
        second = svc.get_user_config(1, db)
        self.assertIs(first, second)
        self.assertEqual(db.query.call_count, 1)

    def test_invalidate_config_cache(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        svc.get_user_config(1, db)
        svc.invalidate_config_cache(1)
        self.assertNotIn(1, svc._config_cache)
        svc.invalidate_config_cache()
        self.assertEqual(svc._config_cache, {})


class WsMessageTests(unittest.TestCase):
    """eth_subscription message handling."""

    def test_ignores_non_subscription_messages(self):
        with patch.object(svc, "_handle_decoded", new=AsyncMock()) as handler:
            asyncio.run(svc._handle_ws_message(json.dumps({"id": 1, "result": "0xsub"})))
        handler.assert_not_awaited()

    def test_ignores_invalid_json(self):
        asyncio.run(svc._handle_ws_message("not-json"))

    def test_processes_subscription_log(self):
        log = _transfer_single_log(OTHER, WHALE, 123, 30000)
        message = json.dumps(
            {
                "method": "eth_subscription",
                "params": {"subscription": "0x1", "result": log},
            }
        )
        with patch.object(svc, "_handle_decoded", new=AsyncMock()) as handler:
            asyncio.run(svc._handle_ws_message(message))
        handler.assert_awaited_once()
        self.assertEqual(handler.await_args.args[0]["event"], "TransferSingle")


class FailoverTests(unittest.TestCase):
    """WS RPC failover rotation."""

    def test_failover_rotates_to_second_rpc(self):
        stop_event = asyncio.Event()
        connect_urls = []
        sleep_calls = []

        def _connect(url, **kwargs):
            connect_urls.append(url)
            raise RuntimeError("rpc down")

        async def _fake_sleep(delay):
            sleep_calls.append(delay)
            if len(sleep_calls) >= 2:
                stop_event.set()

        with (
            patch("websockets.connect", side_effect=_connect),
            patch.object(svc.asyncio, "sleep", _fake_sleep),
        ):
            asyncio.run(svc._ws_listen(stop_event))

        self.assertEqual(connect_urls, [svc.WS_RPC_URLS[0], svc.WS_RPC_URLS[1]])


if __name__ == "__main__":
    unittest.main()
