"""Summarize one frozen paper benchmark log without fetching or trading."""

import argparse
import json
from pathlib import Path

from app.services.trading.btc_regime_scorecard import scorecard


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("--start", type=int, help="Inclusive first window epoch")
    parser.add_argument("--end", type=int, help="Exclusive last window epoch")
    args = parser.parse_args()
    with args.log.open() as stream:
        report = scorecard(map(json.loads, stream), start=args.start, end=args.end)
    print(json.dumps(report, indent=2, default=str))  # noqa: T201 - CLI report


if __name__ == "__main__":
    main()
