"""Bounded operator retention; frozen datasets are pinned. Dry-run by default."""

import argparse
from datetime import datetime

from app.services.market_data.venue_observations import prune_unpinned
from app.utils.database import SessionLocal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, help="ISO timestamp with timezone")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    before = datetime.fromisoformat(args.before)
    if before.tzinfo is None or not 1 <= args.limit <= 1000:
        parser.error("timezone required; limit must be between 1 and 1000")
    with SessionLocal() as db:
        count = prune_unpinned(db, before, args.limit)
        if args.apply:
            db.commit()
        else:
            db.rollback()
        print({"eligible": count, "applied": args.apply, "limit": args.limit})  # noqa: T201


if __name__ == "__main__":
    main()
