"""Hourly portfolio-equity snapshots (plan 05).

Once an hour this service samples every user's portfolio equity and
writes one ``PortfolioSnapshot`` row per ``(user_id, bucket_hour)`` so the
Dashboard can chart the equity curve and Plan 02's daily digest has a
``start_value`` to compare against.

**Bankroll has exactly one definition.** ``usdc_balance`` and the
positions are fetched through ``kelly_service.compute_bankroll`` and the
returned number is stored verbatim; nothing here recomputes it. The row's
``unrealized_pnl`` is then derived algebraically (*bankroll − usdc*), so
the two can never disagree. ``total_invested``, ``total_current_value``
and ``positions_count`` come from the very position rows
``compute_bankroll`` already fetched — the wrapper below captures them, so
one cycle costs the same two upstream calls per user as a lone bankroll
lookup.

**Failure policy.** ``compute_bankroll`` swallows upstream errors by
design (order placement must never block), which would otherwise leave
this writer recording a silent zero equity point. The capture wrapper
re-raises nothing but records which fetch failed; a user with any
failure is logged and **skipped** — no zero row is ever written.

**Concurrency.** Users are independent, so the cycle samples them in
parallel behind a semaphore with a per-user timeout: one hung CLOB/RPC
call stalls only that user, never the whole pass. Skipping for any reason
(failure, timeout, duplicate hour) keeps the skip — never-zero — policy.

**Idempotency.** ``bucket_hour`` is the wall-clock hour truncated to the
hour. A row is written only when that bucket is still empty, so a re-run
or an overlapping worker (the advisory lock fails open on database
errors) refreshes the series instead of corrupting it.

**Paper split.** A user with ``simulation_mode`` on gets
``is_paper=True`` and ``bankroll = paper_balance``; the wallet-derived
columns stay zero because they describe the real wallet, not the paper
account. The two series are never mixed.
"""

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.portfolio_snapshot import PortfolioSnapshot
from app.models.user import User
from app.models.user_settings import UserSettings
from app.security.credential_store import CredentialStoreError, load_wallet_credentials
from app.services import kelly_service
from app.services.polymarket_service import get_polymarket_service
from app.utils.database import SessionLocal
from app.utils.scheduler_lock import (
    acquire_scheduler_lock,
    release_scheduler_lock,
    scheduler_heartbeat,
)
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

SCHEDULER_NAME = "equity_snapshots"

_snapshot_task: asyncio.Task | None = None

# Default cadence when the setting is missing or unusable (seconds).
DEFAULT_SNAPSHOT_INTERVAL = 3600

# Writer concurrency: one user's snapshot costs two upstream CLOB/RPC calls,
# and users are independent, so the cycle awaits them in parallel behind a
# semaphore. One hung wallet call must not stall every other user's point
# (pattern copied from ``leaderboard_service._enrich_entries`` /
# ``trade_monitor._poll_and_process``).
MAX_CONCURRENT_SNAPSHOTS = 8

# Per-user ceiling on the two upstream calls. A user that exceeds it is
# skipped exactly like a failed fetch — never recorded as a zero row.
SNAPSHOT_USER_TIMEOUT_SECONDS = 60.0

# Read-side defaults.
DEFAULT_SNAPSHOT_LIMIT = 500
MAX_SNAPSHOT_LIMIT = 2000
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_PERIOD = "7d"

# label -> window in hours. "all" has no lower bound (None).
PERIOD_HOURS: dict[str, int | None] = {
    "24h": 24,
    "7d": 24 * 7,
    "30d": 24 * 30,
    "all": None,
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _to_float(value: Any, default: float = 0.0) -> float:
    """Coerce an upstream API value to a float, ``default`` when unusable."""
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def truncate_to_hour(moment: datetime) -> datetime:
    """Drop minutes/seconds/microseconds — the snapshot bucket key."""
    return moment.replace(minute=0, second=0, microsecond=0)


def period_window(period: str | None) -> timedelta | None:
    """Look-back window for a summary period, ``None`` for "all".

    An unknown label falls back to the default window rather than
    raising: a bad query string must not 500 the Dashboard.
    """
    key = (period or DEFAULT_PERIOD).strip().lower()
    hours = PERIOD_HOURS.get(key, PERIOD_HOURS[DEFAULT_PERIOD])
    return None if hours is None else timedelta(hours=hours)


def position_totals(positions: list | None) -> dict[str, Any]:
    """Aggregate the fetched position rows into the snapshot's summary columns.

    Rows arrive in both the authenticated (``avgPrice``/``curPrice``) and
    the Data-API (``avg_price``/``cur_price``) spellings, so both are
    accepted. A missing live price falls back to the entry price, so a
    position carries its cost basis instead of inventing a gain or loss.
    Rows without a positive size are not positions and are ignored.
    """
    total_invested = 0.0
    total_current_value = 0.0
    positions_count = 0
    for raw in positions or []:
        if not isinstance(raw, dict):
            continue
        size = _to_float(raw.get("size"))
        if size <= 0:
            continue
        avg_price = _to_float(raw.get("avgPrice", raw.get("avg_price")))
        cur_price = _to_float(raw.get("curPrice", raw.get("cur_price", raw.get("price"))))
        if cur_price <= 0:
            cur_price = avg_price
        total_invested += size * avg_price
        total_current_value += size * cur_price
        positions_count += 1
    return {
        "total_invested": round(total_invested, 4),
        "total_current_value": round(total_current_value, 4),
        "positions_count": positions_count,
    }


def max_drawdown_pct(bankrolls: list[float]) -> float:
    """Worst peak-to-trough decline of a bankroll series, as a percentage.

    Runs a high-water mark over the series; a trough that is not below a
    previous peak contributes nothing. A zero or negative peak divides
    nothing (there is no equity to draw down from) and yields 0.
    """
    peak: float | None = None
    worst = 0.0
    for value in bankrolls:
        peak = value if peak is None else max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100.0)
    return worst


# --------------------------------------------------------------------------
# wallet fetch
# --------------------------------------------------------------------------


class _SnapshotFetch:
    """Pass-through Polymarket service that records one snapshot's fetches.

    ``kelly_service.compute_bankroll`` catches every upstream error and
    returns ``0.0`` instead of raising, so this writer could not otherwise
    tell "the wallet is empty" from "the CLOB is down". This wrapper keeps
    both swallow points visible (``failures``) and keeps the successful
    payloads (``usdc_balance`` / ``positions``) so the snapshot's position
    columns cost no extra upstream call.

    Everything else is delegated to the wrapped service, so the object can
    be handed straight to ``compute_bankroll``.
    """

    def __init__(self, service: Any):
        # Set before anything else: __getattr__ reads it for every other
        # attribute and must not recurse.
        self._service = service
        self.positions: list = []
        self.usdc_balance = 0.0
        self.failures: list[str] = []

    def __getattr__(self, name: str):
        return getattr(self._service, name)

    async def get_wallet_balance(self, *args, **kwargs):
        try:
            balance = await self._service.get_wallet_balance(*args, **kwargs)
        except Exception:
            self.failures.append("balance")
            raise
        self.usdc_balance = _to_float((balance or {}).get("usdc_balance"))
        return balance

    async def get_positions(self, *args, **kwargs):
        try:
            positions = await self._service.get_positions(*args, **kwargs)
        except Exception:
            self.failures.append("positions")
            raise
        self.positions = positions if isinstance(positions, list) else []
        return positions


def _load_credentials(wallet_address: str) -> tuple[str | None, dict | None]:
    """Stored private key + CLOB creds, or ``(None, None)`` when unavailable."""
    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError:
        return None, None
    except Exception as exc:  # noqa: BLE001 — credential store must never kill the cycle
        logger.warning("Equity snapshot credential lookup failed for %s: %s", wallet_address, exc)
        return None, None
    if not stored:
        return None, None
    return stored.get("private_key"), stored.get("clob_creds")


def _user_settings(db: Session, user_id: int) -> UserSettings | None:
    return db.query(UserSettings).filter(UserSettings.user_id == user_id).first()


# --------------------------------------------------------------------------
# writer
# --------------------------------------------------------------------------


async def capture_user_snapshot(db: Session, user: User) -> PortfolioSnapshot | None:
    """Sample one user's equity and append it to the series.

    Returns the new row, or ``None`` when nothing was written: the current
    hour already has a row, either wallet fetch is unusable, or the user is
    in paper mode (see :func:`_append_paper_snapshot`).
    """
    bucket = truncate_to_hour(utc_now())
    existing = (
        db.query(PortfolioSnapshot)
        .filter(
            PortfolioSnapshot.user_id == user.id,
            PortfolioSnapshot.bucket_hour == bucket,
        )
        .first()
    )
    if existing is not None:
        return None

    settings_row = _user_settings(db, user.id)
    if settings_row is not None and bool(settings_row.simulation_mode):
        return _append_paper_snapshot(db, user, bucket, settings_row)

    private_key, clob_creds = _load_credentials(user.wallet_address)

    fetcher = _SnapshotFetch(get_polymarket_service())
    try:
        bankroll = await kelly_service.compute_bankroll(
            fetcher,
            user.wallet_address,
            private_key=private_key,
            clob_creds=clob_creds,
        )
    except Exception as exc:
        # compute_bankroll is documented as never-raising; treat a breach
        # exactly like a failed fetch — skip, never write a zero row.
        logger.error(
            "Equity snapshot bankroll lookup failed for user %d: %s",
            user.id,
            exc,
        )
        return None

    if fetcher.failures:
        logger.warning(
            "Equity snapshot skipped for user %d: %s fetch failed — no row written",
            user.id,
            " + ".join(fetcher.failures),
        )
        return None

    totals = position_totals(fetcher.positions)
    usdc_balance = round(fetcher.usdc_balance, 4)
    bankroll = round(_to_float(bankroll), 4)

    snapshot = PortfolioSnapshot(
        user_id=user.id,
        bucket_hour=bucket,
        captured_at=utc_now(),
        usdc_balance=usdc_balance,
        # Algebraic consequence of the stored bankroll, never a second
        # sum over the positions.
        unrealized_pnl=round(bankroll - usdc_balance, 4),
        bankroll=bankroll,
        total_invested=totals["total_invested"],
        total_current_value=totals["total_current_value"],
        positions_count=totals["positions_count"],
        is_paper=False,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    logger.info(
        "Equity snapshot user=%d bucket=%s bankroll=%.2f positions=%d",
        user.id,
        bucket.isoformat(),
        bankroll,
        totals["positions_count"],
    )
    return snapshot


def _append_paper_snapshot(
    db: Session,
    user: User,
    bucket: datetime,
    settings_row: UserSettings,
) -> PortfolioSnapshot:
    """Write the paper account's equity point.

    Paper equity is ``UserSettings.paper_balance``. The wallet columns stay
    zero and ``is_paper`` is set, so the paper series is unmistakably a
    different account from the live one.
    """
    paper_balance = round(_to_float(settings_row.paper_balance), 4)
    snapshot = PortfolioSnapshot(
        user_id=user.id,
        bucket_hour=bucket,
        captured_at=utc_now(),
        usdc_balance=0.0,
        unrealized_pnl=0.0,
        bankroll=paper_balance,
        total_invested=0.0,
        total_current_value=0.0,
        positions_count=0,
        is_paper=True,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    logger.info(
        "Equity snapshot (paper) user=%d bucket=%s bankroll=%.2f",
        user.id,
        bucket.isoformat(),
        paper_balance,
    )
    return snapshot


async def run_snapshot_cycle(db: Session) -> dict[str, int]:
    """Write one snapshot per user. Per-user errors never abort the cycle.

    Users are sampled concurrently behind a semaphore, so a slow or hung
    CLOB/RPC call delays only its own user: with ``return_exceptions=True``
    every other user still gets its point, and a user that fails (or times
    out) is counted as skipped instead of written as a zero row.
    """
    users = db.query(User).all()
    if not users:
        return {"users": 0, "written": 0, "skipped": 0}

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SNAPSHOTS)

    async def _capture(user: User) -> PortfolioSnapshot | None:
        async with semaphore:
            return await asyncio.wait_for(
                capture_user_snapshot(db, user),
                timeout=SNAPSHOT_USER_TIMEOUT_SECONDS,
            )

    results = await asyncio.gather(
        *(_capture(user) for user in users),
        return_exceptions=True,
    )

    written = 0
    skipped = 0
    for user, outcome in zip(users, results, strict=False):
        if isinstance(outcome, BaseException):
            db.rollback()
            logger.error(
                "Equity snapshot failed for user %d: %s",
                user.id,
                outcome,
            )
            skipped += 1
            continue
        if outcome is None:
            skipped += 1
        else:
            written += 1
    return {"users": len(users), "written": written, "skipped": skipped}


async def _snapshot_loop():
    """Background loop: sample every user's equity on the configured interval.

    Shape copied from ``position_lifecycle_service._lifecycle_loop``:
    advisory lock first, then work → heartbeat → sleep → release, with
    every iteration error-isolated so one bad pass cannot kill the loop.
    """
    if not acquire_scheduler_lock(SCHEDULER_NAME):
        return
    interval = snapshot_interval()
    logger.info("Equity snapshot manager started (interval=%ds)", interval)

    try:
        while True:
            try:
                db = SessionLocal()
                try:
                    result = await run_snapshot_cycle(db)
                    if result["written"] or result["skipped"]:
                        logger.info(
                            "Equity snapshot pass: %d written, %d skipped, %d users",
                            result["written"],
                            result["skipped"],
                            result["users"],
                        )
                finally:
                    db.close()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Equity snapshot loop error: %s", e)

            scheduler_heartbeat(SCHEDULER_NAME)
            await asyncio.sleep(interval)
    finally:
        release_scheduler_lock(SCHEDULER_NAME)

    logger.info("Equity snapshot manager stopped")


def snapshot_interval() -> int:
    """Effective loop interval in seconds (floored at 60s)."""
    try:
        return max(60, int(get_settings().equity_snapshot_interval_seconds))
    except (TypeError, ValueError):
        return DEFAULT_SNAPSHOT_INTERVAL


async def start_equity_snapshot_service():
    """Start the equity snapshot background task."""
    global _snapshot_task
    if _snapshot_task and not _snapshot_task.done():
        logger.warning("Equity snapshot manager already running")
        return
    _snapshot_task = asyncio.create_task(_snapshot_loop())
    logger.info("Equity snapshot manager started")


async def stop_equity_snapshot_service():
    """Stop the equity snapshot background task."""
    global _snapshot_task
    if _snapshot_task and not _snapshot_task.done():
        _snapshot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _snapshot_task
    _snapshot_task = None
    logger.info("Equity snapshot manager stopped")


def is_equity_snapshot_running() -> bool:
    """Whether the background snapshot task is alive (metrics/health checks)."""
    return _snapshot_task is not None and not _snapshot_task.done()


def get_equity_snapshot_metrics() -> dict[str, Any]:
    return {
        "running": is_equity_snapshot_running(),
        "interval_seconds": snapshot_interval(),
        "scheduler_name": SCHEDULER_NAME,
    }


# --------------------------------------------------------------------------
# read path
# --------------------------------------------------------------------------


def serialize_snapshot(snapshot: PortfolioSnapshot) -> dict[str, Any]:
    """Wire shape of one equity point (``EquitySnapshotPoint``)."""
    return {
        "captured_at": snapshot.captured_at.isoformat() if snapshot.captured_at else None,
        "bucket_hour": snapshot.bucket_hour.isoformat() if snapshot.bucket_hour else None,
        "usdc_balance": snapshot.usdc_balance,
        "unrealized_pnl": snapshot.unrealized_pnl,
        "bankroll": snapshot.bankroll,
        "total_invested": snapshot.total_invested,
        "total_current_value": snapshot.total_current_value,
        "positions_count": snapshot.positions_count,
        "is_paper": snapshot.is_paper,
    }


def get_snapshots(
    db: Session,
    user_id: int,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = DEFAULT_SNAPSHOT_LIMIT,
    is_paper: bool | None = None,
) -> list[PortfolioSnapshot]:
    """Equity points for one user, oldest first.

    The window is inclusive of ``start`` and exclusive of ``end``; with no
    window the last ``limit`` points are returned. The limit is clamped so
    a hostile query string cannot pull the whole table.

    ``is_paper`` selects one account: ``True`` returns only paper points,
    ``False`` only live ones, and ``None`` (the default) returns both.
    Leaving it unset mixes two bankrolls on one series, so the read path
    that feeds the chart always passes the mode explicitly.
    """
    limit = max(1, min(int(limit or DEFAULT_SNAPSHOT_LIMIT), MAX_SNAPSHOT_LIMIT))
    query = db.query(PortfolioSnapshot).filter(PortfolioSnapshot.user_id == user_id)
    if is_paper is not None:
        query = query.filter(PortfolioSnapshot.is_paper == bool(is_paper))
    if start is not None:
        query = query.filter(PortfolioSnapshot.bucket_hour >= start)
    if end is not None:
        query = query.filter(PortfolioSnapshot.bucket_hour < end)
    # Take the newest ``limit`` rows, then re-order ascending so the chart
    # receives a forward-running series.
    rows = query.order_by(PortfolioSnapshot.bucket_hour.desc()).limit(limit).all()
    rows.reverse()
    return rows


def summarize_snapshots(
    db: Session,
    user_id: int,
    period: str | None = DEFAULT_PERIOD,
    *,
    now: datetime | None = None,
    is_paper: bool | None = None,
) -> dict[str, Any]:
    """Period-change metrics over one user's equity series.

    ``start``/``end`` are the first and last bankroll inside the window,
    ``delta`` and ``delta_pct`` the change between them, and
    ``max_drawdown_pct`` the worst peak-to-trough decline on the way.
    A user with no history gets zeros and ``points: 0`` — never an error,
    so the Dashboard can render its empty state.

    ``is_paper`` is forwarded to :func:`get_snapshots`: a summary that spans
    a mode switch would compute delta and drawdown across two different
    accounts, so it is filtered to the account being reported.
    """
    moment = now or utc_now()
    window = period_window(period)
    start = None if window is None else (moment - window)
    rows = get_snapshots(db, user_id, start=start, limit=MAX_SNAPSHOT_LIMIT, is_paper=is_paper)

    if not rows:
        return {
            "period": (period or DEFAULT_PERIOD),
            "start": 0.0,
            "end": 0.0,
            "delta": 0.0,
            "delta_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "points": 0,
        }

    first = _to_float(rows[0].bankroll)
    last = _to_float(rows[-1].bankroll)
    delta = last - first
    return {
        "period": (period or DEFAULT_PERIOD),
        "start": round(first, 4),
        "end": round(last, 4),
        "delta": round(delta, 4),
        "delta_pct": round(delta / first * 100.0, 4) if first else 0.0,
        "max_drawdown_pct": round(max_drawdown_pct([_to_float(row.bankroll) for row in rows]), 4),
        "points": len(rows),
    }
