"""Bounded metadata-only legacy classification. Defaults to a read-only dry run."""

import argparse
import json

from app.services.analytics.paper_history import classify
from app.utils.database import SessionLocal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Label metadata; never invent journal entries"
    )
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    with SessionLocal() as db:
        result = classify(db, after=args.after, limit=args.limit, apply=args.apply)
        if args.apply:
            db.commit()
        print(json.dumps(result))  # noqa: T201 - CLI emits one machine-readable report


if __name__ == "__main__":
    main()
