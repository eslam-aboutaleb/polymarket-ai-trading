#!/usr/bin/env python3
"""Offline diagnostic comparison on forward-recorded quotes; no real fills or trading."""

import argparse
import hashlib
import importlib.util
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "services/shared"))
from app.services.analytics.event_correlation_risk import (  # noqa: E402 - standalone repository CLI.
    EventCorrelationRequest,
    ExposureLeg,
    assess_event_correlation_risk,
)
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT  # noqa: E402


def report():
    spec = importlib.util.spec_from_file_location(
        "acceptance_replay", ROOT / "scripts/acceptance-replay-report.py"
    )
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    dataset = ROOT / "user guide/acceptance-replay-2026-11-13/dataset.json.gz"
    base = replay.analyze(dataset)
    _, raw = replay.load(dataset)
    # A basket includes only quotes for the SAME settlement window. Earlier
    # windows cannot be silently counted as simultaneous open positions.
    cohorts = defaultdict(list)
    for row in base["forecasts"]:
        cohorts[row["window_start"]].append(row)
    diagnostics = []
    for start, rows in sorted(cohorts.items()):
        if len(rows) < 2:
            continue
        legs = []
        for row in rows:
            legs.append(
                ExposureLeg(
                    condition_id=row["condition_id"],
                    token_id=json.loads(raw[row["book_sha256"]])["asset_id"],
                    outcome="YES",
                    shares=Decimal(10),
                    entry_price=Decimal(row["vwap_ten_shares"]),
                    fee_per_share=Decimal(row["fee_upper_bound"]) / 10,
                    event_id=row["condition_id"],
                    underlying_asset=row["symbol"],
                    oracle="Chainlink-TWAP-60s-declared-in-official-rules",
                    resolution_utc=datetime.fromtimestamp(start + 300, UTC),
                    leg_id=row["book_sha256"],
                )
            )
        as_of = max(
            datetime.fromisoformat(row["decision_available_at"].replace("Z", "+00:00"))
            for row in rows
        )
        request = EventCorrelationRequest(legs=legs, as_of=as_of)
        result = assess_event_correlation_risk(request)
        gross = result.total_gross_notional
        single_flags = [
            leg.condition_id
            for leg in legs
            if leg.shares * (leg.entry_price + leg.fee_per_share) / gross > Decimal(".4")
        ]
        terminal = sum(
            (
                Decimal(int(row["outcome_up"])) * 10
                - Decimal(row["vwap_ten_shares"]) * 10
                - Decimal(row["fee_upper_bound"])
                for row in rows
            ),
            Decimal(0),
        )
        diagnostics.append(
            {
                "window_start": start,
                "as_of": as_of.isoformat(),
                "quote_count": len(rows),
                "book_sha256": [row["book_sha256"] for row in rows],
                "single_condition_concentration_flags": single_flags,
                "group_concentration_flags": result.concentration_warnings,
                "worst_case_loss": str(result.worst_case_portfolio_loss),
                "oracle_outage_or_zero_payout_stress_bound": str(result.stress_loss_upper_bound),
                "counterfactual_terminal_pnl_after_fee_bound": str(terminal),
                "observed_counterfactual_loss_within_bound": -terminal
                <= result.worst_case_portfolio_loss,
                "actual_fills": False,
                "evidence_class": "post_capture_diagnostic_on_forward_recorded_matched_quotes",
            }
        )
    return {
        "schema": "event-risk-shadow-v1",
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "mode": "read_only_implemented_detector",
        "diagnostics": diagnostics,
        "hypothesis_status": (
            "Concentration comparison is diagnostic, not a precommitted trading-strategy backtest."
        ),
        "production_approved": False,
        "expected_shortfall": None,
        "limitations": [
            "No actual historical account holdings, wallet reconciliation or fills are fabricated.",
            (
                "Declared oracle grouping is operational source concentration, "
                "not measured price correlation."
            ),
            (
                "Future source outages/void/revision states use a conservative gross-loss bound, "
                "not invented probabilities."
            ),
            "Existing account caps, signing, order gateway and halt remain authoritative.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with localcontext(ACCOUNTING_CONTEXT):
        result = report()
    body = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(body)
    else:
        sys.stdout.write(body)


if __name__ == "__main__":
    main()
