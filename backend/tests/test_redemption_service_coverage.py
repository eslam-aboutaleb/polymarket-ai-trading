"""Additional coverage tests for :mod:`app.services.redemption_service`.

Complements ``tests/test_redemption_service.py`` (which owns the
happy paths and the known collectionId vectors). This file targets
the remaining statements:

* pure-helper edge cases — ``_to_float`` (None/""/non-numeric/
  non-finite), ``_is_valid_condition_id`` (non-string input),
  ``_sanitize_url_segment`` (180-char boundary, whitespace-only),
  ``determine_winner_index`` (invalid JSON string, tuples, floats,
  the exact 0.99 threshold);
* ``_parse_redeemable_position`` — every upstream field fallback
  (snake_case aliases, nested ``id``/``condition_id``, pos-level
  aliases, ``market: None``, missing resolved flags, title default);
* web3 plumbing — ``_connect_web3`` (first-RPC success, all-RPC
  failure, provider construction errors), ``_resolve_proxy_address``
  (contract error, empty/zero proxy), ``_daily_attempt_count``
  (query failure);
* ``_execute_redeem`` — ``CredentialStoreError``, receipt never
  arriving (attempt stays ``submitted``), tx-build failure on the
  EOA path, and the ``dry_run=None`` default;
* ``_run_redeem_in_thread`` — session open/close including the
  error path;
* orchestration — ``_redeem_user_positions`` (fetch error, non-dict
  and unparseable positions, worker-thread failure, None attempts),
  ``run_redeem_cycle`` (per-user error, every status counter),
  ``redeem_market_for_user`` (skip paths), ``_redeem_loop`` (summary
  logging, cycle error, heartbeat, lock release), the manager
  start/stop edges, ``_notify_user`` (dispatch failure fallback,
  inline delivery without a deep link);
* the defensive ``ImportError`` degradation for plan-09
  ``alert_service`` and plan-02 ``scheduler_lock`` (exercised by
  reloading the module with the dependency removed from
  ``sys.modules``).
"""

import asyncio
import importlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from web3 import Web3

import app.services.redemption_service as redemption_service
from app.security.credential_store import CredentialStoreError
from app.services.redemption_service import (
    _CONDITIONAL_TOKENS,
    _CTF_EXCHANGE,
    _POLYGON_RPCS,
    REDEEM_CHECK_INTERVAL,
    REDEEM_STATUS_CLAIM_VIA_UI,
    REDEEM_STATUS_CONFIRMED,
    REDEEM_STATUS_FAILED,
    REDEEM_STATUS_PENDING,
    REDEEM_STATUS_SUBMITTED,
    build_polymarket_event_url,
    compute_collection_id,
    determine_winner_index,
)

# ── shared fixtures (mirrors test_redemption_service.py) ──

CID_AB = "0x" + "ab" * 32
CID_AB_INDEX_0 = "9cdd4f07b364dd440fa1d10c130070958423f0e6b92e2c4aa3ef4dd91412db20"
CID_AB_INDEX_1 = "77dd4e5b43eb8e1cf61beb45da6aaec9aa91f52f53c698e3fd26b26c460a169f"
ZERO_ADDRESS = "0x" + "00" * 20
TEST_EOA = "0x" + "11" * 20
TEST_PROXY = "0x" + "22" * 20
TEST_PRIVATE_KEY = "0x" + "33" * 32  # test-only key, never a real wallet
TEST_TX_HASH = "0x" + "ab" * 32


def _fake_db(daily_count: int = 0) -> MagicMock:
    db = MagicMock()
    db.query.return_value.filter.return_value.count.return_value = daily_count
    return db


def _fake_w3(
    balance: int = 100,
    proxy_address: str | None = None,
    gas_wei: int | None = None,
    receipt_status: int = 1,
) -> MagicMock:
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


def _credentials(**overrides) -> dict:
    base = {"private_key": TEST_PRIVATE_KEY, "clob_creds": None}
    base.update(overrides)
    return base


# ── pure helpers ───────────────────────────────────────────


class DetermineWinnerIndexEdgeCases(unittest.TestCase):
    def test_invalid_json_string_yields_none(self):
        # json.loads raises ValueError -> prices treated as absent.
        self.assertIsNone(determine_winner_index("{not json"))

    def test_tuple_of_prices_is_accepted(self):
        self.assertEqual(determine_winner_index((0.0, 1.0)), 1)

    def test_float_prices_are_accepted(self):
        self.assertEqual(determine_winner_index([0.0, 1.0]), 1)

    def test_exact_threshold_is_a_winner(self):
        self.assertEqual(determine_winner_index(["0.99", "0.01"]), 0)

    def test_just_below_threshold_is_not_a_winner(self):
        self.assertIsNone(determine_winner_index(["0.989", "0.011"]))

    def test_empty_list_yields_none(self):
        self.assertIsNone(determine_winner_index([]))


class ToFloatTests(unittest.TestCase):
    def test_none_and_empty_string_return_the_default(self):
        self.assertIsNone(redemption_service._to_float(None))
        self.assertIsNone(redemption_service._to_float(""))
        self.assertEqual(redemption_service._to_float(None, default=1.5), 1.5)
        self.assertEqual(redemption_service._to_float("", default=2.5), 2.5)

    def test_non_numeric_strings_return_the_default(self):
        self.assertIsNone(redemption_service._to_float("abc"))
        self.assertIsNone(redemption_service._to_float("1.2.3"))

    def test_non_string_non_numeric_input_returns_the_default(self):
        # float(object()) raises TypeError, which is swallowed.
        self.assertIsNone(redemption_service._to_float(object()))

    def test_non_finite_values_return_the_default(self):
        self.assertIsNone(redemption_service._to_float(float("inf")))
        self.assertIsNone(redemption_service._to_float(float("-inf")))
        self.assertIsNone(redemption_service._to_float(float("nan")))
        self.assertIsNone(redemption_service._to_float("inf"))

    def test_valid_values_are_coerced(self):
        self.assertEqual(redemption_service._to_float("3.5"), 3.5)
        self.assertEqual(redemption_service._to_float(2), 2.0)
        self.assertEqual(redemption_service._to_float("  2.5  "), 2.5)
        self.assertEqual(redemption_service._to_float(0), 0.0)


class IsValidConditionIdTests(unittest.TestCase):
    def test_non_string_input_is_invalid(self):
        self.assertFalse(redemption_service._is_valid_condition_id(123))
        self.assertFalse(redemption_service._is_valid_condition_id(None))
        self.assertFalse(redemption_service._is_valid_condition_id(["0x" + "ab" * 32]))

    def test_valid_hex_is_accepted(self):
        self.assertTrue(redemption_service._is_valid_condition_id(CID_AB))
        self.assertTrue(redemption_service._is_valid_condition_id(CID_AB.removeprefix("0x")))
        self.assertTrue(redemption_service._is_valid_condition_id("0x" + "AB" * 32))

    def test_surrounding_whitespace_is_stripped(self):
        self.assertTrue(redemption_service._is_valid_condition_id(f"  {CID_AB}  "))

    def test_wrong_length_or_non_hex_is_invalid(self):
        self.assertFalse(redemption_service._is_valid_condition_id("0x" + "ab" * 31))
        self.assertFalse(redemption_service._is_valid_condition_id("0x" + "g" * 64))
        self.assertFalse(redemption_service._is_valid_condition_id(""))


class SanitizeUrlSegmentTests(unittest.TestCase):
    def test_none_empty_and_whitespace_only_return_none(self):
        self.assertIsNone(redemption_service._sanitize_url_segment(None))
        self.assertIsNone(redemption_service._sanitize_url_segment(""))
        self.assertIsNone(redemption_service._sanitize_url_segment("   "))

    def test_length_boundary_is_one_hundred_eighty(self):
        self.assertEqual(redemption_service._sanitize_url_segment("a" * 180), "a" * 180)
        self.assertIsNone(redemption_service._sanitize_url_segment("a" * 181))

    def test_safe_characters_pass_through(self):
        segment = "Market.Slug_~weird-ok"
        self.assertEqual(redemption_service._sanitize_url_segment(segment), segment)

    def test_unsafe_characters_are_rejected(self):
        self.assertIsNone(redemption_service._sanitize_url_segment("bad slug"))
        self.assertIsNone(redemption_service._sanitize_url_segment("bad/slug"))
        self.assertIsNone(redemption_service._sanitize_url_segment("bad?slug"))


class BuildPolymarketEventUrlEdgeCases(unittest.TestCase):
    def test_slugs_are_uri_encoded(self):
        # Every pattern-safe character survives encoding unchanged.
        self.assertEqual(
            build_polymarket_event_url("event.slug~1", "market-slug_2"),
            "https://polymarket.com/event/event.slug~1/market-slug_2",
        )

    def test_whitespace_around_slugs_is_trimmed(self):
        self.assertEqual(
            build_polymarket_event_url("  event-slug  ", "  market-slug  "),
            "https://polymarket.com/event/event-slug/market-slug",
        )


# ── position parsing fallbacks ───────────────────────────────


class ParseRedeemablePositionFallbackTests(unittest.TestCase):
    def test_nested_market_snake_case_fallbacks(self):
        parsed = redemption_service._parse_redeemable_position(
            {
                "conditionId": CID_AB,
                "size": "2.5",
                "market": {
                    "closed": True,
                    "title": "Snake title",
                    "outcome_prices": ["0.0", "1.0"],
                    "market_slug": "snake-market",
                    "event_slug": "snake-event",
                },
            }
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["title"], "Snake title")
        self.assertEqual(parsed["winner_index"], 1)
        self.assertEqual(parsed["market_slug"], "snake-market")
        self.assertEqual(parsed["event_slug"], "snake-event")

    def test_nested_market_id_fallback(self):
        # No pos-level conditionId: the nested market's "id" is used.
        parsed = redemption_service._parse_redeemable_position(
            {
                "size": "2.5",
                "market": {
                    "resolved": True,
                    "id": CID_AB,
                    "outcomePrices": ["1.0", "0.0"],
                },
            }
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["market_id"], CID_AB)
        self.assertEqual(parsed["winner_index"], 0)

    def test_nested_market_condition_id_fallback(self):
        parsed = redemption_service._parse_redeemable_position(
            {
                "size": "2.5",
                "market": {
                    "resolved": True,
                    "condition_id": CID_AB,
                    "outcomePrices": ["1.0", "0.0"],
                },
            }
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["market_id"], CID_AB)

    def test_pos_level_snake_case_fallbacks(self):
        # market is not a dict -> the pos-level aliases are read.
        parsed = redemption_service._parse_redeemable_position(
            {
                "condition_id": CID_AB,
                "size": "1.5",
                "outcome_prices": ["0.0", "1.0"],
                "market_slug": "pos-market",
                "event_slug": "pos-event",
                "question": "Pos question",
                "resolved": True,
            }
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["title"], "Pos question")
        self.assertEqual(parsed["market_slug"], "pos-market")
        self.assertEqual(parsed["event_slug"], "pos-event")
        self.assertEqual(parsed["winner_index"], 1)

    def test_none_market_uses_pos_level_fields(self):
        parsed = redemption_service._parse_redeemable_position(
            {
                "conditionId": CID_AB,
                "size": "3.0",
                "market": None,
                "outcomePrices": ["1.0", "0.0"],
                "resolved": True,
            }
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["market_id"], CID_AB)

    def test_missing_resolved_flags_still_redeemable(self):
        # No resolved/closed key at all -> treated as resolved.
        parsed = redemption_service._parse_redeemable_position(
            {
                "conditionId": CID_AB,
                "size": "3.0",
                "market": {"outcomePrices": ["1.0", "0.0"]},
            }
        )
        self.assertIsNotNone(parsed)

    def test_pos_condition_id_takes_precedence(self):
        other = "0x" + "cd" * 32
        parsed = redemption_service._parse_redeemable_position(
            {
                "conditionId": CID_AB,
                "condition_id": other,
                "size": "1.0",
                "market": CID_AB,
                "outcomePrices": ["1.0", "0.0"],
                "resolved": True,
            }
        )
        self.assertEqual(parsed["market_id"], CID_AB)

    def test_title_defaults_to_unknown(self):
        parsed = redemption_service._parse_redeemable_position(
            {
                "conditionId": CID_AB,
                "size": "1.0",
                "market": {"resolved": True, "outcomePrices": ["1.0", "0.0"]},
            }
        )
        self.assertEqual(parsed["title"], "Unknown")

    def test_invalid_size_string_is_skipped(self):
        self.assertIsNone(redemption_service._parse_redeemable_position(_raw_position(size="abc")))

    def test_missing_size_is_skipped(self):
        raw = _raw_position()
        del raw["size"]
        self.assertIsNone(redemption_service._parse_redeemable_position(raw))

    def test_non_finite_size_is_skipped(self):
        self.assertIsNone(redemption_service._parse_redeemable_position(_raw_position(size="inf")))


# ── web3 plumbing ──────────────────────────────────────────


class ConnectWeb3Tests(unittest.TestCase):
    def test_first_reachable_rpc_is_returned(self):
        w3 = MagicMock()
        w3.is_connected.return_value = True
        web3_cls = MagicMock(return_value=w3)

        with patch.object(redemption_service, "Web3", web3_cls):
            result = redemption_service._connect_web3()

        self.assertIs(result, w3)
        self.assertEqual(web3_cls.call_count, 1)
        web3_cls.HTTPProvider.assert_called_once_with(
            _POLYGON_RPCS[0], request_kwargs={"timeout": 10}
        )

    def test_none_when_no_rpc_is_reachable(self):
        w3 = MagicMock()
        w3.is_connected.return_value = False
        web3_cls = MagicMock(return_value=w3)

        with (
            patch.object(redemption_service, "Web3", web3_cls),
            self.assertLogs(redemption_service.logger, level="WARNING") as captured,
        ):
            result = redemption_service._connect_web3()

        self.assertIsNone(result)
        self.assertEqual(web3_cls.call_count, len(_POLYGON_RPCS))
        self.assertTrue(
            any("Could not connect to any Polygon RPC" in line for line in captured.output)
        )

    def test_provider_construction_errors_are_suppressed(self):
        web3_cls = MagicMock(side_effect=RuntimeError("dns failure"))

        with patch.object(redemption_service, "Web3", web3_cls):
            result = redemption_service._connect_web3()

        self.assertIsNone(result)
        self.assertEqual(web3_cls.call_count, len(_POLYGON_RPCS))


class ResolveProxyAddressTests(unittest.TestCase):
    def test_contract_error_returns_none(self):
        w3 = MagicMock()
        w3.eth.contract.side_effect = RuntimeError("contract call failed")

        with self.assertLogs(redemption_service.logger, level="WARNING"):
            result = redemption_service._resolve_proxy_address(w3, TEST_EOA)

        self.assertIsNone(result)

    def test_empty_proxy_string_returns_none(self):
        w3 = MagicMock()
        exchange = MagicMock()
        exchange.functions.getPolyProxyWalletAddress.return_value.call.return_value = ""
        w3.eth.contract.return_value = exchange

        self.assertIsNone(redemption_service._resolve_proxy_address(w3, TEST_EOA))

    def test_zero_address_returns_none(self):
        w3 = MagicMock()
        exchange = MagicMock()
        exchange.functions.getPolyProxyWalletAddress.return_value.call.return_value = ZERO_ADDRESS
        w3.eth.contract.return_value = exchange

        self.assertIsNone(redemption_service._resolve_proxy_address(w3, TEST_EOA))

    def test_proxy_address_is_returned(self):
        w3 = MagicMock()
        exchange = MagicMock()
        exchange.functions.getPolyProxyWalletAddress.return_value.call.return_value = TEST_PROXY
        w3.eth.contract.return_value = exchange

        self.assertEqual(redemption_service._resolve_proxy_address(w3, TEST_EOA), TEST_PROXY)


class DailyAttemptCountTests(unittest.TestCase):
    def test_counts_live_attempts(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.count.return_value = 3
        self.assertEqual(redemption_service._daily_attempt_count(db, 1), 3)

    def test_query_failure_returns_zero(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")

        with self.assertLogs(redemption_service.logger, level="WARNING"):
            self.assertEqual(redemption_service._daily_attempt_count(db, 1), 0)


# ── core redemption flow ───────────────────────────────────


class ExecuteRedeemEdgeCases(unittest.IsolatedAsyncioTestCase):
    def _execute(self, w3, position=None, db=None, dry_run=False):
        with patch(
            "app.services.redemption_service.load_wallet_credentials",
            return_value=_credentials(),
        ):
            return redemption_service._execute_redeem(
                db or _fake_db(),
                1,
                TEST_EOA,
                position or _position(),
                w3=w3,
                dry_run=dry_run,
            )

    def test_credential_store_error_fails_the_attempt(self):
        w3 = _fake_w3()

        with patch(
            "app.services.redemption_service.load_wallet_credentials",
            side_effect=CredentialStoreError("keystore unavailable"),
        ):
            attempt = redemption_service._execute_redeem(
                _fake_db(), 1, TEST_EOA, _position(), w3=w3, dry_run=False
            )

        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, REDEEM_STATUS_FAILED)
        self.assertEqual(attempt.error, "credential store error: keystore unavailable")
        w3.eth.send_raw_transaction.assert_not_called()

    def test_receipt_unavailable_keeps_attempt_submitted(self):
        w3 = _fake_w3()
        w3.eth.wait_for_transaction_receipt.side_effect = RuntimeError("receipt timeout")

        attempt = self._execute(w3)

        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, REDEEM_STATUS_SUBMITTED)
        self.assertEqual(attempt.tx_hash, TEST_TX_HASH)
        self.assertIn("receipt not available", attempt.error)
        self.assertIn("receipt timeout", attempt.error)

    def test_eoa_build_failure_records_failed(self):
        w3 = _fake_w3()

        with patch.object(
            redemption_service,
            "_build_redeem_tx",
            side_effect=RuntimeError("nonce fetch failed"),
        ):
            attempt = self._execute(w3)

        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, REDEEM_STATUS_FAILED)
        self.assertIn("nonce fetch failed", attempt.error)
        w3.eth.send_raw_transaction.assert_not_called()

    def test_dry_run_defaults_to_the_module_constant(self):
        w3 = _fake_w3()

        with (
            patch(
                "app.services.redemption_service.load_wallet_credentials",
                return_value=_credentials(),
            ),
            patch.object(redemption_service, "REDEEM_DRY_RUN", True),
        ):
            attempt = redemption_service._execute_redeem(
                _fake_db(), 1, TEST_EOA, _position(), w3=w3
            )

        self.assertEqual(attempt.status, REDEEM_STATUS_PENDING)
        w3.eth.send_raw_transaction.assert_not_called()

    def test_daily_count_query_failure_does_not_block_redemption(self):
        # A broken daily-count query fails open (count 0).
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        w3 = _fake_w3()

        with patch(
            "app.services.redemption_service.load_wallet_credentials",
            return_value=_credentials(),
        ):
            attempt = redemption_service._execute_redeem(
                db, 1, TEST_EOA, _position(), w3=w3, dry_run=True
            )

        self.assertEqual(attempt.status, REDEEM_STATUS_PENDING)


class RunRedeemInThreadTests(unittest.TestCase):
    def test_opens_its_own_session_and_closes_it(self):
        db = MagicMock()
        attempt = MagicMock()
        position = _position()

        with (
            patch.object(redemption_service, "SessionLocal", return_value=db),
            patch.object(redemption_service, "_execute_redeem", return_value=attempt) as execute,
        ):
            result = redemption_service._run_redeem_in_thread(1, TEST_EOA, position, dry_run=True)

        self.assertIs(result, attempt)
        execute.assert_called_once_with(db, 1, TEST_EOA, position, dry_run=True)
        db.close.assert_called_once_with()

    def test_closes_the_session_when_execution_raises(self):
        db = MagicMock()

        with (
            patch.object(redemption_service, "SessionLocal", return_value=db),
            patch.object(
                redemption_service,
                "_execute_redeem",
                side_effect=RuntimeError("boom"),
            ),
            self.assertRaises(RuntimeError),
        ):
            redemption_service._run_redeem_in_thread(1, TEST_EOA, _position())

        db.close.assert_called_once_with()


# ── orchestration ──────────────────────────────────────────


class RedeemUserPositionsTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, positions, **kwargs):
        service = MagicMock()
        service.get_positions = AsyncMock(return_value=positions, **kwargs)
        return service

    async def test_position_fetch_error_returns_empty(self):
        service = self._service([], side_effect=RuntimeError("positions api down"))

        with (
            patch(
                "app.services.polymarket_service.get_polymarket_service",
                return_value=service,
            ),
            self.assertLogs(redemption_service.logger, level="ERROR"),
        ):
            result = await redemption_service._redeem_user_positions(1, TEST_EOA)

        self.assertEqual(result, [])

    async def test_non_dict_and_unparseable_positions_are_skipped(self):
        service = self._service(["not-a-dict", {"size": "1"}, _raw_position()])
        db = _fake_db()
        w3 = _fake_w3()

        with (
            patch(
                "app.services.polymarket_service.get_polymarket_service",
                return_value=service,
            ),
            patch.object(redemption_service, "SessionLocal", return_value=db),
            patch.object(redemption_service, "_connect_web3", return_value=w3),
            patch(
                "app.services.redemption_service.load_wallet_credentials",
                return_value=_credentials(),
            ),
        ):
            result = await redemption_service._redeem_user_positions(1, TEST_EOA)

        # Only the well-formed position produced an attempt (dry-run).
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].status, REDEEM_STATUS_PENDING)

    async def test_worker_thread_failure_is_logged_and_skipped(self):
        service = self._service([_raw_position()])

        with (
            patch(
                "app.services.polymarket_service.get_polymarket_service",
                return_value=service,
            ),
            patch.object(
                redemption_service,
                "_run_redeem_in_thread",
                side_effect=RuntimeError("thread boom"),
            ),
            self.assertLogs(redemption_service.logger, level="ERROR"),
        ):
            result = await redemption_service._redeem_user_positions(1, TEST_EOA)

        self.assertEqual(result, [])

    async def test_none_attempts_are_not_appended(self):
        service = self._service([_raw_position()])

        with (
            patch(
                "app.services.polymarket_service.get_polymarket_service",
                return_value=service,
            ),
            patch.object(redemption_service, "_run_redeem_in_thread", return_value=None),
        ):
            result = await redemption_service._redeem_user_positions(1, TEST_EOA)

        self.assertEqual(result, [])

    async def test_zero_balance_attempts_are_not_appended(self):
        service = self._service([_raw_position()])
        db = _fake_db()
        w3 = _fake_w3(balance=0)

        with (
            patch(
                "app.services.polymarket_service.get_polymarket_service",
                return_value=service,
            ),
            patch.object(redemption_service, "SessionLocal", return_value=db),
            patch.object(redemption_service, "_connect_web3", return_value=w3),
            patch(
                "app.services.redemption_service.load_wallet_credentials",
                return_value=_credentials(),
            ),
        ):
            result = await redemption_service._redeem_user_positions(1, TEST_EOA)

        self.assertEqual(result, [])


class RunRedeemCycleTests(unittest.IsolatedAsyncioTestCase):
    def _cycle_db(self, user_rows):
        db = MagicMock()
        db.query.return_value.all.return_value = user_rows
        return db

    async def test_per_user_error_is_logged_and_skipped(self):
        db = self._cycle_db([(1, TEST_EOA), (2, TEST_EOA)])

        with (
            patch.object(redemption_service, "SessionLocal", return_value=db),
            patch.object(
                redemption_service,
                "_redeem_user_positions",
                new=AsyncMock(side_effect=RuntimeError("user boom")),
            ),
            self.assertLogs(redemption_service.logger, level="ERROR"),
        ):
            summary = await redemption_service.run_redeem_cycle()

        self.assertEqual(summary["users_checked"], 2)
        self.assertEqual(summary["attempts"], 0)

    async def test_every_status_is_counted(self):
        db = self._cycle_db([(1, TEST_EOA)])
        attempts = [
            SimpleNamespace(status=REDEEM_STATUS_SUBMITTED),
            SimpleNamespace(status=REDEEM_STATUS_CONFIRMED),
            SimpleNamespace(status=REDEEM_STATUS_FAILED),
            SimpleNamespace(status=REDEEM_STATUS_CLAIM_VIA_UI),
            SimpleNamespace(status=REDEEM_STATUS_PENDING),
        ]

        with (
            patch.object(redemption_service, "SessionLocal", return_value=db),
            patch.object(
                redemption_service,
                "_redeem_user_positions",
                new=AsyncMock(return_value=attempts),
            ),
        ):
            summary = await redemption_service.run_redeem_cycle()

        self.assertEqual(summary["users_checked"], 1)
        self.assertEqual(summary["attempts"], 5)
        self.assertEqual(summary["submitted"], 1)
        self.assertEqual(summary["confirmed"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["claim_via_ui"], 1)

    async def test_no_users_yields_an_empty_summary(self):
        db = self._cycle_db([])

        with patch.object(redemption_service, "SessionLocal", return_value=db):
            summary = await redemption_service.run_redeem_cycle()

        self.assertEqual(summary["users_checked"], 0)
        self.assertEqual(summary["attempts"], 0)
        self.assertIn("checked_at", summary)


class RedeemMarketForUserTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_dict_and_unparseable_positions_are_skipped(self):
        service = MagicMock()
        service.get_positions = AsyncMock(return_value=["nope", {"size": "1"}])

        with patch(
            "app.services.polymarket_service.get_polymarket_service",
            return_value=service,
        ):
            attempt = await redemption_service.redeem_market_for_user(1, TEST_EOA, CID_AB)

        self.assertIsNone(attempt)

    async def test_winner_index_override_is_applied(self):
        db = _fake_db()
        service = MagicMock()
        service.get_positions = AsyncMock(return_value=[_raw_position()])
        w3 = _fake_w3()

        with (
            patch.object(redemption_service, "SessionLocal", return_value=db),
            patch.object(redemption_service, "REDEEM_DRY_RUN", False),
            patch(
                "app.services.polymarket_service.get_polymarket_service",
                return_value=service,
            ),
            patch.object(redemption_service, "_connect_web3", return_value=w3),
            patch(
                "app.services.redemption_service.load_wallet_credentials",
                return_value=_credentials(),
            ),
        ):
            attempt = await redemption_service.redeem_market_for_user(
                1, TEST_EOA, CID_AB, winner_outcome_index=0
            )

        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.collection_id, CID_AB_INDEX_0)


class RedeemLoopTests(unittest.IsolatedAsyncioTestCase):
    async def _run_loop(self, cycle_mock):
        delays: list[float] = []

        async def _fake_sleep(seconds):
            delays.append(seconds)
            raise asyncio.CancelledError

        with (
            patch.object(redemption_service, "acquire_scheduler_lock", return_value=True),
            patch.object(redemption_service, "release_scheduler_lock") as release,
            patch.object(redemption_service, "scheduler_heartbeat") as heartbeat,
            patch.object(redemption_service, "run_redeem_cycle", new=cycle_mock),
            patch.object(redemption_service.asyncio, "sleep", new=_fake_sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await redemption_service._redeem_loop()
        return release, heartbeat, delays

    async def test_summary_is_logged_when_attempts_exist(self):
        cycle = AsyncMock(return_value={"attempts": 1})

        with self.assertLogs(redemption_service.logger, level="INFO") as captured:
            release, heartbeat, delays = await self._run_loop(cycle)

        cycle.assert_awaited_once()
        self.assertTrue(any("Redemption cycle summary" in line for line in captured.output))
        heartbeat.assert_called_once_with("redemption")
        release.assert_called_once_with("redemption")
        self.assertEqual(delays, [REDEEM_CHECK_INTERVAL])

    async def test_empty_summary_is_not_logged(self):
        cycle = AsyncMock(return_value={"attempts": 0})

        with self.assertLogs(redemption_service.logger, level="INFO") as captured:
            await self._run_loop(cycle)

        self.assertFalse(any("Redemption cycle summary" in line for line in captured.output))

    async def test_cycle_error_is_logged_and_the_loop_continues(self):
        cycle = AsyncMock(side_effect=RuntimeError("cycle boom"))

        with self.assertLogs(redemption_service.logger, level="ERROR") as captured:
            release, heartbeat, _delays = await self._run_loop(cycle)

        self.assertTrue(any("Redemption cycle error" in line for line in captured.output))
        heartbeat.assert_called_once_with("redemption")
        release.assert_called_once_with("redemption")

    async def test_lock_release_is_skipped_when_plan02_is_absent(self):
        cycle = AsyncMock(return_value={"attempts": 0})
        delays: list[float] = []

        async def _fake_sleep(seconds):
            delays.append(seconds)
            raise asyncio.CancelledError

        with (
            patch.object(redemption_service, "acquire_scheduler_lock", None),
            patch.object(redemption_service, "release_scheduler_lock", None),
            patch.object(redemption_service, "scheduler_heartbeat", None),
            patch.object(redemption_service, "run_redeem_cycle", new=cycle),
            patch.object(redemption_service.asyncio, "sleep", new=_fake_sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await redemption_service._redeem_loop()
        cycle.assert_awaited_once()


class ManagerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved_task = redemption_service._redeem_task
        redemption_service._redeem_task = None
        self.addCleanup(self._restore)

    def _restore(self):
        task = redemption_service._redeem_task
        if task is not None and not task.done():
            task.cancel()
        redemption_service._redeem_task = self._saved_task

    async def test_start_while_already_running_is_a_no_op(self):
        task = asyncio.create_task(asyncio.Event().wait())
        redemption_service._redeem_task = task
        self.addCleanup(task.cancel)

        with (
            patch.object(redemption_service, "acquire_scheduler_lock") as acquire,
            self.assertLogs(redemption_service.logger, level="WARNING") as captured,
        ):
            await redemption_service.start_redemption_manager()

        self.assertIs(redemption_service._redeem_task, task)
        acquire.assert_not_called()
        self.assertTrue(any("already running" in line for line in captured.output))

    async def test_start_replaces_a_finished_task(self):
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        redemption_service._redeem_task = finished

        with (
            patch.object(redemption_service, "acquire_scheduler_lock", return_value=True),
            patch.object(redemption_service, "release_scheduler_lock"),
            patch.object(redemption_service, "scheduler_heartbeat"),
            patch.object(
                redemption_service,
                "run_redeem_cycle",
                new=AsyncMock(return_value={"attempts": 0}),
            ),
        ):
            await redemption_service.start_redemption_manager()
            await asyncio.sleep(0)
            self.assertTrue(redemption_service.get_redemption_metrics()["running"])
            await redemption_service.stop_redemption_manager()

        self.assertFalse(redemption_service.get_redemption_metrics()["running"])

    async def test_stop_without_a_task_is_a_no_op(self):
        redemption_service._redeem_task = None
        await redemption_service.stop_redemption_manager()
        self.assertIsNone(redemption_service._redeem_task)

    async def test_stop_with_a_finished_task_clears_the_handle(self):
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        redemption_service._redeem_task = finished

        await redemption_service.stop_redemption_manager()

        self.assertIsNone(redemption_service._redeem_task)
        self.assertTrue(finished.done())

    def test_metrics_report_a_running_manager(self):
        task = MagicMock()
        task.done.return_value = False
        redemption_service._redeem_task = task
        try:
            metrics = redemption_service.get_redemption_metrics()
        finally:
            redemption_service._redeem_task = None
        self.assertTrue(metrics["running"])
        self.assertEqual(metrics["max_redeem_value_per_tx"], 10000.0)
        self.assertEqual(metrics["daily_redeem_cap_per_user"], 50)
        self.assertEqual(metrics["min_gas_balance_matic"], 0.05)


class NotifyUserTests(unittest.TestCase):
    def test_dispatch_failure_falls_back_to_logging(self):
        dispatch = AsyncMock(side_effect=RuntimeError("alert bus down"))

        with (
            patch.object(redemption_service, "dispatch_alert", dispatch),
            self.assertLogs(redemption_service.logger, level="WARNING") as captured,
        ):
            redemption_service._notify_user(1, "redeem_failed", "message", "https://link")

        dispatch.assert_called_once()
        self.assertTrue(any("alert_service dispatch failed" in line for line in captured.output))

    def test_inline_delivery_carries_the_deep_link(self):
        dispatch = AsyncMock()

        with patch.object(redemption_service, "dispatch_alert", dispatch):
            redemption_service._notify_user(2, "redeem_available", "message", "https://link")

        dispatch.assert_called_once()
        args, kwargs = dispatch.call_args
        self.assertEqual(args[0], "redeem_available")
        self.assertEqual(args[1], 2)
        self.assertEqual(args[2], {"message": "message", "deep_link": "https://link"})
        self.assertFalse(kwargs.get("background", True))

    def test_log_only_path_without_a_deep_link(self):
        with (
            patch.object(redemption_service, "dispatch_alert", None),
            self.assertLogs(redemption_service.logger, level="INFO") as captured,
        ):
            redemption_service._notify_user(3, "redeem_failed", "plain message")

        self.assertTrue(any("plain message" in line for line in captured.output))
        self.assertFalse(any("[link:" in line for line in captured.output))


# ── defensive import degradation (plan 09 / plan 02 absent) ─


class ImportDegradationTests(unittest.TestCase):
    def test_alert_service_absent_degrades_to_log_only(self):
        try:
            with patch.dict(sys.modules, {"app.services.alert_service": None}):
                reloaded = importlib.reload(redemption_service)
                self.assertIsNone(reloaded.dispatch_alert)
        finally:
            # Restore the real integration for every other test.
            importlib.reload(redemption_service)
        self.assertIsNotNone(redemption_service.dispatch_alert)

    def test_scheduler_lock_absent_degrades_to_unlocked_cycles(self):
        try:
            with patch.dict(sys.modules, {"app.utils.scheduler_lock": None}):
                reloaded = importlib.reload(redemption_service)
                self.assertIsNone(reloaded.acquire_scheduler_lock)
                self.assertIsNone(reloaded.release_scheduler_lock)
                self.assertIsNone(reloaded.scheduler_heartbeat)
        finally:
            importlib.reload(redemption_service)
        self.assertIsNotNone(redemption_service.acquire_scheduler_lock)


# ── collection id / url helpers (boundary coverage) ────────


class ComputeCollectionIdEdgeCases(unittest.TestCase):
    def test_whitespace_around_the_condition_id_is_stripped(self):
        self.assertEqual(
            compute_collection_id(f"  {CID_AB}  ", 1),
            CID_AB_INDEX_1,
        )

    def test_higher_winner_indices_produce_distinct_collections(self):
        first = compute_collection_id(CID_AB, 0)
        second = compute_collection_id(CID_AB, 2)
        self.assertNotEqual(first, second)
        # HexBytes.hex() carries no 0x prefix: a bare 64-char hex string.
        self.assertEqual(len(first), 64)
        self.assertEqual(len(second), 64)
        int(first, 16)
        int(second, 16)


if __name__ == "__main__":
    unittest.main()
