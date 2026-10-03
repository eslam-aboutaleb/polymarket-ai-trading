"""Coverage tests for ``app.services.pre_trade_gate``.

Exercises every layer of ``apply_global_safety_caps`` (halt, cooldown,
monthly loss, drawdown, total-loss halt, position cap with pending
exposure, daily loss), the risk aggregate queries, the order
idempotency store (shared cache + durable DB fallback, including
every best-effort failure path), and the CLOB order verification
helpers with all external I/O mocked.
"""

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.models.base import Base
from app.services import pre_trade_gate as gate
from app.services.pre_trade_gate import (
    IDEMPOTENCY_TTL_SECONDS,
    OrderIdempotencyKey,
    apply_global_safety_caps,
    get_idempotent_order_result,
    store_idempotent_order_result,
)

USER_ID = 1


def _settings(**overrides):
    values = {
        "user_id": USER_ID,
        "trading_halted": False,
        "halt_reason": None,
        "cooldown_until": None,
        "monthly_loss_limit": None,
        "max_drawdown_pct": 25.0,
        "peak_capital": None,
        "initial_capital": None,
        "total_loss_halt_pct": 40.0,
        "max_position_size": 100.0,
        "daily_loss_limit": 500.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeQuery:
    def __init__(self, value):
        self.value = value
        self.filter_args = []

    def filter(self, *args, **kwargs):
        self.filter_args.append(args)
        return self

    def scalar(self):
        return self.value


class _FakeDB:
    """DB double returning queued scalar values, one per query()."""

    def __init__(self, values=()):
        self._values = list(values)
        self.queries = []

    def query(self, *args, **kwargs):
        self.queries.append(args)
        if self._values:
            return _FakeQuery(self._values.pop(0))
        return _FakeQuery(0.0)


class _FakeCache:
    def __init__(self, fail_get=False, fail_set=False):
        self.store = {}
        self.set_calls = []
        self.fail_get = fail_get
        self.fail_set = fail_set

    def get(self, key):
        if self.fail_get:
            raise RuntimeError("cache read failed")
        return self.store.get(key)

    def set(self, key, value, ttl_seconds=None):
        if self.fail_set:
            raise RuntimeError("cache write failed")
        self.set_calls.append((key, value, ttl_seconds))
        self.store[key] = value


def _make_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class JsonHelperTests(unittest.TestCase):
    def test_round_trip_with_orjson(self):
        self.assertEqual(gate._json_loads(gate._json_dumps({"a": 1})), {"a": 1})

    def test_stdlib_fallback_when_orjson_unavailable(self):
        with (
            patch.object(gate, "ORJSON_AVAILABLE", False),
            patch.object(gate, "_orjson", None),
        ):
            self.assertEqual(gate._json_dumps({"a": 1}), '{"a": 1}')
            self.assertEqual(gate._json_loads('{"a": 1}'), {"a": 1})


class ToFloatTests(unittest.TestCase):
    def test_none_returns_default(self):
        self.assertEqual(gate._to_float(None), 0.0)
        self.assertEqual(gate._to_float(None, 5.0), 5.0)

    def test_numeric_strings_and_numbers(self):
        self.assertEqual(gate._to_float("12.5"), 12.5)
        self.assertEqual(gate._to_float(3), 3.0)
        self.assertEqual(gate._to_float(0), 0.0)

    def test_invalid_values_return_default(self):
        self.assertEqual(gate._to_float("abc"), 0.0)
        self.assertEqual(gate._to_float("abc", 7.0), 7.0)
        self.assertEqual(gate._to_float([1, 2]), 0.0)
        self.assertEqual(gate._to_float({"a": 1}, 2.5), 2.5)


class AggregateTests(unittest.TestCase):
    def _db_with(self, value):
        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = value
        return db

    def test_daily_loss_so_far_returns_abs_negative_sum(self):
        self.assertEqual(gate._daily_loss_so_far(self._db_with(-100.0), USER_ID), 100.0)

    def test_daily_loss_so_far_none_result_is_zero(self):
        self.assertEqual(gate._daily_loss_so_far(self._db_with(None), USER_ID), 0.0)

    def test_monthly_loss_so_far(self):
        self.assertEqual(gate._monthly_loss_so_far(self._db_with(-50.0), USER_ID), 50.0)
        self.assertEqual(gate._monthly_loss_so_far(self._db_with(None), USER_ID), 0.0)

    def test_total_pnl_since_start_keeps_sign(self):
        self.assertEqual(gate._total_pnl_since_start(self._db_with(-30.0), USER_ID), -30.0)
        self.assertEqual(gate._total_pnl_since_start(self._db_with(12.5), USER_ID), 12.5)
        self.assertEqual(gate._total_pnl_since_start(self._db_with(None), USER_ID), 0.0)

    def test_pending_exposure_so_far(self):
        self.assertEqual(gate._pending_exposure_so_far(self._db_with(-20.0), USER_ID), 20.0)
        self.assertEqual(gate._pending_exposure_so_far(self._db_with(None), USER_ID), 0.0)


class ApplyGlobalSafetyCapsTests(unittest.TestCase):
    def test_zero_size_is_rejected(self):
        size, reason = apply_global_safety_caps(_settings(), 0, _FakeDB())
        self.assertEqual(size, 0.0)
        self.assertEqual(reason, "Calculated trade size is zero")

    def test_negative_size_is_rejected(self):
        size, reason = apply_global_safety_caps(_settings(), -10, _FakeDB())
        self.assertEqual(size, 0.0)
        self.assertEqual(reason, "Calculated trade size is zero")

    def test_non_numeric_size_is_rejected(self):
        size, reason = apply_global_safety_caps(_settings(), "not-a-number", _FakeDB())
        self.assertEqual(size, 0.0)
        self.assertEqual(reason, "Calculated trade size is zero")

    def test_halted_settings_return_custom_reason(self):
        size, reason = apply_global_safety_caps(
            _settings(trading_halted=True, halt_reason="Risk halt"), 50, _FakeDB()
        )
        self.assertEqual(size, 0.0)
        self.assertEqual(reason, "Risk halt")

    def test_halted_settings_return_default_reason(self):
        size, reason = apply_global_safety_caps(
            _settings(trading_halted=True, halt_reason=None), 50, _FakeDB()
        )
        self.assertEqual(size, 0.0)
        self.assertEqual(reason, "Trading halted by risk system")

    def test_active_cooldown_blocks_trading(self):
        cooldown_until = datetime.now(UTC) + timedelta(minutes=10)
        size, reason = apply_global_safety_caps(
            _settings(cooldown_until=cooldown_until), 50, _FakeDB()
        )
        self.assertEqual(size, 0.0)
        self.assertTrue(reason.startswith("Cooldown active ("))
        self.assertTrue(reason.endswith("m remaining)"))

    def test_expired_cooldown_is_cleared_and_trading_continues(self):
        settings = _settings(cooldown_until=datetime.now(UTC) - timedelta(minutes=1))
        size, reason = apply_global_safety_caps(settings, 50, _FakeDB())
        self.assertIsNone(settings.cooldown_until)
        self.assertEqual((size, reason), (50.0, None))

    def test_monthly_loss_limit_reached(self):
        settings = _settings(monthly_loss_limit=100.0)
        size, reason = apply_global_safety_caps(settings, 50, _FakeDB([100.0]))
        self.assertEqual((size, reason), (0.0, "Monthly loss limit reached"))

    def test_monthly_loss_limit_caps_size(self):
        settings = _settings(monthly_loss_limit=100.0)
        size, reason = apply_global_safety_caps(settings, 100, _FakeDB([40.0, 0.0, 0.0]))
        self.assertEqual((size, reason), (60.0, None))

    def test_max_drawdown_breach_halts_trading(self):
        settings = _settings(max_drawdown_pct=25.0, peak_capital=100.0, initial_capital=100.0)
        size, reason = apply_global_safety_caps(settings, 50, _FakeDB([-30.0]))
        self.assertEqual(size, 0.0)
        self.assertEqual(reason, "Max drawdown 25.0% breached (current: 30.0%)")
        self.assertTrue(settings.trading_halted)
        self.assertEqual(settings.halt_reason, reason)

    def test_new_peak_capital_is_recorded(self):
        settings = _settings(max_drawdown_pct=25.0, peak_capital=100.0, initial_capital=100.0)
        size, reason = apply_global_safety_caps(settings, 100, _FakeDB([50.0, 50.0, 0.0, 0.0]))
        self.assertEqual((size, reason), (100.0, None))
        self.assertEqual(settings.peak_capital, 150.0)

    def test_total_loss_halt_breach(self):
        settings = _settings(max_drawdown_pct=0.0, total_loss_halt_pct=40.0, initial_capital=100.0)
        size, reason = apply_global_safety_caps(settings, 50, _FakeDB([-40.0]))
        self.assertEqual(size, 0.0)
        self.assertEqual(reason, "Total loss 40.0% of initial capital breached")
        self.assertTrue(settings.trading_halted)

    def test_total_loss_halt_not_breached_on_profit(self):
        settings = _settings(max_drawdown_pct=0.0, total_loss_halt_pct=40.0, initial_capital=100.0)
        size, reason = apply_global_safety_caps(settings, 50, _FakeDB([30.0, 0.0, 0.0]))
        self.assertEqual((size, reason), (50.0, None))

    def test_max_position_reached_with_pending_exposure(self):
        settings = _settings(max_position_size=100.0)
        size, reason = apply_global_safety_caps(settings, 50, _FakeDB([100.0]))
        self.assertEqual((size, reason), (0.0, "Max position size reached (pending exposure)"))

    def test_max_position_size_caps_for_pending_exposure(self):
        settings = _settings(max_position_size=100.0)
        size, reason = apply_global_safety_caps(settings, 100, _FakeDB([60.0, 0.0]))
        self.assertEqual((size, reason), (40.0, None))

    def test_daily_loss_limit_reached(self):
        settings = _settings(daily_loss_limit=500.0)
        size, reason = apply_global_safety_caps(settings, 50, _FakeDB([0.0, 500.0]))
        self.assertEqual((size, reason), (0.0, "Daily loss limit reached"))

    def test_daily_loss_limit_caps_size(self):
        settings = _settings(daily_loss_limit=500.0)
        size, reason = apply_global_safety_caps(settings, 100, _FakeDB([0.0, 450.0]))
        self.assertEqual((size, reason), (50.0, None))

    def test_daily_loss_limit_defaults_to_500(self):
        settings = _settings(daily_loss_limit=None)
        size, reason = apply_global_safety_caps(settings, 50, _FakeDB([0.0, 600.0]))
        self.assertEqual((size, reason), (0.0, "Daily loss limit reached"))

    def test_all_layers_pass_and_size_is_capped_by_monthly_limit(self):
        settings = _settings(
            monthly_loss_limit=100.0,
            max_drawdown_pct=25.0,
            peak_capital=100.0,
            initial_capital=100.0,
            total_loss_halt_pct=40.0,
            max_position_size=100.0,
            daily_loss_limit=500.0,
        )
        size, reason = apply_global_safety_caps(settings, 100, _FakeDB([10.0, 5.0, 5.0, 0.0, 10.0]))
        self.assertEqual((size, reason), (90.0, None))
        self.assertEqual(settings.peak_capital, 105.0)

    def test_size_is_rounded_to_two_decimals(self):
        size, reason = apply_global_safety_caps(_settings(), 10.006, _FakeDB([0.0, 0.0]))
        self.assertEqual((size, reason), (10.01, None))

    def test_disabled_layers_are_skipped(self):
        settings = _settings(
            monthly_loss_limit=0.0,
            max_drawdown_pct=0.0,
            total_loss_halt_pct=0.0,
            max_position_size=0.0,
        )
        size, reason = apply_global_safety_caps(settings, 75, _FakeDB([0.0]))
        self.assertEqual((size, reason), (75.0, None))


class IdempotencyKeyModelTests(unittest.TestCase):
    def test_repr_includes_user_and_key_prefix(self):
        key = OrderIdempotencyKey(user_id=7, idempotency_key="a" * 40)
        self.assertIn("user=7", repr(key))
        self.assertIn("a" * 16, repr(key))

    def test_cache_key_format(self):
        self.assertEqual(gate._idempotency_cache_key(USER_ID, "abc"), f"order:{USER_ID}:abc")
        self.assertEqual(gate._idempotency_cache_key(None, "x"), "order:None:x")

    def test_get_idempotency_cache_lazy_init(self):
        original = gate._idempotency_cache
        gate._idempotency_cache = None
        self.addCleanup(setattr, gate, "_idempotency_cache", original)
        cache = gate._get_idempotency_cache()
        self.assertIsNotNone(cache)
        self.assertIs(gate._idempotency_cache, cache)
        self.assertIs(gate._get_idempotency_cache(), cache)


class GetIdempotentOrderResultTests(unittest.TestCase):
    def test_missing_key_returns_none_without_querying(self):
        db = MagicMock()
        self.assertIsNone(get_idempotent_order_result(db, USER_ID, None))
        self.assertIsNone(get_idempotent_order_result(db, USER_ID, ""))
        db.query.assert_not_called()

    def test_cache_hit_returns_cached_result(self):
        cache = _FakeCache()
        cache.store[gate._idempotency_cache_key(USER_ID, "key-1")] = {"success": True}
        db = MagicMock()
        with patch.object(gate, "_idempotency_cache", cache):
            result = get_idempotent_order_result(db, USER_ID, "key-1")
        self.assertEqual(result, {"success": True})
        db.query.assert_not_called()

    def test_db_fallback_returns_stored_result(self):
        db = _make_db()
        self.addCleanup(db.close)
        db.add(
            OrderIdempotencyKey(
                user_id=USER_ID, idempotency_key="key-1", result_json='{"success": true}'
            )
        )
        db.commit()
        with patch.object(gate, "_idempotency_cache", _FakeCache()):
            result = get_idempotent_order_result(db, USER_ID, "key-1")
        self.assertEqual(result, {"success": True})

    def test_cache_failure_falls_back_to_db(self):
        db = _make_db()
        self.addCleanup(db.close)
        db.add(
            OrderIdempotencyKey(
                user_id=USER_ID, idempotency_key="key-1", result_json='{"success": true}'
            )
        )
        db.commit()
        cache = _FakeCache(fail_get=True)
        with (
            patch.object(gate, "_idempotency_cache", cache),
            self.assertLogs(gate.logger, level="WARNING") as captured,
        ):
            result = get_idempotent_order_result(db, USER_ID, "key-1")
        self.assertEqual(result, {"success": True})
        self.assertTrue(any("Idempotency cache read failed" in line for line in captured.output))

    def test_complete_miss_returns_none(self):
        db = _make_db()
        self.addCleanup(db.close)
        with patch.object(gate, "_idempotency_cache", _FakeCache()):
            self.assertIsNone(get_idempotent_order_result(db, USER_ID, "missing"))

    def test_db_failure_returns_none(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        with (
            patch.object(gate, "_idempotency_cache", _FakeCache()),
            self.assertLogs(gate.logger, level="WARNING") as captured,
        ):
            self.assertIsNone(get_idempotent_order_result(db, USER_ID, "key-1"))
        self.assertTrue(any("Idempotency DB read failed" in line for line in captured.output))

    def test_corrupted_row_json_returns_none(self):
        db = _make_db()
        self.addCleanup(db.close)
        db.add(
            OrderIdempotencyKey(user_id=USER_ID, idempotency_key="key-1", result_json="not-json{")
        )
        db.commit()
        with patch.object(gate, "_idempotency_cache", _FakeCache()):
            self.assertIsNone(get_idempotent_order_result(db, USER_ID, "key-1"))


class StoreIdempotentOrderResultTests(unittest.TestCase):
    RESULT = {"success": True, "order_hash": "0xorder-1"}

    def test_missing_key_is_a_no_op(self):
        db = MagicMock()
        cache = _FakeCache()
        store_idempotent_order_result(db, USER_ID, None, self.RESULT)
        store_idempotent_order_result(db, USER_ID, "", self.RESULT)
        self.assertEqual(cache.set_calls, [])
        db.query.assert_not_called()

    def test_store_writes_cache_and_inserts_db_row(self):
        db = _make_db()
        self.addCleanup(db.close)
        cache = _FakeCache()
        with patch.object(gate, "_idempotency_cache", cache):
            store_idempotent_order_result(db, USER_ID, "key-1", self.RESULT)
        self.assertEqual(
            cache.set_calls,
            [(gate._idempotency_cache_key(USER_ID, "key-1"), self.RESULT, IDEMPOTENCY_TTL_SECONDS)],
        )
        row = db.query(OrderIdempotencyKey).filter_by(idempotency_key="key-1").first()
        self.assertIsNotNone(row)
        self.assertEqual(row.user_id, USER_ID)
        self.assertEqual(gate._json_loads(row.result_json), self.RESULT)

    def test_store_updates_existing_row(self):
        db = _make_db()
        self.addCleanup(db.close)
        db.add(
            OrderIdempotencyKey(
                user_id=USER_ID, idempotency_key="key-1", result_json='{"success": false}'
            )
        )
        db.commit()
        with patch.object(gate, "_idempotency_cache", _FakeCache()):
            store_idempotent_order_result(db, USER_ID, "key-1", self.RESULT)
        rows = db.query(OrderIdempotencyKey).filter_by(idempotency_key="key-1").all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(gate._json_loads(rows[0].result_json), self.RESULT)

    def test_cache_failure_still_persists_to_db(self):
        db = _make_db()
        self.addCleanup(db.close)
        cache = _FakeCache(fail_set=True)
        with (
            patch.object(gate, "_idempotency_cache", cache),
            self.assertLogs(gate.logger, level="WARNING") as captured,
        ):
            store_idempotent_order_result(db, USER_ID, "key-1", self.RESULT)
        self.assertTrue(any("Idempotency cache write failed" in line for line in captured.output))
        row = db.query(OrderIdempotencyKey).filter_by(idempotency_key="key-1").first()
        self.assertIsNotNone(row)

    def test_db_failure_rolls_back_and_does_not_raise(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        cache = _FakeCache()
        with (
            patch.object(gate, "_idempotency_cache", cache),
            self.assertLogs(gate.logger, level="WARNING") as captured,
        ):
            store_idempotent_order_result(db, USER_ID, "key-1", self.RESULT)
        db.rollback.assert_called_once()
        self.assertTrue(any("Idempotency DB write failed" in line for line in captured.output))


class BuildClobClientTests(unittest.TestCase):
    CREDS = {"api_key": "k", "api_secret": "s", "api_passphrase": "p"}

    def test_builds_client_with_provided_creds(self):
        client = MagicMock()
        creds = MagicMock()
        with (
            patch("py_clob_client.client.ClobClient", return_value=client) as ctor,
            patch("py_clob_client.clob_types.ApiCreds", return_value=creds) as api_creds,
        ):
            result = gate.build_clob_client("0xpk", self.CREDS, proxy_address="0xproxy")
        self.assertIs(result, client)
        ctor.assert_called_once_with(
            host=gate.POLYMARKET_CLOB_API,
            chain_id=137,
            key="0xpk",
            signature_type=1,
            funder="0xproxy",
        )
        api_creds.assert_called_once_with(api_key="k", api_secret="s", api_passphrase="p")
        client.set_api_creds.assert_called_once_with(creds)

    def test_builds_client_deriving_creds_when_absent(self):
        client = MagicMock()
        derived = MagicMock()
        client.create_or_derive_api_creds.return_value = derived
        with (
            patch("py_clob_client.client.ClobClient", return_value=client),
            patch("py_clob_client.clob_types.ApiCreds"),
        ):
            result = gate.build_clob_client("0xpk", None)
        self.assertIs(result, client)
        client.create_or_derive_api_creds.assert_called_once_with()
        client.set_api_creds.assert_called_once_with(derived)


class ComputeOrderHashTests(unittest.TestCase):
    def _client(self):
        client = MagicMock()
        client.get_neg_risk.return_value = True
        client.get_exchange_address.return_value = "0xexchange"
        client.chain_id = 137
        return client

    def test_recomputes_eip712_struct_hash(self):
        client = self._client()
        signed = MagicMock()
        signed.order.signable_bytes.return_value = b"struct-bytes"
        digest = MagicMock()
        digest.hex.return_value = "deadbeef"
        with (
            patch("eth_utils.keccak", return_value=digest) as keccak_mock,
            patch("poly_eip712_structs.make_domain", return_value="domain") as make_domain,
            patch("py_order_utils.utils.prepend_zx", return_value="0xdeadbeef") as prepend,
        ):
            result = gate.compute_order_hash(client, signed, "0xtoken")
        self.assertEqual(result, "0xdeadbeef")
        client.get_neg_risk.assert_called_once_with("0xtoken")
        client.get_exchange_address.assert_called_once_with(neg_risk=True)
        make_domain.assert_called_once_with(
            name="Polymarket CTF Exchange",
            version="1",
            chainId="137",
            verifyingContract="0xexchange",
        )
        signed.order.signable_bytes.assert_called_once_with(domain="domain")
        keccak_mock.assert_called_once_with(b"struct-bytes")
        prepend.assert_called_once_with("deadbeef")


class ClobTradesForOrderTests(unittest.TestCase):
    def _client(self):
        client = MagicMock()
        client.signer = "signer"
        client.creds = "creds"
        client.host = "https://clob.example"
        return client

    def _run(self, resp):
        client = self._client()
        with (
            patch("py_clob_client.clob_types.RequestArgs"),
            patch(
                "py_clob_client.headers.headers.create_level_2_headers",
                return_value={"Authorization": "sig"},
            ),
            patch("py_clob_client.http_helpers.helpers.get", return_value=resp) as clob_get,
        ):
            return gate.clob_trades_for_order(client, "0xorder"), clob_get

    def test_dict_response_with_list_data(self):
        result, clob_get = self._run({"data": [{"id": 1}]})
        self.assertEqual(result, [{"id": 1}])
        clob_get.assert_called_once_with(
            "https://clob.example/data/trades?order_id=0xorder",
            headers={"Authorization": "sig"},
        )

    def test_dict_response_with_non_list_data_returns_empty(self):
        result, _ = self._run({"data": "unexpected"})
        self.assertEqual(result, [])

    def test_list_response_is_returned_directly(self):
        result, _ = self._run([{"id": 1}, {"id": 2}])
        self.assertEqual(result, [{"id": 1}, {"id": 2}])

    def test_unexpected_response_type_returns_empty(self):
        result, _ = self._run("not-a-dict-or-list")
        self.assertEqual(result, [])


class OrderLandedOnClobTests(unittest.TestCase):
    def _client(self):
        client = MagicMock()
        client.chain_id = 137
        return client

    def test_hash_computation_failure_returns_none(self):
        client = self._client()
        signed = MagicMock()
        with (
            patch.object(gate, "compute_order_hash", side_effect=RuntimeError("hash failed")),
            self.assertLogs(gate.logger, level="WARNING") as captured,
        ):
            self.assertIsNone(gate.order_landed_on_clob(client, signed, "0xtoken"))
        self.assertTrue(any("Could not compute order hash" in line for line in captured.output))

    def test_open_order_returns_hash(self):
        client = self._client()
        client.get_order.return_value = {"order_id": "0xhash"}
        signed = MagicMock()
        with (
            patch.object(gate, "compute_order_hash", return_value="0xhash"),
            patch.object(gate, "clob_trades_for_order") as trades_mock,
        ):
            self.assertEqual(gate.order_landed_on_clob(client, signed, "0xtoken"), "0xhash")
        trades_mock.assert_not_called()

    def test_falsy_open_order_falls_through_to_trade_feed(self):
        client = self._client()
        client.get_order.return_value = None
        signed = MagicMock()
        with (
            patch.object(gate, "compute_order_hash", return_value="0xhash"),
            patch.object(gate, "clob_trades_for_order", return_value=[{"id": 1}]),
        ):
            self.assertEqual(gate.order_landed_on_clob(client, signed, "0xtoken"), "0xhash")

    def test_filled_order_found_in_trade_feed(self):
        client = self._client()
        client.get_order.side_effect = RuntimeError("404")
        signed = MagicMock()
        with (
            patch.object(gate, "compute_order_hash", return_value="0xhash"),
            patch.object(gate, "clob_trades_for_order", return_value=[{"id": 1}]),
        ):
            self.assertEqual(gate.order_landed_on_clob(client, signed, "0xtoken"), "0xhash")

    def test_no_open_order_and_no_trades_is_safe_to_retry(self):
        client = self._client()
        client.get_order.side_effect = RuntimeError("404")
        signed = MagicMock()
        with (
            patch.object(gate, "compute_order_hash", return_value="0xhash"),
            patch.object(gate, "clob_trades_for_order", return_value=[]),
        ):
            self.assertEqual(gate.order_landed_on_clob(client, signed, "0xtoken"), "")

    def test_trade_lookup_failure_returns_none(self):
        client = self._client()
        client.get_order.side_effect = RuntimeError("404")
        signed = MagicMock()
        with (
            patch.object(gate, "compute_order_hash", return_value="0xhash"),
            patch.object(gate, "clob_trades_for_order", side_effect=RuntimeError("feed down")),
            self.assertLogs(gate.logger, level="WARNING") as captured,
        ):
            self.assertIsNone(gate.order_landed_on_clob(client, signed, "0xtoken"))
        self.assertTrue(any("CLOB trade lookup failed" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
