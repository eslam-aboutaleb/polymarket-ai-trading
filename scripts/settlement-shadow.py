#!/usr/bin/env python3
"""Reproduce a settlement diagnostic from captured public quotes and labels.

Observed label-availability delay is not chain finality or actual lockup duration.
The positions, funding rates and gas scenarios are declared counterfactuals.
"""

import argparse
import importlib.util
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT  # noqa: E402
from app.services.analytics.settlement_lifecycle import (  # noqa: E402
    SettlementAnalysisRequest,
    analyze_settlement_lifecycle,
)


def report():
    spec = importlib.util.spec_from_file_location(
        "settlement_public_replay", ROOT / "scripts/acceptance-replay-report.py"
    )
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    dataset = ROOT / "user guide/acceptance-replay-2026-11-13/dataset.json.gz"
    base = replay.analyze(dataset)
    rows = []
    for row in base["forecasts"]:
        end = datetime.fromtimestamp(row["window_start"] + 300, UTC)
        observed = datetime.fromisoformat(row["outcome_received_at"].replace("Z", "+00:00"))
        delay = Decimal(str((observed - end).total_seconds()))
        cost = Decimal(10) * Decimal(row["vwap_ten_shares"]) + Decimal(row["fee_upper_bound"])
        result = analyze_settlement_lifecycle(
            SettlementAnalysisRequest(
                market_id=row["condition_id"],
                state="FINAL",
                position_shares=10,
                entry_cost_usdc=cost,
                locked_collateral_usdc=cost,
                now=observed,
            )
        )
        rows.append(
            {
                "condition_id": row["condition_id"],
                "end_at": end.isoformat(),
                "label_available_at": observed.isoformat(),
                "label_sha256": row["outcome_sha256"],
                "quote_sha256": row["book_sha256"],
                "observed_label_delay_seconds": str(delay),
                "counterfactual_entry_cost_usdc": str(cost),
                "end_date_only_cash_release_baseline_usdc": str(cost),
                "verified_receipt_required_cash_release_usdc": str(result.released_collateral_usdc),
                "expected_funding_cost_8pct_2days_usdc": str(result.expected_opportunity_cost_usdc),
                "stressed_funding_cost_8pct_14days_usdc": str(
                    result.stressed_opportunity_cost_usdc
                ),
                "hypothetical_full_claim_pnl_by_gas_usdc": {
                    gas: str(Decimal(int(row["outcome_up"])) * 10 - cost - Decimal(gas))
                    for gas in ("0", "0.1", "1", "5")
                },
                "actual_fill": False,
                "chain_receipt_available": False,
            }
        )
    return {
        "schema_version": "settlement-shadow-v1",
        "dataset_sha256": base["dataset_sha256"],
        "evidence_class": "implemented_detector_post_capture_diagnostic",
        "production_approved": False,
        "observations": rows,
        "sample_count": len(rows),
        "baseline": "Unsafe end-date-only capital-release assumption; not recorded bot decisions",
        "limitations": [
            (
                "No historical account fills, authenticated redemption receipts, realized returns "
                "or calibrated delay distribution are fabricated."
            ),
            (
                "Label availability is an observed receipt clock, not economic resolution time "
                "or canonical chain finality."
            ),
            (
                "Funding/gas sensitivities are declared scenarios; not measured fees, "
                "available cash or actual P&L."
            ),
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with localcontext(ACCOUNTING_CONTEXT):
        args.output.write_text(json.dumps(report(), indent=2, sort_keys=True) + "\n")
