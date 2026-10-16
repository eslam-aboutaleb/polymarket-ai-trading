"""The services proto and the backend proto must stay in sync.

Both trees compile against the same ``polymarket.analysis.AnalysisService``
contract: each service in ``services/`` generates its stubs from
``services/proto/analysis.proto`` while the backend generates its client
against ``backend/proto/analysis.proto``. Once those files drift, the two
sides disagree about the wire format (missing field, renamed method) and the
failure only shows up as a broken RPC at runtime, so this test pins them
together.

The check is intentionally plain-path and import-free: it compares the two
files as text so it runs from the repo root with ``python -m pytest
services/tests`` without any of the AI stacks installed.
"""

import difflib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICES_PROTO = REPO_ROOT / "services" / "proto" / "analysis.proto"
BACKEND_PROTO = REPO_ROOT / "backend" / "proto" / "analysis.proto"


def _read_proto(path: Path) -> str:
    """Read a proto source as text without newline translation."""
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def test_proto_sources_are_byte_identical() -> None:
    services_proto = _read_proto(SERVICES_PROTO)
    backend_proto = _read_proto(BACKEND_PROTO)

    if services_proto == backend_proto:
        return

    diff = "".join(
        difflib.unified_diff(
            services_proto.splitlines(keepends=True),
            backend_proto.splitlines(keepends=True),
            fromfile=str(SERVICES_PROTO.relative_to(REPO_ROOT)),
            tofile=str(BACKEND_PROTO.relative_to(REPO_ROOT)),
        )
    )
    pytest.fail(
        f"services/proto/analysis.proto and backend/proto/analysis.proto have drifted:\n{diff}"
    )
