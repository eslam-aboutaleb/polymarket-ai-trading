"""Restartable, cancellation-only worker. Never signs or submits an order.

Discovered order IDs commit before any cancellation. The cancellation phase
retains a row lock until results commit. A crash leaves the operation PENDING
with those identities intact; no missing order implies a fill or cancellation.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.trading.trading_control import EmergencyHaltOperation, TradingControlState
from app.models.users.user import User
from app.services.trading.trading_halt_service import empty_report
from app.utils.time import utc_now

logger = logging.getLogger(__name__)


class CancellationAdapter(Protocol):
    def get_orders(self) -> list[dict]: ...
    def cancel(self, order_id: str) -> object: ...
    def get_order(self, order_id: str) -> dict: ...


def _open_order_ids(adapter: CancellationAdapter) -> set[str]:
    orders = adapter.get_orders()  # The SDK drains all pages, or raises.
    if not isinstance(orders, list):
        raise ValueError("Invalid open-order snapshot")
    ids = set()
    for order in orders:
        if not isinstance(order, dict) or not (order.get("id") or order.get("order_id")):
            raise ValueError("Missing order identity")
        ids.add(str(order.get("id") or order["order_id"]))
    return ids


def reconcile_cancellations(
    adapter: CancellationAdapter, *, known_order_ids: set[str] | None = None
) -> dict:
    """Count only explicit cancellation evidence, retaining all uncertainty."""
    report = empty_report()
    report["reconciliation_status"] = "RECOVERY_REQUIRED"
    # The legacy portfolio query has public/synthetic fallbacks. It cannot
    # establish authoritative position closure or release the risk halt.
    report["errors"].append("AUTHORITATIVE_POSITION_RECONCILIATION_REQUIRED")
    try:
        before = _open_order_ids(adapter) | (known_order_ids or set())
    except Exception:
        report["orders_unresolved"] = sorted(known_order_ids) if known_order_ids else None
        report["errors"].append("OPEN_ORDER_QUERY_FAILED")
        return report
    report["open_orders_found"] = len(before)
    report["known_order_ids"] = sorted(before)
    # In the worker, only identities committed in the discovery phase may
    # receive cancellation requests. Newly appearing orders remain reported
    # as unresolved and can be handled by a subsequent halt operation.
    cancel_ids = before if known_order_ids is None else known_order_ids
    for order_id in sorted(cancel_ids):
        report["cancel_requests_submitted"] += 1
        try:
            adapter.cancel(order_id)
        except Exception:
            # Do not log exception payloads containing authenticated requests.
            report["errors"].append(f"CANCEL_REQUEST_UNCERTAIN:{order_id}")
    try:
        after = _open_order_ids(adapter)
    except Exception:
        report["orders_unresolved"] = sorted(before)
        report["errors"].append("CANCELLATION_VERIFICATION_FAILED")
        return report
    report["orders_still_open"] = len(after)
    unresolved = before | after
    for order_id in sorted(before - after):
        try:
            evidence = adapter.get_order(order_id)
            identity = evidence.get("id") or evidence.get("order_id")
            status = str(evidence.get("status", "")).upper()
            if str(identity) == order_id and status in {"CANCELLED", "CANCELED"}:
                report["orders_confirmed_cancelled"] += 1
                unresolved.discard(order_id)
            else:
                report["errors"].append(f"ORDER_ACCOUNTING_REQUIRES_RECONCILIATION:{order_id}")
        except Exception:
            report["errors"].append(f"ORDER_OUTCOME_UNVERIFIED:{order_id}")
    report["orders_unresolved"] = sorted(unresolved)
    return report


def _adapter_for_user(db: Session, user_id: int) -> CancellationAdapter:
    from py_clob_client.signer import Signer

    from app.security.credential_store import load_wallet_credentials
    from app.services.trading.copy_trade_service import _get_poly_proxy_wallet_address
    from app.services.trading.pre_trade_gate import build_clob_client

    user = db.get(User, user_id)
    stored = load_wallet_credentials(user.wallet_address)
    if not stored:
        raise ValueError("Credentials unavailable")
    private_key = stored["private_key"]
    eoa = Signer(private_key, 137).address()
    proxy = _get_poly_proxy_wallet_address(eoa)
    # Cancellation must use the same maker identity as order creation. Guessing
    # EOA after a proxy lookup failure could query the wrong empty portfolio.
    if not proxy:
        raise ValueError("Wallet identity unverified")
    return build_clob_client(private_key, stored.get("clob_creds"), proxy)


def process_next_halt(
    session_factory: Callable[[], Session],
    adapter_factory: Callable[[Session, int], CancellationAdapter] = _adapter_for_user,
) -> bool:
    """Commit discovery before side effects, then claim cancellation work."""
    with session_factory() as db, db.begin():
        if db.get_bind().dialect.name != "postgresql":
            raise RuntimeError("Cancellation worker requires PostgreSQL row locks")
        operation = db.scalars(
            select(EmergencyHaltOperation)
            .where(EmergencyHaltOperation.state == "PENDING")
            .order_by(EmergencyHaltOperation.created_at, EmergencyHaltOperation.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).first()
        if operation is None:
            return False
        operation_id = operation.id
        if "known_order_ids" not in operation.report:
            try:
                adapter = adapter_factory(db, operation.user_id)
                discovered = sorted(_open_order_ids(adapter))
            except Exception:
                report = empty_report()
                report["reconciliation_status"] = "RECOVERY_REQUIRED"
                report["errors"] = ["AUTHENTICATED_ORDER_DISCOVERY_UNAVAILABLE"]
                _record_result(db, operation, report)
                return True
            operation.report = {
                **operation.report,
                "known_order_ids": discovered,
                "open_orders_found": len(discovered),
            }
            operation.version += 1
            operation.updated_at = utc_now()
        # The transaction commits the snapshot before any cancel call.

    with session_factory() as db, db.begin():
        operation = db.scalars(
            select(EmergencyHaltOperation)
            .where(
                EmergencyHaltOperation.id == operation_id,
                EmergencyHaltOperation.state == "PENDING",
            )
            .with_for_update(skip_locked=True)
        ).one_or_none()
        if operation is None:
            return False  # Another worker claimed or completed it after discovery.
        known_ids = set(operation.report["known_order_ids"])
        try:
            report = reconcile_cancellations(
                adapter_factory(db, operation.user_id), known_order_ids=known_ids
            )
        except Exception:
            report = empty_report()
            report["reconciliation_status"] = "RECOVERY_REQUIRED"
            report["errors"] = ["AUTHENTICATED_CANCELLATION_UNAVAILABLE"]
            report["orders_unresolved"] = sorted(known_ids)
        report["known_order_ids"] = sorted(known_ids | set(report.get("known_order_ids", [])))
        _record_result(db, operation, report)
        return True


def _record_result(db: Session, operation: EmergencyHaltOperation, report: dict) -> None:
    if operation.close_positions:
        report["errors"].append("POSITION_EXIT_REQUIRES_DURABLE_GATEWAY")
    operation.report = report
    operation.state = "RECOVERY_REQUIRED"
    operation.version += 1
    operation.updated_at = utc_now()
    # Never change a newer halt or release any halt after cancellation.
    db.query(TradingControlState).filter(
        TradingControlState.scope == f"user:{operation.user_id}",
        TradingControlState.state == "HALTING",
        TradingControlState.version == operation.control_version,
    ).update(
        {
            "state": "RECOVERY_REQUIRED",
            "reason": "Review persisted cancellation evidence and reconcile wallet positions",
            "version": TradingControlState.version + 1,
            "updated_at": utc_now(),
        },
        synchronize_session=False,
    )


def finalize_global_halt(session_factory: Callable[[], Session]) -> None:
    """Once cancellation work drains, retain a global recovery barrier."""
    with session_factory() as db, db.begin():
        control = db.scalars(
            select(TradingControlState)
            .where(TradingControlState.scope == "global")
            .with_for_update()
        ).one_or_none()
        if control is None or control.state != "HALTING":
            return
        pending = db.scalars(
            select(EmergencyHaltOperation.id)
            .where(EmergencyHaltOperation.state == "PENDING")
            .limit(1)
        ).first()
        if pending is None:
            control.state = "RECOVERY_REQUIRED"
            control.reason = (
                "Cancellation attempts recorded; authoritative portfolio recovery required"
            )
            control.version += 1
            control.updated_at = utc_now()


async def run_halt_cancellation_worker() -> None:
    from app.utils.database import SessionLocal

    while True:
        try:
            processed = await asyncio.to_thread(process_next_halt, SessionLocal)
            await asyncio.to_thread(finalize_global_halt, SessionLocal)
        except Exception:
            logger.error("Halt cancellation worker failed; durable work remains pending")
            processed = False
        await asyncio.sleep(0.1 if processed else 5)
