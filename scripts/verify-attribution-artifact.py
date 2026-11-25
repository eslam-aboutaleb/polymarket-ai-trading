#!/usr/bin/env python3
"""Verify a downloaded attribution report/snapshot offline, without credentials.

Canonical form: JSON object excluding report_id/id, ASCII escapes, sorted keys,
compact separators. Monetary quantities and nonintegral configuration are strings.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    try:
        body = json.loads(args.artifact.read_text())
        if not isinstance(body, dict):
            raise ValueError("artifact must be an object")
        identity = body.pop("report_id", None) or body.pop("id", None)
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise ValueError("missing artifact identity")
        actual = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        if actual != identity:
            raise ValueError("content mismatch")
    except (OSError, TypeError, ValueError):
        print(json.dumps({"status": "invalid"}))  # noqa: T201 - CLI output
        return 1
    print(json.dumps({"status": "passed", "artifact_id": identity}))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
