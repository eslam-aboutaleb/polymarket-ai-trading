# Point-in-time market data (#1)

Use **Backtesting → Market data quality** to inspect capture availability before
using historical data. Enter the native token ID and optionally an ISO cutoff
with a timezone. The panel shows current source status/freshness and observations
received by the requested cutoff. Source clocks are displayed separately from
receipt clocks in API results; an old source timestamp never permits a late
receipt into an earlier decision.

Choose **Freeze these observations** to create a dataset ID. **Replay frozen
observations** loads that exact manifest, verifies its payload and receipt hashes,
and returns the same evidence even after newer captures arrive. The browser sends
explicit inspected snapshot IDs when freezing, so receipts committed between
inspection and freezing cannot silently enter the selected dataset. A dataset is a
bounded captured sample, not a claim of complete historical coverage. Empty,
stale, disconnected or interrupted feeds remain visibly insufficient.

## Operator setup

1. Apply `alembic upgrade head` from `backend/` with the normal configured
   environment (`PYTHONPATH=.:../services/shared` when running the CLI directly).
2. Enable `OBSERVATION_ARCHIVE_ENABLED=true`. It defaults off and introduces no
   order execution or credential access.
3. Set `OBSERVATION_ARCHIVE_TOKENS=11,22` using actual native token IDs. At most 50
   comma-separated numeric IDs are accepted. These are pinned subscriptions in
   the existing CLOB manager, so capture continues without an open UI. Run the
   existing worker/all process role for collection; web-only processes do not
   start background services. UI subscriptions may additionally request tokens.
4. Monitor the panel/API and archive-error logs. A disconnected source writes a
   status receipt when storage is available. Recovery from a write failure records
   an explicit gap. A database outage cannot record its own receipt; logs and
   aging last-received timestamps expose that failure until recovery.

Storage is additive: `venue_observations`, `observation_datasets` and
`observation_dataset_members`. Token/receipt, token/kind/receipt and
condition/receipt indexes support bounded reads. No financial history is rewritten.
The REST `ReplayCapture` and canonical `MarketObservation` stores remain intact:
those existing paths capture trader first-seen signals, paired venue metadata,
rule identities and public evidence. This archive adds raw market-channel events,
not a second market-data client or an alternate execution path.

## API and provenance

All endpoints require the existing authenticated browser session. Records contain
public source data only; private saved analyses/replay runs retain their existing
account authorization. No public ingestion API lets callers backdate receipts.

- `GET /api/market-data/observations?token_id=...&as_of=...&limit=50` returns
  receipt-ordered observations, with `next_before` for stable cursor pagination.
  Pass that ID as `before` with the same cutoff/token for the next page.
- `GET /api/market-data/observations/{snapshot_id}?as_of=...` retrieves one
  hash-verified snapshot; a snapshot unavailable at that cutoff returns 404.
- `GET /api/market-data/quality?token_id=...` reports current source state,
  receipt age, stale status, quality flags and bounded sample count.
- `POST /api/market-data/datasets?token_id=...&as_of=...&limit=50` freezes
  ordered snapshot references, payload/receipt hashes, normalization version,
  source contract and explicit rights-verification status. An optional JSON body
  `{"snapshot_ids":["<id>"]}` pins an exact inspected selection; token and cutoff
  checks still apply to every selected ID.
- `GET /api/market-data/datasets/{dataset_id}` verifies and replays the manifest.
  Missing or altered evidence fails with 409; it never falls back to current data.

Canonical event JSON and Decimal strings preserve source precision. JSON decimal
number lexemes are retained as strings rather than rounded through binary floats;
the checksum identifies that versioned canonical payload, not raw wire whitespace. Duplicate payloads retain
the first committed receipt. Changed payloads, conflicting sequences, late events,
connection changes, gaps, missing depth and unknown/future source clocks remain
separate evidence or explicit flags. Sequence comparisons are diagnostics, not a
claim about undocumented provider ordering. Crossed books are flagged. No missing
price or size is replaced by a fabricated zero.

Frames are capped at 256 KiB, batches at 1,000 events, and API pages at 100
observations. Oversized/invalid frames log a capture failure and mark a recovery
gap rather than silently claiming continuous coverage.

API results remain `INSUFFICIENT_DATA` for **continuous executable reconstruction**.
An intact captured book is usable source evidence, but it cannot establish queue
position, aggressor attribution or liquidity replenishment. Those are #18/#34
responsibilities. Rights metadata records an operator verification requirement;
public endpoint access does not itself grant redistribution or commercial rights.
#54 owns expanded provider contracts. No unlicensed substitute feed is introduced.

## Retention and rollback

Retention is explicit and bounded, and cannot remove observations pinned by a
frozen dataset. From `backend/`, first review a dry run:

```bash
PYTHONPATH=.:../services/shared .venv/bin/python scripts/prune_observations.py --before 2026-01-01T00:00:00+00:00 --limit 1000
# Repeat with --apply only when that retention cutoff is intended.
```

Unpinned observations older than the cutoff are eligible for deletion. Preserve
or freeze evidence required for research before applying retention. Foreign keys
protect dataset membership; indexed bounded batches avoid whole-table rewrites.
Partitioning is a future measured scaling choice, not a prerequisite for the
current bounded capture contract.

Disable the archive flag before rollback. Export evidence first: downgrading this
migration removes its three archive tables, while prior/live/financial tables
remain. If another pending PR adds an earlier migration, rebase/rechain this
migration and verify a single Alembic head before merging.

## Acceptance evidence and benchmark

| #1 acceptance | Verification |
| --- | --- |
| Book/sequence/Decimal round-trip | `test_decimal_roundtrip_first_receipt_and_duplicates`; browser checks preserve `0.400000000000000001`. |
| Out-of-order and duplicate events never overwrite | Gap/reconnect/conflicting-sequence regression, ten duplicate deliveries and concurrent PostgreSQL first-receipt test. |
| Missing depth returns insufficient data | Tick/missing/invalid/crossed-depth fixtures; API and browser show `INSUFFICIENT_DATA`. |
| As-of hides later observations | WebSocket → real SQL → HTTP regression; late-backfill cutoff regression; snapshot cutoff and cursor-boundary checks. Existing native trader-capture tests also preserve first-seen boundaries. |
| Source errors/freshness visible | Persisted disconnect/recovery status, archive-write-gap regression and two browser scenarios covering source failure/missing data/disabled archive. |
| Replay reproducible from snapshot IDs | Frozen manifest equality after newer receipts, direct snapshot retrieval, tamper rejection and pinned-retention regression; browser freeze/replay. |
| Migrations/ingestion concurrency | PostgreSQL simultaneous inserts/freezes, repeated bootstrap upgrade, downgrade preserving unrelated data, and re-upgrade. |

The deterministic data-availability benchmark compares the previous transient
price-tick path (no durable event replay) with the archive: ten identical deliveries
retain exactly one observation; one later backfill adds one observation to the
later window and zero to the earlier window; frozen replay remains identical after
that addition and retention. These are fixture correctness results, not measured
customer profitability, historical provider coverage or production throughput.

Tests live in `backend/tests/test_venue_observations.py`,
`backend/tests/integration/test_venue_observations_postgres.py` and
`frontend/e2e/observations/quality.spec.ts`. Run `npm run test:observations` from
`frontend/` for real HTTP/SQL/UI regressions. Controlled source receipts replace
external providers; no live money path is exercised. CI runs these alongside the
existing metadata, trader capture, authentication and trading-safety regressions.
