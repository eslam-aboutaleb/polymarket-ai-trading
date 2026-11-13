"""Opt-in official public price-history probe; requires no application or wallet secrets."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.market_data.provider_contract import (  # noqa: E402
    OFFICIAL_CLOB_API,
    POLYMARKET_CLOB_PRICES_CONTRACT,
    run_provider_contract_canary,
)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-id", required=True)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--enabled", action="store_true", help="Opt in to the public HTTP probe")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = await run_provider_contract_canary(
        args.token_id, enabled=args.enabled, lookback_hours=args.hours
    )
    payload = {
        "evidence_type": "live_public_provider_contract_canary"
        if args.enabled
        else "disabled_probe",
        "endpoint": f"{OFFICIAL_CLOB_API}/prices-history",
        "token_id": args.token_id,
        "contract": POLYMARKET_CLOB_PRICES_CONTRACT.model_dump(mode="json"),
        "report": report.model_dump(mode="json"),
        "raw_body_base64": report.raw_body_base64,
        "credentials_used": False,
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(  # noqa: T201 - CLI emits only public readiness counts, never request headers or secrets.
        f"{report.health_state}: {report.observed_bars}/{report.expected_bars} hourly samples; "
        f"{report.failure_reason or 'contract passed'}"
    )
    return 0 if report.canary_status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
