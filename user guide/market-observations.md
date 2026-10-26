# Point-in-time public observations

This first milestone of #1 archives the existing public CLOB WebSocket stream.
It preserves the REST ReplayCapture and MarketObservation metadata stores.

Apply `alembic upgrade head` in the configured backend environment; enable
`OBSERVATION_ARCHIVE_ENABLED=true` (default off). Capture follows the existing
price-stream subscriptions. Unsubscribed tokens have no promised coverage.

Authenticated `GET /api/market-data/observations?token_id=...&as_of=...&limit=100`
returns a bounded page in descending receipt order. Cutoffs require timezones.
Late-arriving events cannot enter earlier queries merely because their source
timestamp is old. Responses retain raw JSON, content hashes, stable IDs, exact
Decimal normalization, source and receipt clocks, connection identity and quality
flags. Identical payloads keep their first committed receipt. Changed payloads,
including conflicting sequences, remain separate rows rather than overwrites.

Reconnects, numeric sequence gaps/conflicts, late events, unknown/future source
clocks and missing depth are flagged. Sequence comparison is diagnostic, not proof
of provider semantics. Receipt age exposes freshness. All responses explicitly
report insufficient evidence for executable reconstructed books. Snapshots and
ticks cannot establish queue position, aggressor semantics or replenishment.
#18 owns verified reconstruction; no observation becomes an assumed fill.

Messages over 1 MB, batches over 1,000 events and normalized sides over 1,000 levels
are bounded. Database persistence uses its own transaction off the event loop.
Failures are logged without inventing receipts or stopping existing price ticks.
There is no client ingestion endpoint to backdate receipts and no private account
data, signing or trading credential access in this public archive.

Rollback: disable the flag first. The migration downgrade drops only
`venue_observations`; export evidence before dropping it. No automatic retention
purge can silently invalidate exported replay fixtures. Limit subscriptions and
monitor storage and archive-error logs until measured retention is implemented.

Tests: `tests/test_venue_observations.py` covers exact values, deduplication,
source clocks, gaps/reconnects and WebSocket-envelope-to-authenticated-HTTP as-of
behavior. `tests/integration/test_venue_observations_postgres.py` covers concurrent
receipts and upgrade/downgrade preservation and is included in CI.

## Remaining #1 milestones

Keep #1 open after this PR. Continuous collection independent of UI subscriptions,
frozen multi-source dataset manifests, research-evidence rights metadata,
production retention/partitions and the quality UI remain separate increments.
Raw stream storage does not claim those capabilities or #18 book reconstruction.
