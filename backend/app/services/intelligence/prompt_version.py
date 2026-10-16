"""Prompt-store versioning (plan 04).

``prompt_version`` answers "which prompt produced this verdict?" with a
*content hash* rather than a hand-maintained number, so a prompt edit
is automatically a new version and calibration impact is measurable
per version without anyone remembering to bump a counter.

The hash is over the *canonical* prompt store: keys sorted, so a JSON
reformat that changes nothing changes no version. It is cached because
it is computed on the hot path (every recorded verdict) and the store
changes only on deploy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Short hash length. 16 hex chars (64 bits) makes collisions
# irrelevant at any realistic number of prompt versions while keeping
# the column small enough to index.
PROMPT_VERSION_LENGTH = 16

_lock = threading.Lock()
_cached_version: str | None = None


def _candidate_paths() -> list[Path]:
    """Prompt-store lookup order, mirroring the backend's own loader.

    ``backend/prompts/prompts.json`` is the fallback the gRPC client
    uses (``app.grpc_clients.analysis_client``), and ``PROMPTS_PATH``
    is the containerised location; /app is the production path.
    """
    paths: list[Path] = []
    env_path = os.environ.get("PROMPTS_PATH")
    if env_path:
        paths.append(Path(env_path))
    here = Path(__file__).resolve().parents[3]
    paths.append(here / "prompts" / "prompts.json")
    paths.append(Path("/app/prompts/prompts.json"))
    return paths


def _load_store() -> dict:
    for path in _candidate_paths():
        try:
            with path.open(encoding="utf-8") as handle:
                store = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.debug("Prompt store not readable at %s: %s", path, exc)
            continue
        if isinstance(store, dict):
            return store
    # A missing store is a real misconfiguration, but the version must
    # still be *something* stable, so it hashes the empty store rather
    # than raising on a verdict that would otherwise be recorded.
    logger.warning("No prompt store found; versioning the empty store")
    return {}


def _compute_version(store: dict) -> str:
    """Content hash over the canonical form of the prompt store."""
    try:
        canonical = json.dumps(store, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        canonical = repr(sorted(store))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:PROMPT_VERSION_LENGTH]


def current_prompt_version() -> str:
    """Return the current prompt-store version (cached per process).

    ``reset_prompt_version()`` clears the cache; it is what a process
    calls after a deploy that hot-swaps the prompt store, and what the
    tests call to isolate a change.
    """
    global _cached_version
    with _lock:
        if _cached_version is None:
            _cached_version = _compute_version(_load_store())
        return _cached_version


def reset_prompt_version() -> None:
    """Drop the cached version (tests, and a prompt-store hot swap)."""
    global _cached_version
    with _lock:
        _cached_version = None


@lru_cache(maxsize=1)
def _cached_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
