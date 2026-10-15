"""Run the background schedulers only (ROLE=worker, plan 10 Phase A).

The web tier no longer owns any monitor: ``ROLE=web`` skips them all in
``app.main.lifespan``. This script boots the same lifespan with the
scheduler role so a dedicated container can own exactly one copy of
each monitor, while the ``backend`` service scales horizontally.

Why this exists instead of ``uvicorn --workers N`` for everything:
the schedulers coordinate through Postgres advisory locks, so an extra
HTTP worker used to silently multiply every monitor — a single
stop-loss trigger placed two independent sell orders. Splitting the
role keeps the HTTP tier scalable without that hazard.

Usage (as the container CMD):
    uv run python -m app.worker_main
"""

from __future__ import annotations

import asyncio
import logging

from app.main import lifespan  # noqa: F401  (re-exported for introspection)

logger = logging.getLogger("app.worker_main")


async def _run() -> None:
    """Drive the lifespan outside of an ASGI server.

    ``app.main.lifespan`` is the single source of truth for which
    services start; reusing it here means a scheduler can never be
    started by the worker and forgotten by the web tier, or vice versa.
    """
    async with lifespan(None):
        logger.info("Background scheduler worker running (ROLE=worker)")
        # Park forever: every service is its own asyncio task, so this
        # coroutine only has to stay alive for them to keep running.
        await asyncio.Event().wait()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Background scheduler worker stopped")


if __name__ == "__main__":
    main()
