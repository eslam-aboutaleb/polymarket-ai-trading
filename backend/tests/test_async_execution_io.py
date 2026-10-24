"""Live exchange latency and cancellation must not race session cleanup."""

import asyncio
import threading

import pytest

from app.services.trading.execution.async_io import run_blocking_execution


def test_exchange_work_keeps_event_loop_responsive():
    async def scenario():
        entered = threading.Event()
        release = threading.Event()
        loop_thread = threading.get_ident()

        def exchange_call():
            assert threading.get_ident() != loop_thread
            entered.set()
            assert release.wait(5)
            return "submitted"

        task = asyncio.create_task(run_blocking_execution(exchange_call))
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
                # An unrelated request can run while the exchange is waiting.
                assert not task.done()
                await asyncio.sleep(0)
        finally:
            release.set()
        assert await task == "submitted"

    asyncio.run(scenario())


def test_cancellation_waits_for_exchange_before_session_cleanup():
    async def scenario():
        entered = threading.Event()
        release = threading.Event()
        exited = threading.Event()
        cleanup = asyncio.Event()

        def exchange_call():
            entered.set()
            assert release.wait(5)
            assert not cleanup.is_set()
            exited.set()

        async def request():
            try:
                await run_blocking_execution(exchange_call)
            finally:
                assert exited.is_set()
                cleanup.set()

        task = asyncio.create_task(request())
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0.01)
                    assert not task.done()
                    assert not cleanup.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleanup.is_set()

    asyncio.run(scenario())


def test_exchange_errors_propagate_to_the_caller():
    def fail():
        raise RuntimeError("exchange unavailable")

    with pytest.raises(RuntimeError, match="exchange unavailable"):
        asyncio.run(run_blocking_execution(fail))
