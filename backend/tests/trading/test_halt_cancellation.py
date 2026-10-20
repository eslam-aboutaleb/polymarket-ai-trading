"""Deterministic exchange faults: requests never stand in for evidence."""

from app.services.trading.halt_cancellation_worker import reconcile_cancellations


class Exchange:
    def __init__(self, *, cancel_timeout=False, verify_failure=False, final_status="CANCELED"):
        self.orders = {"order-1"}
        self.cancel_timeout = cancel_timeout
        self.verify_failure = verify_failure
        self.final_status = final_status
        self.queries = 0

    def get_orders(self):
        self.queries += 1
        if self.verify_failure and self.queries > 1:
            raise TimeoutError
        return [{"id": oid} for oid in self.orders]

    def cancel(self, order_id):
        if self.cancel_timeout:
            raise TimeoutError
        self.orders.discard(order_id)
        return {"success": True}

    def get_order(self, order_id):
        return {"id": order_id, "status": self.final_status}


def test_cancel_timeout_retains_live_exposure():
    report = reconcile_cancellations(Exchange(cancel_timeout=True))
    assert report["orders_still_open"] == 1
    assert report["orders_confirmed_cancelled"] == 0
    assert report["orders_unresolved"] == ["order-1"]


def test_cancel_acknowledgment_requires_verification():
    report = reconcile_cancellations(Exchange(verify_failure=True))
    assert report["orders_still_open"] is None
    assert report["orders_confirmed_cancelled"] == 0
    assert report["positions_confirmed_closed"] == 0


def test_disappearance_and_fill_race_are_not_cancellation_or_closure():
    report = reconcile_cancellations(Exchange(final_status="MATCHED"))
    assert report["orders_still_open"] == 0
    assert report["orders_confirmed_cancelled"] == 0
    assert report["orders_unresolved"] == ["order-1"]
    assert report["positions_confirmed_closed"] == 0


def test_confirmed_cancellation_does_not_release_position_halt():
    report = reconcile_cancellations(Exchange())
    assert report["orders_confirmed_cancelled"] == 1
    assert report["orders_unresolved"] == []
    assert report["positions_found"] is None
    assert report["reconciliation_status"] == "RECOVERY_REQUIRED"


def test_malformed_snapshot_is_unknown_not_empty():
    exchange = Exchange()
    exchange.get_orders = dict  # type: ignore[method-assign]
    report = reconcile_cancellations(exchange)
    assert report["open_orders_found"] is None
    assert report["orders_still_open"] is None
    assert "OPEN_ORDER_QUERY_FAILED" in report["errors"]


def test_new_order_without_committed_identity_is_reported_without_cancelling():
    exchange = Exchange()
    exchange.orders = {"order-1", "new-order"}
    report = reconcile_cancellations(exchange, known_order_ids={"order-1"})
    assert exchange.orders == {"new-order"}
    assert report["cancel_requests_submitted"] == 1
    assert report["orders_still_open"] == 1
    assert report["orders_unresolved"] == ["new-order"]
