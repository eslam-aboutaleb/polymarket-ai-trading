"""Console entrypoint for the Polymarket backend.

The web application itself lives in :mod:`app.main`, which exposes the ASGI
``app`` object consumed by Uvicorn:

    uvicorn app.main:app --reload

Running this module directly (``python main.py``) only emits a log line. It
exists as a stable target for process supervisors and container healthchecks
that want a cheap, import-light command that proves the package is installed
and importable, without starting the server, opening database connections or
binding a port.
"""

import logging

logger = logging.getLogger(__name__)


def main() -> None:
    """Confirm the backend package is importable.

    No-op beyond logging; see the module docstring for why the ASGI app is not
    started from here.
    """
    logger.info("Polymarket backend entrypoint invoked.")


if __name__ == "__main__":
    main()
