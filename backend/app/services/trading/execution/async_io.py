"""Keep synchronous exchange execution off application event loops."""

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


async def run_blocking_execution(operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Transfer exclusive use of caller-owned sessions until the operation ends.

    The caller must not use its session concurrently. Cancellation drains the
    worker before propagating, so request/monitor cleanup cannot close or reuse
    a session while an exchange submission still owns it.
    """
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # Consume worker errors; cancellation stays authoritative.
        raise
