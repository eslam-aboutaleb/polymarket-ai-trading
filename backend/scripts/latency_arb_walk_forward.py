"""Walk-forward edge threshold re-pick from past paper signals.

Usage: python scripts/latency_arb_walk_forward.py signals.csv [--min-trades 100]

``signals.csv`` needs the columns ``day`` (YYYY-MM-DD), ``edge``, ``pnl`` and
``stake`` (one row per past paper or replay signal, fees included in pnl).
For each day it prints the threshold chosen from the days BEFORE it only, so
the choice never sees the day it is applied to. Re-run weekly and set
LATENCY_ARB_EDGE_THRESHOLD to the latest value. Not financial advice: a
threshold picked this way is only as good as the fill model behind ``pnl``.
"""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.trading.latency_arb_service import pick_edge_threshold  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path")
    parser.add_argument("--min-trades", type=int, default=100)
    args = parser.parse_args()
    by_day: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    with Path(args.csv_path).open(newline="") as handle:
        for row in csv.DictReader(handle):
            by_day[row["day"]].append((float(row["edge"]), float(row["pnl"]), float(row["stake"])))
    past: list[tuple[float, float, float]] = []
    for day in sorted(by_day):
        threshold = pick_edge_threshold(past, min_trades=args.min_trades)
        sys.stdout.write(f"{day} threshold={threshold:.2f}\n")
        past.extend(by_day[day])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
