"""Behavioural tests for :mod:`app.services.stop_loss_monitor`.

Covers every branch of the background stop-loss / take-profit engine:

* ``start_stop_loss_monitor`` / ``stop_stop_loss_monitor`` task lifecycle,
  including the idempotent "already running" guard.
* ``_monitor_loop`` – normal cycles, swallowed check errors, and the
  ``CancelledError`` pass-through that lets shutdown work.
* ``_check_stop_losses`` – the two-query cycle, the empty-result and
  missing-wallet short circuits, and per-user dispatch.
* ``_check_user_price_orders`` – credential-store failures, the
  ``price <= stop_price`` / ``price >= take_profit_price`` boundary
  conditions, and orders with no available price.
* ``_fetch_live_prices`` – the POLY_PROXY -> EOA -> gnosis signature-type
  retry ladder, ``token_id``/``asset_id`` response keys, and midpoint
  back-fill.
* ``_execute_stop_loss`` / ``_execute_take_profit`` – success, broker
  rejection, raised exception, and the rollback fallback.

All external I/O (CLOB client, Web3 proxy lookup, credential store, order
placement) is mocked; no network calls are made.
"""

import asyncio
import contextlib
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import app.services.stop_loss_monitor as stop_loss_monitor
from app.security.credential_store import CredentialStoreError
from app.services.polymarket_service import PolymarketService


class _LoopBreak(Exception):
    """Sentinel used to break out of an otherwise infinite monitor loop."""


def _canned(*results):
    """Side-effect callable yielding ``results`` in order.

    Raises ``AssertionError`` rather than ``StopIteration`` when exhausted:
    a bare ``StopIteration`` escaping an ``asyncio.to_thread`` worker can never
    be delivered to a Future, which hangs the test instead of failing it.
    """
    remaining = list(results)

    def _next(*_args, **_kwargs):
        if not remaining:
            raise AssertionError(f"side_effect exhausted after {len(results)} call(s)")
        result = remaining.pop(0)
        if isinstance(result, type) and issubclass(result, BaseException):
            result = result()
        if isinstance(result, BaseException):
            raise result
        return result

    return _next


class _ClobFactory:
    """``_get_clob_client`` double: fails the first N attempts, then succeeds."""

    def __init__(self, clob, failures=()):
        self._clob = clob
        self._failures = list(failures)

    def __call__(self, *_args, **_kwargs):
        if self._failures:
            raise self._failures.pop(0)
        return self._clob


async def _block_forever():
    """Stand-in for ``_monitor_loop`` that parks until it is cancelled."""
    await asyncio.Event().wait()


def _sl_order(order_id, user_id, token_id, stop_price, wallet="0xwallet", size=1.0):
    """Build a stand-in ``StopLossOrder`` row."""
    return SimpleNamespace(
        id=order_id,
        user_id=user_id,
        token_id=token_id,
        stop_price=stop_price,
        size=size,
        status="active",
        order_hash=None,
        executed_price=None,
        triggered_at=None,
        updated_at=None,
        user=None if wallet is None else SimpleNamespace(wallet_address=wallet),
    )


def _tp_order(order_id, user_id, token_id, take_profit_price, wallet="0xwallet", size=2.0):
    """Build a stand-in ``TakeProfitOrder`` row."""
    return SimpleNamespace(
        id=order_id,
        user_id=user_id,
        token_id=token_id,
        take_profit_price=take_profit_price,
        size=size,
        status="active",
        order_hash=None,
        executed_price=None,
        triggered_at=None,
        updated_at=None,
        user=None if wallet is None else SimpleNamespace(wallet_address=wallet),
    )


def _query_double(result):
    """Chainable query mock whose ``.all()`` yields ``result``."""
    query = MagicMock()
    query.options.return_value = query
    query.filter.return_value = query
    query.order_by.return_value = query
    query.group_by.return_value = query
    query.having.return_value = query
    query.all.return_value = result
    return query


def _session_with(sl_orders, tp_orders):
    """Magic session returning ``sl_orders`` then ``tp_orders`` from ``query``."""
    db = MagicMock()
    db.query.side_effect = [
        _query_double(sl_orders),
        _query_double(tp_orders),
    ]
    return db


class _FakeClobClient:
    """CLOB client double that records how it was probed."""

    def __init__(self, prices=None, midpoints=None, probe_error=None):
        self._prices = [] if prices is None else prices
        self._midpoints = [] if midpoints is None else midpoints
        self._probe_error = probe_error
        self.price_calls = []
        self.midpoint_calls = []

    def get_last_trades_prices(self, books):
        self.price_calls.append([b.token_id for b in books])
        if len(books) == 1 and self._probe_error is not None:
            raise self._probe_error
        return self._prices

    def get_midpoints(self, books):
        self.midpoint_calls.append([b.token_id for b in books])
        return self._midpoints


def _service_double(clob=None, clob_factory=None):
    """Polymarket service double exposing the real ``_to_float`` converter."""
    service = MagicMock()
    service._to_float = PolymarketService._to_float
    service._get_clob_client.return_value = clob
    if clob_factory is not None:
        service._get_clob_client.side_effect = clob_factory
    return service


_EOA_ADDRESS = "0x00000000000000000000000000000000000000e0a"


class MonitorLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """``start_stop_loss_monitor`` / ``stop_stop_loss_monitor`` task handling."""

    async def asyncTearDown(self):
        task = stop_loss_monitor._monitor_task
        stop_loss_monitor._monitor_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _start_monitor(self):
        """Start the monitor with a loop that parks, and let the task run."""
        loop_mock = AsyncMock(side_effect=_block_forever)
        patcher = patch.object(stop_loss_monitor, "_monitor_loop", new=loop_mock)
        patcher.start()
        self.addCleanup(patcher.stop)
        await stop_loss_monitor.start_stop_loss_monitor()
        # Yield control so the freshly created task actually begins executing.
        await asyncio.sleep(0)
        return loop_mock, stop_loss_monitor._monitor_task

    async def test_start_creates_background_task_and_stop_cancels_it(self):
        loop_mock, task = await self._start_monitor()

        self.assertIsNotNone(task)
        self.assertFalse(task.done())
        self.assertEqual(loop_mock.await_count, 1)

        await stop_loss_monitor.stop_stop_loss_monitor()

        self.assertIsNone(stop_loss_monitor._monitor_task)
        self.assertTrue(task.cancelled())

    async def test_start_is_idempotent_while_task_alive(self):
        loop_mock, first = await self._start_monitor()

        await stop_loss_monitor.start_stop_loss_monitor()
        await asyncio.sleep(0)

        self.assertIs(first, stop_loss_monitor._monitor_task)
        # The second start must not have spawned a duplicate loop.
        self.assertEqual(loop_mock.await_count, 1)
        await stop_loss_monitor.stop_stop_loss_monitor()

    async def test_start_restarts_after_previous_task_finished(self):
        loop_mock, first = await self._start_monitor()
        await stop_loss_monitor.stop_stop_loss_monitor()

        await stop_loss_monitor.start_stop_loss_monitor()
        await asyncio.sleep(0)
        second = stop_loss_monitor._monitor_task

        self.assertIsNot(first, second)
        self.assertFalse(second.done())
        self.assertEqual(loop_mock.await_count, 2)
        await stop_loss_monitor.stop_stop_loss_monitor()

    async def test_stop_with_no_task_is_a_noop(self):
        stop_loss_monitor._monitor_task = None

        await stop_loss_monitor.stop_stop_loss_monitor()

        self.assertIsNone(stop_loss_monitor._monitor_task)

    async def test_stop_does_not_await_an_already_finished_task(self):
        loop_mock, task = await self._start_monitor()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self.assertTrue(task.done())
        awaits_before = loop_mock.await_count

        await stop_loss_monitor.stop_stop_loss_monitor()

        self.assertTrue(task.cancelled())
        self.assertIsNone(stop_loss_monitor._monitor_task)
        self.assertEqual(loop_mock.await_count, awaits_before)


class MonitorLoopTests(unittest.IsolatedAsyncioTestCase):
    """``_monitor_loop`` iteration, error swallowing, and cancellation."""

    async def test_loop_keeps_checking_until_interrupted(self):
        check_mock = AsyncMock(return_value=None)
        sleep_mock = AsyncMock(side_effect=_canned(None, _LoopBreak))

        with (
            patch.object(stop_loss_monitor, "_check_stop_losses", new=check_mock),
            patch.object(stop_loss_monitor.asyncio, "sleep", new=sleep_mock),
            self.assertRaises(_LoopBreak),
        ):
            await stop_loss_monitor._monitor_loop()

        self.assertEqual(check_mock.await_count, 2)
        sleep_mock.assert_has_awaits([call(stop_loss_monitor.STOP_LOSS_CHECK_INTERVAL)] * 2)

    async def test_loop_swallows_check_errors_and_continues(self):
        check_mock = AsyncMock(side_effect=_canned(RuntimeError("db exploded"), None))
        sleep_mock = AsyncMock(side_effect=_canned(None, _LoopBreak))
        logger_mock = MagicMock()

        with (
            patch.object(stop_loss_monitor, "_check_stop_losses", new=check_mock),
            patch.object(stop_loss_monitor.asyncio, "sleep", new=sleep_mock),
            patch.object(stop_loss_monitor, "logger", new=logger_mock),
            self.assertRaises(_LoopBreak),
        ):
            await stop_loss_monitor._monitor_loop()

        self.assertEqual(check_mock.await_count, 2)
        logged_errors = [c.args[0] for c in logger_mock.error.call_args_list]
        self.assertIn("Stop-loss check error: %s", logged_errors)

    async def test_loop_reraises_cancellation_without_sleeping(self):
        check_mock = AsyncMock(side_effect=asyncio.CancelledError)
        sleep_mock = AsyncMock()

        with (
            patch.object(stop_loss_monitor, "_check_stop_losses", new=check_mock),
            patch.object(stop_loss_monitor.asyncio, "sleep", new=sleep_mock),
            self.assertRaises(asyncio.CancelledError),
        ):
            await stop_loss_monitor._monitor_loop()

        sleep_mock.assert_not_awaited()


class CheckStopLossesTests(unittest.IsolatedAsyncioTestCase):
    """``_check_stop_losses`` query fan-out and per-user dispatch."""

    async def test_no_active_orders_returns_early(self):
        db = _session_with([], [])
        check_user_mock = AsyncMock(return_value=0)

        with (
            patch.object(stop_loss_monitor, "SessionLocal", return_value=db),
            patch.object(stop_loss_monitor, "_check_user_price_orders", new=check_user_mock),
        ):
            result = await stop_loss_monitor._check_stop_losses()

        self.assertIsNone(result)
        check_user_mock.assert_not_awaited()
        db.close.assert_called_once_with()

    async def test_orders_without_wallet_address_are_skipped(self):
        # ``user`` relationship not eager-loaded, or wallet still empty.
        db = _session_with(
            [_sl_order(1, 1, "tok-1", 0.4, wallet=None), _sl_order(2, 2, "tok-2", 0.5, wallet="")],
            [],
        )
        check_user_mock = AsyncMock(return_value=0)

        with (
            patch.object(stop_loss_monitor, "SessionLocal", return_value=db),
            patch.object(stop_loss_monitor, "_check_user_price_orders", new=check_user_mock),
        ):
            await stop_loss_monitor._check_stop_losses()

        check_user_mock.assert_not_awaited()
        db.close.assert_called_once_with()

    async def test_dispatches_each_user_with_its_own_wallet_and_orders(self):
        sl_orders = [
            _sl_order(1, 1, "tok-1", 0.4, wallet="0xaaa"),
            _sl_order(2, 1, "tok-2", 0.6, wallet="0xaaa"),
            _sl_order(3, 2, "tok-3", 0.7, wallet="0xbbb"),
        ]
        tp_orders = [
            _tp_order(4, 1, "tok-1", 0.9, wallet="0xaaa"),
            _tp_order(5, 2, "tok-4", 0.8, wallet="0xbbb"),
        ]
        db = _session_with(sl_orders, tp_orders)
        check_user_mock = AsyncMock(side_effect=_canned(0, 2))

        with (
            patch.object(stop_loss_monitor, "SessionLocal", return_value=db),
            patch.object(stop_loss_monitor, "_check_user_price_orders", new=check_user_mock),
        ):
            await stop_loss_monitor._check_stop_losses()

        self.assertEqual(check_user_mock.await_count, 2)
        by_user = {c.kwargs["user_id"]: c.kwargs for c in check_user_mock.await_args_list}
        self.assertEqual(set(by_user), {1, 2})
        self.assertEqual(by_user[1]["wallet_address"], "0xaaa")
        self.assertEqual(by_user[2]["wallet_address"], "0xbbb")
        self.assertEqual([o.id for o in by_user[1]["sl_orders"]], [1, 2])
        self.assertEqual([o.id for o in by_user[1]["tp_orders"]], [4])
        self.assertEqual([o.id for o in by_user[2]["sl_orders"]], [3])
        self.assertEqual([o.id for o in by_user[2]["tp_orders"]], [5])
        self.assertIs(by_user[1]["db"], db)
        db.close.assert_called_once_with()

    async def test_session_is_closed_even_when_dispatch_raises(self):
        db = _session_with([_sl_order(1, 1, "tok-1", 0.4, wallet="0xaaa")], [])
        check_user_mock = AsyncMock(side_effect=RuntimeError("boom"))

        with (
            patch.object(stop_loss_monitor, "SessionLocal", return_value=db),
            patch.object(stop_loss_monitor, "_check_user_price_orders", new=check_user_mock),
            self.assertRaises(RuntimeError) as ctx,
        ):
            await stop_loss_monitor._check_stop_losses()

        self.assertEqual(str(ctx.exception), "boom")
        db.close.assert_called_once_with()


class CheckUserPriceOrdersTests(unittest.IsolatedAsyncioTestCase):
    """``_check_user_price_orders`` credential lookup and trigger rules."""

    def setUp(self):
        self.db = MagicMock()
        # _check_user_price_orders looks up the owning user's
        # settings first; default to "no settings row" so the
        # tests exercise the live (credential-based) path.
        self.db.query.return_value.filter.return_value.first.return_value = None

    async def test_missing_credentials_for_wallet_returns_zero(self):
        with (
            patch.object(stop_loss_monitor, "SessionLocal", return_value=self.db),
            patch.object(
                stop_loss_monitor, "load_wallet_credentials", return_value=None
            ) as load_mock,
            patch.object(stop_loss_monitor, "_fetch_live_prices", new=AsyncMock()) as fetch_mock,
        ):
            result = await stop_loss_monitor._check_user_price_orders(
                db=self.db,
                user_id=1,
                wallet_address="0xaaa",
                sl_orders=[_sl_order(1, 1, "tok-1", 0.4)],
                tp_orders=[],
            )

        self.assertEqual(result, 0)
        load_mock.assert_called_once_with("0xaaa")
        fetch_mock.assert_not_awaited()

    async def test_credential_store_error_returns_zero(self):
        with (
            patch.object(
                stop_loss_monitor,
                "load_wallet_credentials",
                side_effect=CredentialStoreError("keystore locked"),
            ),
            patch.object(stop_loss_monitor, "_fetch_live_prices", new=AsyncMock()) as fetch_mock,
        ):
            result = await stop_loss_monitor._check_user_price_orders(
                db=self.db,
                user_id=7,
                wallet_address="0xbbb",
                sl_orders=[_sl_order(1, 7, "tok-1", 0.4)],
                tp_orders=[],
            )

        self.assertEqual(result, 0)
        fetch_mock.assert_not_awaited()

    async def test_no_token_ids_returns_empty_price_map(self):
        """No orders means no price lookup is issued at all."""
        with (
            patch.object(stop_loss_monitor, "load_wallet_credentials", return_value=None),
            patch.object(stop_loss_monitor, "_fetch_live_prices", new=AsyncMock()) as fetch_mock,
        ):
            result = await stop_loss_monitor._check_user_price_orders(
                db=self.db,
                user_id=1,
                wallet_address="0xaaa",
                sl_orders=[],
                tp_orders=[],
            )

        self.assertEqual(result, 0)
        fetch_mock.assert_not_awaited()

    async def test_stop_loss_triggers_exactly_at_boundary_price(self):
        order = _sl_order(1, 1, "tok-1", 0.40, size=3.0)
        execute_mock = AsyncMock()

        with (
            patch.object(
                stop_loss_monitor,
                "load_wallet_credentials",
                return_value={"private_key": "0xpk", "clob_creds": {"api_key": "k"}},
            ),
            patch.object(
                stop_loss_monitor,
                "_fetch_live_prices",
                new=AsyncMock(return_value={"tok-1": 0.40}),
            ) as fetch_mock,
            patch.object(stop_loss_monitor, "_execute_stop_loss", new=execute_mock),
        ):
            result = await stop_loss_monitor._check_user_price_orders(
                db=self.db,
                user_id=1,
                wallet_address="0xaaa",
                sl_orders=[order],
                tp_orders=[],
            )

        self.assertEqual(result, 1)
        execute_mock.assert_awaited_once_with(self.db, order, "0xpk", {"api_key": "k"}, 0.40, False)
        fetch_mock.assert_awaited_once_with("0xpk", {"api_key": "k"}, ["tok-1"])

    async def test_stop_loss_not_triggered_just_above_boundary(self):
        execute_mock = AsyncMock()

        with (
            patch.object(
                stop_loss_monitor,
                "load_wallet_credentials",
                return_value={"private_key": "0xpk", "clob_creds": None},
            ),
            patch.object(
                stop_loss_monitor,
                "_fetch_live_prices",
                new=AsyncMock(return_value={"tok-1": 0.401}),
            ),
            patch.object(stop_loss_monitor, "_execute_stop_loss", new=execute_mock),
        ):
            result = await stop_loss_monitor._check_user_price_orders(
                db=self.db,
                user_id=1,
                wallet_address="0xaaa",
                sl_orders=[_sl_order(1, 1, "tok-1", 0.40)],
                tp_orders=[],
            )

        self.assertEqual(result, 0)
        execute_mock.assert_not_awaited()

    async def test_orders_without_a_live_price_are_skipped(self):
        sl_execute = AsyncMock()
        tp_execute = AsyncMock()

        with (
            patch.object(
                stop_loss_monitor,
                "load_wallet_credentials",
                return_value={"private_key": "0xpk"},
            ),
            patch.object(
                stop_loss_monitor,
                "_fetch_live_prices",
                new=AsyncMock(return_value={}),
            ),
            patch.object(stop_loss_monitor, "_execute_stop_loss", new=sl_execute),
            patch.object(stop_loss_monitor, "_execute_take_profit", new=tp_execute),
        ):
            result = await stop_loss_monitor._check_user_price_orders(
                db=self.db,
                user_id=1,
                wallet_address="0xaaa",
                sl_orders=[_sl_order(1, 1, "tok-1", 0.9)],
                tp_orders=[_tp_order(2, 1, "tok-2", 0.1)],
            )

        self.assertEqual(result, 0)
        sl_execute.assert_not_awaited()
        tp_execute.assert_not_awaited()

    async def test_take_profit_triggers_exactly_at_boundary_price(self):
        order = _tp_order(9, 4, "tok-9", 0.75, size=1.5)
        execute_mock = AsyncMock()

        with (
            patch.object(
                stop_loss_monitor,
                "load_wallet_credentials",
                return_value={"private_key": "0xpk", "clob_creds": {"api_key": "k"}},
            ),
            patch.object(
                stop_loss_monitor,
                "_fetch_live_prices",
                new=AsyncMock(return_value={"tok-9": 0.75}),
            ),
            patch.object(stop_loss_monitor, "_execute_take_profit", new=execute_mock),
        ):
            result = await stop_loss_monitor._check_user_price_orders(
                db=self.db,
                user_id=4,
                wallet_address="0xaaa",
                sl_orders=[],
                tp_orders=[order],
            )

        self.assertEqual(result, 1)
        execute_mock.assert_awaited_once_with(self.db, order, "0xpk", {"api_key": "k"}, 0.75, False)

    async def test_take_profit_not_triggered_just_below_boundary(self):
        execute_mock = AsyncMock()

        with (
            patch.object(
                stop_loss_monitor,
                "load_wallet_credentials",
                return_value={"private_key": "0xpk", "clob_creds": None},
            ),
            patch.object(
                stop_loss_monitor,
                "_fetch_live_prices",
                new=AsyncMock(return_value={"tok-9": 0.7499}),
            ),
            patch.object(stop_loss_monitor, "_execute_take_profit", new=execute_mock),
        ):
            result = await stop_loss_monitor._check_user_price_orders(
                db=self.db,
                user_id=4,
                wallet_address="0xaaa",
                sl_orders=[],
                tp_orders=[_tp_order(9, 4, "tok-9", 0.75)],
            )

        self.assertEqual(result, 0)
        execute_mock.assert_not_awaited()

    async def test_stop_loss_and_take_profit_both_fire_and_count_separately(self):
        sl_execute = AsyncMock()
        tp_execute = AsyncMock()

        with (
            patch.object(
                stop_loss_monitor,
                "load_wallet_credentials",
                return_value={"private_key": "0xpk", "clob_creds": None},
            ),
            patch.object(
                stop_loss_monitor,
                "_fetch_live_prices",
                new=AsyncMock(return_value={"tok-1": 0.10, "tok-2": 0.95}),
            ),
            patch.object(stop_loss_monitor, "_execute_stop_loss", new=sl_execute),
            patch.object(stop_loss_monitor, "_execute_take_profit", new=tp_execute),
        ):
            result = await stop_loss_monitor._check_user_price_orders(
                db=self.db,
                user_id=1,
                wallet_address="0xaaa",
                sl_orders=[
                    _sl_order(1, 1, "tok-1", 0.20),
                    _sl_order(2, 1, "tok-3", 0.05),
                ],
                tp_orders=[
                    _tp_order(3, 1, "tok-2", 0.90),
                    _tp_order(4, 1, "tok-4", 0.99),
                ],
            )

        self.assertEqual(result, 2)
        self.assertEqual(sl_execute.await_count, 1)
        self.assertEqual(sl_execute.await_args.args[1].id, 1)
        self.assertEqual(sl_execute.await_args.args[4], 0.10)
        self.assertEqual(tp_execute.await_count, 1)
        self.assertEqual(tp_execute.await_args.args[1].id, 3)
        self.assertEqual(tp_execute.await_args.args[4], 0.95)

    async def test_token_ids_are_deduplicated_across_order_types(self):
        fetch_mock = AsyncMock(return_value={"tok-1": 0.5})
        sl_execute = AsyncMock()
        tp_execute = AsyncMock()

        with (
            patch.object(
                stop_loss_monitor,
                "load_wallet_credentials",
                return_value={"private_key": "0xpk", "clob_creds": None},
            ),
            patch.object(stop_loss_monitor, "_fetch_live_prices", new=fetch_mock),
            patch.object(stop_loss_monitor, "_execute_stop_loss", new=sl_execute),
            patch.object(stop_loss_monitor, "_execute_take_profit", new=tp_execute),
        ):
            result = await stop_loss_monitor._check_user_price_orders(
                db=self.db,
                user_id=1,
                wallet_address="0xaaa",
                sl_orders=[
                    _sl_order(1, 1, "tok-1", 0.40),
                    _sl_order(2, 1, "tok-1", 0.45),
                ],
                tp_orders=[
                    _tp_order(3, 1, "tok-1", 0.99),
                    _tp_order(4, 1, "tok-2", 0.95),
                ],
            )

        # Nothing breaches: tok-1 trades at 0.5 (above both stop targets and
        # below the take-profit target) and tok-2 has no price at all.
        self.assertEqual(result, 0)
        sl_execute.assert_not_awaited()
        tp_execute.assert_not_awaited()
        token_ids = fetch_mock.await_args.args[2]
        self.assertEqual(sorted(token_ids), ["tok-1", "tok-2"])
        self.assertEqual(len(token_ids), 2)


class FetchLivePricesTests(unittest.IsolatedAsyncioTestCase):
    """``_fetch_live_prices`` signature-type ladder and response parsing."""

    def _patches(self, clob, service=None, proxy="0xproxy"):
        """Patches for Signer, the Polymarket service, and the proxy lookup.

        The ``Signer`` double reports a deterministic EOA so the address fed to
        the proxy resolver can be asserted exactly.
        """
        signer_cls = MagicMock()
        signer_cls.return_value.address.return_value = _EOA_ADDRESS
        return (
            patch("py_clob_client.signer.Signer", new=signer_cls),
            patch.object(
                stop_loss_monitor,
                "get_polymarket_service",
                return_value=service if service is not None else _service_double(clob),
            ),
            patch.object(
                stop_loss_monitor,
                "_get_poly_proxy_wallet_address",
                return_value=proxy,
            ),
        )

    async def test_empty_token_ids_short_circuits_without_service_call(self):
        service = _service_double()
        with patch.object(stop_loss_monitor, "get_polymarket_service", return_value=service):
            prices = await stop_loss_monitor._fetch_live_prices("0xpk", None, [])

        self.assertEqual(prices, {})
        service._get_clob_client.assert_not_called()

    async def test_happy_path_uses_poly_proxy_signature_type_first(self):
        clob = _FakeClobClient(
            prices=[
                {"token_id": "t1", "price": "0.42"},
                {"asset_id": "t2", "price": "0.07"},
            ]
        )
        service = _service_double(clob)
        signer_patch, service_patch, proxy_patch = self._patches(clob, service)

        with signer_patch as signer_cls, service_patch as get_service, proxy_patch as proxy_mock:
            prices = await stop_loss_monitor._fetch_live_prices(
                "0xpk", {"api_key": "k"}, ["t1", "t2"]
            )

        self.assertEqual(prices, {"t1": 0.42, "t2": 0.07})
        get_service.assert_called_once_with()
        # The signer is built with the private key on Polygon (chain 137) and the
        # proxy wallet is resolved from the derived EOA address.
        signer_cls.assert_called_once_with("0xpk", 137)
        proxy_mock.assert_called_once_with(_EOA_ADDRESS)
        # First (and only) auth attempt uses signature_type=1 with the funder set.
        service._get_clob_client.assert_called_once_with(
            "0xpk", {"api_key": "k"}, signature_type=1, funder="0xproxy"
        )
        # One probe call for the first token, then one batched call for all.
        self.assertEqual(clob.price_calls, [["t1"], ["t1", "t2"]])
        self.assertEqual(clob.midpoint_calls, [])

    async def test_retries_next_signature_type_when_auth_probe_fails(self):
        clob = _FakeClobClient(prices=[{"token_id": "t1", "price": "0.5"}])
        service = _service_double(
            clob_factory=_ClobFactory(
                clob, failures=[RuntimeError("l2 auth failed"), RuntimeError("still bad")]
            )
        )
        signer_patch, service_patch, proxy_patch = self._patches(clob, service)

        with signer_patch, service_patch, proxy_patch:
            prices = await stop_loss_monitor._fetch_live_prices("0xpk", None, ["t1"])

        self.assertEqual(prices, {"t1": 0.5})
        self.assertEqual(service._get_clob_client.call_count, 3)
        sig_types = [c.kwargs["signature_type"] for c in service._get_clob_client.call_args_list]
        self.assertEqual(sig_types, [1, 0, 2])
        # Only POLY_PROXY carries a funder; EOA / gnosis pass funder=None.
        funders = [c.kwargs["funder"] for c in service._get_clob_client.call_args_list]
        self.assertEqual(funders, ["0xproxy", None, None])

    async def test_returns_empty_map_when_all_signature_types_fail(self):
        service = _service_double(
            clob_factory=_ClobFactory(None, failures=[RuntimeError("no creds")])
        )
        signer_patch, service_patch, proxy_patch = self._patches(None, service)
        logger_mock = MagicMock()

        with (
            signer_patch,
            service_patch,
            proxy_patch,
            patch.object(stop_loss_monitor, "logger", new=logger_mock),
        ):
            prices = await stop_loss_monitor._fetch_live_prices("0xpk", None, ["t1"])

        self.assertEqual(prices, {})
        self.assertEqual(service._get_clob_client.call_count, 3)
        self.assertIn(
            "Could not create authenticated CLOB client for price fetching",
            [c.args[0] for c in logger_mock.warning.call_args_list],
        )

    async def test_proxy_funder_is_none_when_proxy_unresolved(self):
        clob = _FakeClobClient(prices=[{"token_id": "t1", "price": "0.5"}])
        service = _service_double(clob)
        signer_patch, service_patch, proxy_patch = self._patches(clob, service, proxy=None)

        with signer_patch, service_patch, proxy_patch:
            prices = await stop_loss_monitor._fetch_live_prices("0xpk", None, ["t1"])

        self.assertEqual(prices, {"t1": 0.5})
        service._get_clob_client.assert_called_once_with(
            "0xpk", None, signature_type=1, funder=None
        )

    async def test_missing_prices_are_backfilled_from_midpoints(self):
        clob = _FakeClobClient(
            prices=[
                {"token_id": "t1", "price": "0.42"},
                {"token_id": "t2", "price": "not-a-number"},
                {"token_id": "t3", "price": "0"},
            ],
            midpoints=[{"asset_id": "t2", "mid": "0.61"}, {"token_id": "t3", "mid": "0.33"}],
        )
        service = _service_double(clob)
        signer_patch, service_patch, proxy_patch = self._patches(clob, service)

        with signer_patch, service_patch, proxy_patch:
            prices = await stop_loss_monitor._fetch_live_prices("0xpk", None, ["t1", "t2", "t3"])

        self.assertEqual(prices, {"t1": 0.42, "t2": 0.61, "t3": 0.33})
        # Unparseable and zero prices are both treated as missing.
        self.assertEqual(clob.midpoint_calls, [["t2", "t3"]])

    async def test_entries_without_token_key_or_positive_price_are_dropped(self):
        clob = _FakeClobClient(
            prices=[
                {"price": "0.9"},
                {"token_id": "", "price": "0.9"},
                {"token_id": "t2", "price": "-0.1"},
            ],
            midpoints=[],
        )
        service = _service_double(clob)
        signer_patch, service_patch, proxy_patch = self._patches(clob, service)

        with signer_patch, service_patch, proxy_patch:
            prices = await stop_loss_monitor._fetch_live_prices("0xpk", None, ["t1", "t2"])

        self.assertEqual(prices, {})
        self.assertEqual(clob.midpoint_calls, [["t1", "t2"]])

    async def test_signer_failure_is_swallowed_and_returns_empty_map(self):
        service = _service_double()
        logger_mock = MagicMock()

        with (
            patch("py_clob_client.signer.Signer", side_effect=ValueError("bad private key")),
            patch.object(stop_loss_monitor, "get_polymarket_service", return_value=service),
            patch.object(stop_loss_monitor, "logger", new=logger_mock),
        ):
            prices = await stop_loss_monitor._fetch_live_prices("garbage", None, ["t1"])

        self.assertEqual(prices, {})
        self.assertIn(
            "Failed to fetch live prices for stop-loss: %s",
            [c.args[0] for c in logger_mock.warning.call_args_list],
        )


class ExecuteStopLossTests(unittest.IsolatedAsyncioTestCase):
    """``_execute_stop_loss`` order placement and record updates."""

    def setUp(self):
        self.db = MagicMock()

    async def test_successful_sell_marks_order_triggered_and_commits(self):
        order = _sl_order(1, 1, "tok-1", 0.4, size=5.0)
        place_mock = MagicMock(return_value={"success": True, "order_hash": "0xhash"})

        with patch.object(stop_loss_monitor, "_place_order_on_polymarket", new=place_mock):
            await stop_loss_monitor._execute_stop_loss(
                self.db, order, "0xpk", {"api_key": "k"}, 0.35
            )

        place_mock.assert_called_once_with(
            private_key="0xpk",
            clob_creds={"api_key": "k"},
            token_id="tok-1",
            side="SELL",
            price=0.35,
            size=5.0,
        )
        self.assertEqual(order.status, "triggered")
        self.assertEqual(order.order_hash, "0xhash")
        self.assertEqual(order.executed_price, 0.35)
        self.assertIsNotNone(order.triggered_at)
        self.assertIsNotNone(order.updated_at)
        self.assertIs(order.updated_at, order.triggered_at)
        self.db.commit.assert_called_once_with()
        self.db.rollback.assert_not_called()

    async def test_failed_order_marks_order_failed_without_fill_details(self):
        order = _sl_order(1, 1, "tok-1", 0.4, size=5.0)
        place_mock = MagicMock(return_value={"success": False, "error": "insufficient balance"})

        with patch.object(stop_loss_monitor, "_place_order_on_polymarket", new=place_mock):
            await stop_loss_monitor._execute_stop_loss(self.db, order, "0xpk", None, 0.35)

        self.assertEqual(order.status, "failed")
        self.assertIsNone(order.order_hash)
        self.assertIsNone(order.executed_price)
        self.assertIsNotNone(order.triggered_at)
        self.assertIsNotNone(order.updated_at)
        self.db.commit.assert_called_once_with()

    async def test_raised_exception_marks_order_failed_and_commits(self):
        order = _sl_order(2, 1, "tok-2", 0.4, size=1.0)
        place_mock = MagicMock(side_effect=RuntimeError("network unreachable"))

        with patch.object(stop_loss_monitor, "_place_order_on_polymarket", new=place_mock):
            await stop_loss_monitor._execute_stop_loss(self.db, order, "0xpk", None, 0.30)

        self.assertEqual(order.status, "failed")
        self.assertIsNotNone(order.triggered_at)
        self.assertIsNotNone(order.updated_at)
        self.db.commit.assert_called_once_with()
        self.db.rollback.assert_not_called()

    async def test_commit_failure_triggers_rollback(self):
        order = _sl_order(3, 1, "tok-3", 0.4, size=1.0)
        place_mock = MagicMock(side_effect=RuntimeError("network unreachable"))
        # Every commit attempt explodes, so the inner recovery fails too.
        self.db.commit.side_effect = RuntimeError("session is gone")

        with patch.object(stop_loss_monitor, "_place_order_on_polymarket", new=place_mock):
            await stop_loss_monitor._execute_stop_loss(self.db, order, "0xpk", None, 0.30)

        # Placement raised, so the only commit is the recovery commit; it
        # fails too and the session is rolled back.
        self.assertEqual(order.status, "failed")
        self.db.commit.assert_called_once_with()
        self.db.rollback.assert_called_once_with()

    async def test_execution_survives_a_non_exception_order_result(self):
        """A ``None`` result is falsy, so the order is recorded as failed, not triggered."""
        order = _sl_order(4, 1, "tok-4", 0.4, size=1.0)
        place_mock = MagicMock(return_value=None)

        with patch.object(stop_loss_monitor, "_place_order_on_polymarket", new=place_mock):
            await stop_loss_monitor._execute_stop_loss(self.db, order, "0xpk", None, 0.30)

        self.assertEqual(order.status, "failed")
        self.assertIsNone(order.order_hash)
        self.assertIsNone(order.executed_price)
        self.assertIsNotNone(order.triggered_at)
        self.db.commit.assert_called_once_with()


class ExecuteTakeProfitTests(unittest.IsolatedAsyncioTestCase):
    """``_execute_take_profit`` order placement and record updates."""

    def setUp(self):
        self.db = MagicMock()

    async def test_successful_sell_marks_order_triggered_and_commits(self):
        order = _tp_order(7, 2, "tok-7", 0.8, size=4.0)
        place_mock = MagicMock(return_value={"success": True, "order_hash": "0xtphash"})

        with patch.object(stop_loss_monitor, "_place_order_on_polymarket", new=place_mock):
            await stop_loss_monitor._execute_take_profit(
                self.db, order, "0xpk", {"api_key": "k"}, 0.85
            )

        place_mock.assert_called_once_with(
            private_key="0xpk",
            clob_creds={"api_key": "k"},
            token_id="tok-7",
            side="SELL",
            price=0.85,
            size=4.0,
        )
        self.assertEqual(order.status, "triggered")
        self.assertEqual(order.order_hash, "0xtphash")
        self.assertEqual(order.executed_price, 0.85)
        self.assertIsNotNone(order.triggered_at)
        self.assertIsNotNone(order.updated_at)
        self.assertIs(order.updated_at, order.triggered_at)
        self.db.commit.assert_called_once_with()

    async def test_failed_order_marks_order_failed(self):
        order = _tp_order(7, 2, "tok-7", 0.8, size=4.0)
        place_mock = MagicMock(return_value={"success": False, "error": "no such market"})

        with patch.object(stop_loss_monitor, "_place_order_on_polymarket", new=place_mock):
            await stop_loss_monitor._execute_take_profit(self.db, order, "0xpk", None, 0.85)

        self.assertEqual(order.status, "failed")
        self.assertIsNone(order.executed_price)
        self.assertIsNotNone(order.triggered_at)
        self.assertEqual(order.order_hash, None)
        self.db.commit.assert_called_once_with()

    async def test_success_without_order_hash_records_none(self):
        order = _tp_order(7, 2, "tok-7", 0.8, size=4.0)
        place_mock = MagicMock(return_value={"success": True})

        with patch.object(stop_loss_monitor, "_place_order_on_polymarket", new=place_mock):
            await stop_loss_monitor._execute_take_profit(self.db, order, "0xpk", None, 0.85)

        self.assertEqual(order.status, "triggered")
        self.assertIsNone(order.order_hash)
        self.assertEqual(order.executed_price, 0.85)

    async def test_raised_exception_marks_order_failed_and_commits(self):
        order = _tp_order(7, 2, "tok-7", 0.8, size=4.0)
        place_mock = MagicMock(side_effect=ValueError("bad signer"))

        with patch.object(stop_loss_monitor, "_place_order_on_polymarket", new=place_mock):
            await stop_loss_monitor._execute_take_profit(self.db, order, "0xpk", None, 0.85)

        self.assertEqual(order.status, "failed")
        self.assertIsNotNone(order.triggered_at)
        self.db.commit.assert_called_once_with()

    async def test_commit_failure_triggers_rollback(self):
        order = _tp_order(7, 2, "tok-7", 0.8, size=4.0)
        place_mock = MagicMock(side_effect=ValueError("bad signer"))
        self.db.commit.side_effect = RuntimeError("session is gone")

        with patch.object(stop_loss_monitor, "_place_order_on_polymarket", new=place_mock):
            await stop_loss_monitor._execute_take_profit(self.db, order, "0xpk", None, 0.85)

        self.assertEqual(order.status, "failed")
        # Placement raised, so the only commit is the recovery commit; it fails
        # too and the session is rolled back.
        self.db.commit.assert_called_once_with()
        self.db.rollback.assert_called_once_with()


class ModuleConfigurationTests(unittest.TestCase):
    """Import-time configuration of the monitor."""

    def _reload_with_settings(self, **patch_kwargs):
        """Reload the module under a patched ``get_settings``, then restore it.

        The patch targets ``app.config`` because reload re-executes
        ``from app.config import get_settings``, which would rebind over a
        module-level patch. The restore reload is registered as a cleanup so it
        runs *after* the patch has been undone.
        """
        self.addCleanup(importlib.reload, stop_loss_monitor)
        with patch("app.config.get_settings", **patch_kwargs):
            return importlib.reload(stop_loss_monitor)

    def test_check_interval_is_a_positive_integer(self):
        self.assertIsInstance(stop_loss_monitor.STOP_LOSS_CHECK_INTERVAL, int)
        self.assertGreater(stop_loss_monitor.STOP_LOSS_CHECK_INTERVAL, 0)

    def test_check_interval_falls_back_to_ten_when_settings_unavailable(self):
        reloaded = self._reload_with_settings(side_effect=RuntimeError("no config"))

        self.assertEqual(reloaded.STOP_LOSS_CHECK_INTERVAL, 10)

    def test_check_interval_honours_configured_value(self):
        """A configured interval must win over the built-in fallback.

        This is what makes the fallback assertion above meaningful: 10 is also
        the shipped default, so without this test the fallback case cannot be
        distinguished from "the config happened to say 10".
        """
        settings = MagicMock()
        settings.stop_loss_check_interval_seconds = 37

        reloaded = self._reload_with_settings(return_value=settings)

        self.assertEqual(reloaded.STOP_LOSS_CHECK_INTERVAL, 37)

    def test_reload_restores_a_usable_interval(self):
        """After any patched reload the module must be usable again."""
        self.assertGreater(stop_loss_monitor.STOP_LOSS_CHECK_INTERVAL, 0)


if __name__ == "__main__":
    unittest.main()
