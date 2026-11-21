#!/usr/bin/env python3
"""Read-only public contract canary. No wallets, accounts or order APIs are accessed.

Run via uv --project backend; pass --output REPORT.json to retain the public evidence.
"""

import argparse
import json
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/shared"))
# This CLI runs no auth server; ephemeral defaults only satisfy shared settings.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET_KEY", secrets.token_hex(32))
os.environ.setdefault("REFRESH_TOKEN_HASH_SECRET", secrets.token_hex(32))

from app.services.analytics.chain_evidence import (  # noqa: E402
    COLLATERAL,
    CTF,
    EXCHANGES,
    ChainReader,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    reader = ChainReader(seconds=20)
    report = {
        "classification": "public_read_only_contract_canary_not_funded_performance",
        "checked_at": datetime.now(UTC).isoformat(),
        "chain_id": 137,
        "collateral": COLLATERAL,
        "token_contract": CTF,
        "exchanges": sorted(EXCHANGES),
        "abi_source": "Polymarket/ctf-exchange-v2@ccc0596074f4dfd62c944fbca4de252893b82b4b",
    }
    try:
        block = reader.finalized()
        reader.validate_assets(block)
        for exchange in sorted(EXCHANGES):
            reader.validate_exchange(exchange, block)
        reader.canonical(block)
        report.update(
            status="passed",
            finalized_block={key: block[key] for key in ("number", "hash", "timestamp")},
            collateral_decimals=6,
            evidence=reader.responses,
        )
    except Exception as exc:
        report.update(status="unavailable", error_type=type(exc).__name__)
    finally:
        reader.close()
    text = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    else:
        sys.stdout.write(text)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
