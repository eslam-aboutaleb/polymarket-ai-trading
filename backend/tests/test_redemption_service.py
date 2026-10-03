"""Tests for the on-chain auto-redeem service (plan 05).

All web3 interactions are mocked: no network access and no
real on-chain transactions. Covers:

* ``compute_collection_id`` against known vectors (cross-
  verified with eth-utils keccak).
* ``determine_winner_index`` outcome-price classification.
* ``build_polymarket_event_url`` (backend mirror of the
  frontend urlSafety builder).
* ``_parse_redeemable_position`` for both upstream shapes.
* The EOA redemption path (direct Conditional Tokens call).
* The POLY_PROXY path (redeem through the proxy contract).
* Proxy failure → claim_via_ui fallback with deep-link.
* Dry-run mode (tx built, never broadcast).
* Gas guard, per-tx value cap, per-user daily cap.
* Receipt confirmation / on-chain revert handling.
* The redemption cycle and the manual per-market trigger.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from web3 import Web3

import app.services.redemption_service as redemption_service
from app.services.redemption_service import (
    _CONDITIONAL_TOKENS,
    _CTF_EXCHANGE,
    DAILY_REDEEM_CAP_PER_USER,
    MAX_REDEEM_VALUE_PER_TX,
    build_polymarket_event_url,
    compute_collection_id,
    determine_winner_index,
)

# ── Known collectionId vectors ───────────────────────────
# Computed with the reference formula
# uint256(keccak256(abi.encode(0, conditionId, 1 << index)))
# and cross-verified against eth-utils.keccak.

CID_AB = "0x" + "ab" * 32
CID_AB_INDEX_0 = "9cdd4f07b364dd440fa1d10c130070958423f0e6b92e2c4aa3ef4dd91412db20"
CID_AB_INDEX_1 = "77dd4e5b43eb8e1cf61beb45da6aaec9aa91f52f53c698e3fd26b26c460a169f"

CID_0001 = "0x" + "00" * 31 + "01"
CID_0001_INDEX_0 = "2a60bbfb8848b3d2b00b8c5347dc1c6691a208bd52c3791689dcbcdaf86085b9"
CID_0001_INDEX_1 = "e682b7c401097344fed1af3e3492f018caf2a2491b45159ba612453495164301"
CID_0001_INDEX_2 = "3b789c6482d7a4aea1bd7cac9f83807fb2f421bdcf440b0e4a328418ac1cb3fc"

ZERO_ADDRESS = "0x" + "00" * 20
TEST_EOA = "0x" + "11" * 20
TEST_PROXY = "0x" + "22" * 20
TEST_PRIVATE_KEY = "0x" + "33" * 32  # test-only key, never a real wallet
TEST_TX_HASH = "0x" + "ab" * 32


def _fake_db(daily_count: int = 0) -> MagicMock:
    """MagicMock session whose daily-attempt count is configured."""
    db = MagicMock()
    db.query.return_value.filter.return_value.count.return_value = daily_count
    return db


def _fake_w3(
    balance: int = 100,
    proxy_address: str | None = None,
    gas_wei: int | None = None,
    receipt_status: int = 1,
    proxy_redeem_error: Exception | None = None,
) -> MagicMock:
    """Build a mocked web3 provider for redemption tests.

    Contract mocks are dispatched by address so the service's
    CTF / exchange / proxy calls are individually assertable.
    """
    w3 = MagicMock()
    w3.is_connected.return_value = True
    w3.eth.get_balance.return_value = gas_wei if gas_wei is not None else Web3.to_wei(1.0, "ether")
    w3.eth.get_transaction_count.return_value = 7
    w3.eth.gas_price = Web3.to_wei(50, "gwei")
    signed = MagicMock()
    signed.raw_transaction = b"\x01\x02\x03"
    w3.eth.account.sign_transaction.return_value = signed
    w3.eth.send_raw_transaction.return_value = bytes.fromhex(TEST_TX_HASH.removeprefix("0x"))
    w3.eth.wait_for_transaction_receipt.return_value = {"status": receipt_status}

    ctf = MagicMock()
    ctf.functions.balanceOf.return_value.call.return_value = balance
    ctf.functions.redeemPosition.return_value.build_transaction.return_value = {
        "from": TEST_EOA,
        "nonce": 7,
        "gasPrice": 1,
        "chainId": 137,
    }

    exchange = MagicMock()
    exchange.functions.getPolyProxyWalletAddress.return_value.call.return_value = (
        proxy_address or ZERO_ADDRESS
    )

    proxy = MagicMock()
    if proxy_redeem_error is not None:
        proxy.functions.redeemPosition.return_value.build_transaction.side_effect = (
            proxy_redeem_error
        )
    else:
        proxy.functions.redeemPosition.return_value.build_transaction.return_value = {
            "from": TEST_EOA,
            "nonce": 7,
            "gasPrice": 1,
            "chainId": 137,
        }

    def _contract(address=None, abi=None):
        addr = (address or "").lower()
        if addr == _CTF_EXCHANGE.lower():
            return exchange
        if addr == _CONDITIONAL_TOKENS.lower():
            return ctf
        if proxy_address and addr == proxy_address.lower():
            return proxy
        return MagicMock()

    w3.eth.contract.side_effect = _contract
    w3._ctf = ctf
    w3._exchange = exchange
    w3._proxy = proxy
    return w3


def _position(**overrides) -> dict:
    """Build a parsed redeemable-position record."""
    base = {
        "market_id": CID_AB,
        "condition_id": CID_AB,
        "title": "Test market",
        "size": 5.0,
        "winner_index": 1,
        "market_slug": "test-market-slug",
        "event_slug": "test-event-slug",
    }
    base.update(overrides)
    return base


def _raw_position(**overrides) -> dict:
    """Build a raw upstream position (Gamma-style nested market)."""
    base = {
        "conditionId": CID_AB,
        "size": "5.0",
        "market": {
            "resolved": True,
            "question": "Test market",
            "outcomePrices": ["0.0", "1.0"],
            "marketSlug": "test-market-slug",
            "eventSlug": "test-event-slug",
        },
    }
    base.update(overrides)
    return base


class ComputeCollectionIdTests(unittest.TestCase):
    """collectionId computation against known vectors."""

    def test_known_vector_binary_index_0(self):
        self.assertEqual(compute_collection_id(CID_AB, 0), CID_AB_INDEX_0)

    def test_known_vector_binary_index_1(self):
        self.assertEqual(compute_collection_id(CID_AB, 1), CID_AB_INDEX_1)

    def test_known_vector_multi_outcome(self):
        self.assertEqual(compute_collection_id(CID_0001, 0), CID_0001_INDEX_0)
        self.assertEqual(compute_collection_id(CID_0001, 1), CID_0001_INDEX_1)
        self.assertEqual(compute_collection_id(CID_0001, 2), CID_0001_INDEX_2)

    def test_accepts_condition_id_without_0x_prefix(self):
        self.assertEqual(
            compute_collection_id(CID_AB.removeprefix("0x"), 1),
            CID_AB_INDEX_1,
        )

    def test_rejects_non_32_byte_condition_id(self):
        with self.assertRaises(ValueError):
            compute_collection_id("0x" + "ab" * 31, 0)
        with self.assertRaises(ValueError):
            compute_collection_id("not-a-condition-id", 0)

    def test_index_set_is_one_shifted_by_winner_index(self):
        # Different winner indices must produce different collections.
        self.assertNotEqual(compute_collection_id(CID_AB, 0), compute_collection_id(CID_AB, 1))


class DetermineWinnerIndexTests(unittest.TestCase):
    """Winning-outcome classification from outcomePrices."""

    def test_second_outcome_wins(self):
        self.assertEqual(determine_winner_index(["0.0", "1.0"]), 1)

    def test_first_outcome_wins(self):
        self.assertEqual(determine_winner_index(["0.995", "0.005"]), 0)

    def test_no_decisive_outcome(self):
        self.assertIsNone(determine_winner_index(["0.5", "0.5"]))
        self.assertIsNone(determine_winner_index(["0.4", "0.6"]))

    def test_malformed_prices_are_skipped(self):
        self.assertEqual(determine_winner_index(["abc", "0.99"]), 1)
        self.assertIsNone(determine_winner_index(["abc", None]))

    def test_non_list_input(self):
        self.assertIsNone(determine_winner_index(None))
        self.assertIsNone(determine_winner_index("0.5"))

    def test_json_string_prices(self):
        self.assertEqual(determine_winner_index('["0.0", "1.0"]'), 1)


class BuildPolymarketEventUrlTests(unittest.TestCase):
    """Deep-link builder mirrors frontend/src/utils/urlSafety.ts."""

    def test_market_slug_only(self):
        self.assertEqual(
            build_polymarket_event_url(None, "market-slug"),
            "https://polymarket.com/event/market-slug",
        )

    def test_event_and_market_slug(self):
        self.assertEqual(
            build_polymarket_event_url("event-slug", "market-slug"),
            "https://polymarket.com/event/event-slug/market-slug",
        )

    def test_event_slug_only(self):
        self.assertEqual(
            build_polymarket_event_url("event-slug", None),
            "https://polymarket.com/event/event-slug",
        )

    def test_unsafe_slug_returns_none(self):
        self.assertIsNone(build_polymarket_event_url("bad slug!", None))
        self.assertIsNone(build_polymarket_event_url(None, "bad/slug"))
        self.assertIsNone(build_polymarket_event_url(None, None))

    def test_overlong_slug_returns_none(self):
        self.assertIsNone(build_polymarket_event_url("a" * 200, None))


class ParseRedeemablePositionTests(unittest.TestCase):
    """Upstream position parsing for both shapes."""

    def test_nested_market_shape(self):
        parsed = redemption_service._parse_redeemable_position(_raw_position())
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["market_id"], CID_AB)
        self.assertEqual(parsed["winner_index"], 1)
        self.assertEqual(parsed["size"], 5.0)
        self.assertEqual(parsed["market_slug"], "test-market-slug")

    def test_string_market_shape(self):
        parsed = redemption_service._parse_redeemable_position(
            {
                "conditionId": CID_AB,
                "market": CID_AB,
                "size": 2.5,
                "outcomePrices": ["1.0", "0.0"],
                "resolved": True,
            }
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["winner_index"], 0)

    def test_unresolved_market_skipped(self):
        raw = _raw_position()
        raw["market"]["resolved"] = False
        raw["market"]["closed"] = False
        self.assertIsNone(redemption_service._parse_redeemable_position(raw))

    def test_closed_market_counts_as_resolved(self):
        raw = _raw_position()
        del raw["market"]["resolved"]
        raw["market"]["closed"] = True
        self.assertIsNotNone(redemption_service._parse_redeemable_position(raw))

    def test_no_decisive_winner_skipped(self):
        raw = _raw_position()
        raw["market"]["outcomePrices"] = ["0.5", "0.5"]
        self.assertIsNone(redemption_service._parse_redeemable_position(raw))

    def test_zero_size_skipped(self):
        self.assertIsNone(redemption_service._parse_redeemable_position(_raw_position(size="0")))

    def test_invalid_condition_id_skipped(self):
        self.assertIsNone(
            redemption_service._parse_redeemable_position(_raw_position(conditionId="too-short"))
        )


class ExecuteRedeemTests(unittest.IsolatedAsyncioTestCase):
    """The synchronous redemption core with a mocked provider."""

    def _execute(self, w3, position=None, db=None, dry_run=False):
        with patch(
            "app.services.redemption_service.load_wallet_credentials",
            return_value={"private_key": TEST_PRIVATE_KEY, "clob_creds": None},
        ):
            return redemption_service._execute_redeem(
                db or _fake_db(),
                1,
                TEST_EOA,
                position or _position(),
                w3=w3,
                dry_run=dry_run,
            )

    def test_eoa_path_redeems_via_conditional_tokens(self):
        w3 = _fake_w3()
        attempt = self._execute(w3)
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, "confirmed")
        self.assertEqual(attempt.tx_hash, TEST_TX_HASH)
        self.assertEqual(attempt.collection_id, CID_AB_INDEX_1)
        self.assertEqual(attempt.amount, 5.0)
        # Proxy lookup happened, EOA held the position directly.
        w3._exchange.functions.getPolyProxyWalletAddress.assert_called_once()
        # Redeem went to the Conditional Tokens contract.
        w3._ctf.functions.redeemPosition.assert_called_once_with(CID_AB_INDEX_1)
        w3._proxy.functions.redeemPosition.assert_not_called()
        w3.eth.send_raw_transaction.assert_called_once()

    def test_proxy_path_redeems_through_proxy_contract(self):
        w3 = _fake_w3(proxy_address=TEST_PROXY)
        attempt = self._execute(w3)
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, "confirmed")
        w3._proxy.functions.redeemPosition.assert_called_once_with(CID_AB_INDEX_1)
        w3._ctf.functions.redeemPosition.assert_not_called()

    def test_zero_balance_skips_redemption(self):
        w3 = _fake_w3(balance=0)
        attempt = self._execute(w3)
        self.assertIsNone(attempt)
        w3.eth.send_raw_transaction.assert_not_called()

    def test_dry_run_builds_but_never_broadcasts(self):
        w3 = _fake_w3()
        attempt = self._execute(w3, dry_run=True)
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, "pending")
        self.assertIsNone(attempt.tx_hash)
        # The transaction is built (for logging) but never signed/sent.
        w3._ctf.functions.redeemPosition.return_value.build_transaction.assert_called_once()
        w3.eth.account.sign_transaction.assert_not_called()
        w3.eth.send_raw_transaction.assert_not_called()

    def test_proxy_failure_falls_back_to_claim_via_ui(self):
        w3 = _fake_w3(
            proxy_address=TEST_PROXY,
            proxy_redeem_error=RuntimeError("proxy redeem reverted"),
        )
        with patch(
            "app.services.redemption_service.dispatch_alert",
            new=AsyncMock(),
        ) as mock_dispatch:
            attempt = self._execute(w3)
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, "claim_via_ui")
        self.assertIn("proxy redeem reverted", attempt.error)
        w3.eth.send_raw_transaction.assert_not_called()
        # The user is notified via the alert service with a
        # Polymarket deep-link (inline delivery from the worker thread).
        mock_dispatch.assert_called_once()
        args, kwargs = mock_dispatch.call_args
        self.assertEqual(args[0], "redeem_available")
        self.assertEqual(args[1], 1)
        self.assertIn(
            "polymarket.com/event/test-event-slug/test-market-slug",
            args[2]["deep_link"],
        )
        self.assertFalse(kwargs.get("background", True))

    def test_insufficient_gas_notifies_and_fails(self):
        w3 = _fake_w3(gas_wei=Web3.to_wei(0.001, "ether"))
        with patch(
            "app.services.redemption_service.dispatch_alert",
            new=AsyncMock(),
        ) as mock_dispatch:
            attempt = self._execute(w3)
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.error, "insufficient MATIC for gas")
        w3.eth.send_raw_transaction.assert_not_called()
        mock_dispatch.assert_called_once()
        args, _kwargs = mock_dispatch.call_args
        self.assertEqual(args[0], "redeem_available")

    def test_value_cap_blocks_large_positions(self):
        w3 = _fake_w3()
        attempt = self._execute(w3, position=_position(size=MAX_REDEEM_VALUE_PER_TX + 1))
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, "failed")
        self.assertIn("per-tx cap", attempt.error)
        w3.eth.send_raw_transaction.assert_not_called()

    def test_daily_cap_blocks_excessive_redemptions(self):
        w3 = _fake_w3()
        attempt = self._execute(w3, db=_fake_db(DAILY_REDEEM_CAP_PER_USER))
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.error, "daily redeem cap reached")
        w3.eth.send_raw_transaction.assert_not_called()

    def test_missing_credentials_fail_the_attempt(self):
        w3 = _fake_w3()
        with patch(
            "app.services.redemption_service.load_wallet_credentials",
            return_value=None,
        ):
            attempt = redemption_service._execute_redeem(
                _fake_db(), 1, TEST_EOA, _position(), w3=w3, dry_run=False
            )
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.error, "no wallet credentials stored")

    def test_unreachable_rpc_fails_the_attempt(self):
        with (
            patch(
                "app.services.redemption_service._connect_web3",
                return_value=None,
            ),
            patch(
                "app.services.redemption_service.load_wallet_credentials",
                return_value={"private_key": TEST_PRIVATE_KEY, "clob_creds": None},
            ),
        ):
            attempt = redemption_service._execute_redeem(
                _fake_db(), 1, TEST_EOA, _position(), dry_run=False
            )
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.error, "no Polygon RPC reachable")

    def test_reverted_receipt_marks_failed(self):
        w3 = _fake_w3(receipt_status=0)
        attempt = self._execute(w3)
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, "failed")
        self.assertIn("reverted", attempt.error)
        self.assertEqual(attempt.tx_hash, TEST_TX_HASH)

    def test_eoa_send_failure_marks_failed(self):
        w3 = _fake_w3()
        w3.eth.send_raw_transaction.side_effect = RuntimeError("broadcast failed")
        attempt = self._execute(w3)
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, "failed")
        self.assertIn("broadcast failed", attempt.error)


class OrchestrationTests(unittest.IsolatedAsyncioTestCase):
    """Cycle, manual trigger and background-manager behaviour."""

    async def test_run_redeem_cycle_dry_run_by_default(self):
        db = _fake_db()
        db.query.return_value.all.return_value = [(1, TEST_EOA)]
        service = MagicMock()
        service.get_positions = AsyncMock(return_value=[_raw_position()])
        w3 = _fake_w3()
        with (
            patch(
                "app.services.redemption_service.SessionLocal",
                return_value=db,
            ),
            patch(
                "app.services.polymarket_service.get_polymarket_service",
                return_value=service,
            ),
            patch(
                "app.services.redemption_service._connect_web3",
                return_value=w3,
            ),
            patch(
                "app.services.redemption_service.load_wallet_credentials",
                return_value={"private_key": TEST_PRIVATE_KEY, "clob_creds": None},
            ),
        ):
            summary = await redemption_service.run_redeem_cycle()
        self.assertEqual(summary["users_checked"], 1)
        self.assertEqual(summary["attempts"], 1)
        self.assertTrue(summary["dry_run"])
        # Dry-run: nothing was broadcast.
        w3.eth.send_raw_transaction.assert_not_called()

    async def test_run_redeem_cycle_live_mode_confirms(self):
        db = _fake_db()
        db.query.return_value.all.return_value = [(1, TEST_EOA)]
        service = MagicMock()
        service.get_positions = AsyncMock(return_value=[_raw_position()])
        w3 = _fake_w3()
        with (
            patch(
                "app.services.redemption_service.SessionLocal",
                return_value=db,
            ),
            patch(
                "app.services.redemption_service.REDEEM_DRY_RUN",
                False,
            ),
            patch(
                "app.services.polymarket_service.get_polymarket_service",
                return_value=service,
            ),
            patch(
                "app.services.redemption_service._connect_web3",
                return_value=w3,
            ),
            patch(
                "app.services.redemption_service.load_wallet_credentials",
                return_value={"private_key": TEST_PRIVATE_KEY, "clob_creds": None},
            ),
        ):
            summary = await redemption_service.run_redeem_cycle()
        self.assertEqual(summary["confirmed"], 1)
        w3.eth.send_raw_transaction.assert_called_once()

    async def test_redeem_market_for_user_manual_trigger(self):
        db = _fake_db()
        service = MagicMock()
        service.get_positions = AsyncMock(return_value=[_raw_position()])
        w3 = _fake_w3()
        with (
            patch(
                "app.services.redemption_service.SessionLocal",
                return_value=db,
            ),
            patch(
                "app.services.redemption_service.REDEEM_DRY_RUN",
                False,
            ),
            patch(
                "app.services.polymarket_service.get_polymarket_service",
                return_value=service,
            ),
            patch(
                "app.services.redemption_service._connect_web3",
                return_value=w3,
            ),
            patch(
                "app.services.redemption_service.load_wallet_credentials",
                return_value={"private_key": TEST_PRIVATE_KEY, "clob_creds": None},
            ),
        ):
            attempt = await redemption_service.redeem_market_for_user(1, TEST_EOA, CID_AB)
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, "confirmed")
        self.assertEqual(attempt.collection_id, CID_AB_INDEX_1)

    async def test_redeem_market_for_user_winner_index_override(self):
        db = _fake_db()
        service = MagicMock()
        service.get_positions = AsyncMock(return_value=[_raw_position()])
        w3 = _fake_w3()
        with (
            patch(
                "app.services.redemption_service.SessionLocal",
                return_value=db,
            ),
            patch(
                "app.services.redemption_service.REDEEM_DRY_RUN",
                False,
            ),
            patch(
                "app.services.polymarket_service.get_polymarket_service",
                return_value=service,
            ),
            patch(
                "app.services.redemption_service._connect_web3",
                return_value=w3,
            ),
            patch(
                "app.services.redemption_service.load_wallet_credentials",
                return_value={"private_key": TEST_PRIVATE_KEY, "clob_creds": None},
            ),
        ):
            attempt = await redemption_service.redeem_market_for_user(
                1, TEST_EOA, CID_AB, winner_outcome_index=0
            )
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.collection_id, CID_AB_INDEX_0)

    async def test_redeem_market_for_user_no_position_returns_none(self):
        service = MagicMock()
        service.get_positions = AsyncMock(return_value=[])
        with patch(
            "app.services.polymarket_service.get_polymarket_service",
            return_value=service,
        ):
            attempt = await redemption_service.redeem_market_for_user(1, TEST_EOA, CID_AB)
        self.assertIsNone(attempt)

    async def test_redeem_market_for_user_matches_market_slug(self):
        db = _fake_db()
        service = MagicMock()
        service.get_positions = AsyncMock(return_value=[_raw_position()])
        w3 = _fake_w3()
        with (
            patch(
                "app.services.redemption_service.SessionLocal",
                return_value=db,
            ),
            patch(
                "app.services.redemption_service.REDEEM_DRY_RUN",
                False,
            ),
            patch(
                "app.services.polymarket_service.get_polymarket_service",
                return_value=service,
            ),
            patch(
                "app.services.redemption_service._connect_web3",
                return_value=w3,
            ),
            patch(
                "app.services.redemption_service.load_wallet_credentials",
                return_value={"private_key": TEST_PRIVATE_KEY, "clob_creds": None},
            ),
        ):
            attempt = await redemption_service.redeem_market_for_user(
                1, TEST_EOA, "test-market-slug"
            )
        self.assertIsNotNone(attempt)

    async def test_redeem_loop_skips_when_lock_held(self):
        # Another worker holds the plan-02 advisory lock:
        # this process must not start its loop.
        with (
            patch(
                "app.services.redemption_service.acquire_scheduler_lock",
                return_value=False,
            ),
            patch(
                "app.services.redemption_service.run_redeem_cycle",
                new=AsyncMock(),
            ) as mock_cycle,
        ):
            await redemption_service._redeem_loop()
        mock_cycle.assert_not_called()

    async def test_redeem_loop_runs_unlocked_without_plan02(self):
        # plan 02 absent: the loop runs unlocked and still
        # heartbeats nothing (heartbeat is None).
        async def _cancel_after_first_cycle():
            raise asyncio.CancelledError

        with (
            patch(
                "app.services.redemption_service.acquire_scheduler_lock",
                None,
            ),
            patch(
                "app.services.redemption_service.release_scheduler_lock",
                None,
            ),
            patch(
                "app.services.redemption_service.scheduler_heartbeat",
                None,
            ),
            patch(
                "app.services.redemption_service.run_redeem_cycle",
                new=AsyncMock(side_effect=_cancel_after_first_cycle),
            ) as mock_cycle,
        ):
            await redemption_service._redeem_loop()
        mock_cycle.assert_called_once()

    def test_notify_user_falls_back_to_logging(self):
        # plan 09 absent: notification is log-only.
        with (
            patch(
                "app.services.redemption_service.dispatch_alert",
                None,
            ),
            self.assertLogs("app.services.redemption_service", level="INFO"),
        ):
            redemption_service._notify_user(1, "redeem_available", "message", "https://x")

    def test_get_redemption_metrics(self):
        metrics = redemption_service.get_redemption_metrics()
        self.assertFalse(metrics["running"])
        self.assertTrue(metrics["dry_run"])
        self.assertEqual(metrics["check_interval_seconds"], 300)

    async def test_start_and_stop_redemption_manager(self):
        with (
            patch(
                "app.services.redemption_service.acquire_scheduler_lock",
                return_value=True,
            ) as mock_acquire,
            patch(
                "app.services.redemption_service.release_scheduler_lock",
            ) as mock_release,
            patch(
                "app.services.redemption_service.scheduler_heartbeat",
            ),
            patch(
                "app.services.redemption_service.run_redeem_cycle",
                new=AsyncMock(return_value={"attempts": 0}),
            ),
        ):
            await redemption_service.start_redemption_manager()
            # Yield so the loop task begins and acquires the lock.
            await asyncio.sleep(0)
            self.assertTrue(redemption_service.get_redemption_metrics()["running"])
            await redemption_service.stop_redemption_manager()
        self.assertFalse(redemption_service.get_redemption_metrics()["running"])
        mock_acquire.assert_called_once_with("redemption")
        mock_release.assert_called_once_with("redemption")


if __name__ == "__main__":
    unittest.main()
