#!/usr/bin/env python3
"""Offline shadow comparison for the event-logic skill (#20): no network, orders or fills.

Uses the frozen forward-recorded replay dataset. Pairs are formed only inside one
settlement window; the joint outcome is scored only after both outcomes were received, and
the price inputs are the midpoints observed at the later of the two decision times.
"""

import importlib.util
import itertools
import json
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "services/shared"))
from app.services.intelligence import event_logic as el  # noqa: E402 - standalone CLI.
from app.services.intelligence.event_logic_skill import score_joint_forecasts  # noqa: E402

DATASET = ROOT / "user guide/acceptance-replay-2026-11-13/dataset.json.gz"


def report():
    spec = importlib.util.spec_from_file_location(
        "acceptance_replay", ROOT / "scripts/acceptance-replay-report.py"
    )
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    base = replay.analyze(DATASET)
    cohorts = defaultdict(list)
    for row in base["forecasts"]:
        cohorts[row["window_start"]].append(row)
    records, pairs = [], []
    for start, rows in sorted(cohorts.items()):
        for a, b in itertools.combinations(sorted(rows, key=lambda r: r["symbol"]), 2):
            decided = max(
                datetime.fromisoformat(a["decision_available_at"].replace("Z", "+00:00")),
                datetime.fromisoformat(b["decision_available_at"].replace("Z", "+00:00")),
            )
            received = min(
                datetime.fromisoformat(a["outcome_received_at"]),
                datetime.fromisoformat(b["outcome_received_at"]),
            )
            if decided >= received:  # look-ahead guard
                raise ValueError("price input not strictly before outcome")
            pa, pb = Decimal(a["p_market_midpoint"]), Decimal(b["p_market_midpoint"])
            lo, hi = el.frechet_and(pa, pb)
            independent = pa * pb
            records.append(
                {
                    "cluster": str(start),
                    "pa": str(pa),
                    "pb": str(pb),
                    "a": a["outcome_up"],
                    "b": b["outcome_up"],
                }
            )
            pairs.append(
                {
                    "window_start": start,
                    "legs": [a["symbol"], b["symbol"]],
                    "price_inputs": "observed market midpoints",
                    "frechet_lower": str(lo),
                    "frechet_upper": str(hi),
                    "independence_product": str(independent),
                    "independence_inside_frechet": lo <= independent <= hi,
                    "both_outcomes_up": bool(a["outcome_up"] and b["outcome_up"]),
                }
            )
    return {
        "schema": "event-logic-shadow-v1",
        "dataset_sha256": base["dataset_sha256"],
        "mode": "read_only_implemented_detector",
        "pairs": pairs,
        "scoring": score_joint_forecasts(records),
        "hypothesis_status": (
            "Diagnostic on one forward-recorded settlement window; not a precommitted "
            "backtest. Independence was never outside the Frechet range here, so the "
            "coherent candidate cannot differ from the baseline."
        ),
        "limitations": [
            "All pairs share one settlement window: one effective independent event.",
            "Same-window crypto up/down dependence is unknown; no joint was fabricated.",
            "Midpoints are observed prices, not forecasts; no fills, fees or P&L are modelled.",
            "No claim of alpha or production readiness.",
        ],
        "production_approved": False,
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(report(), indent=2, sort_keys=True) + "\n")
