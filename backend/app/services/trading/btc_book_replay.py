"""Conservative paper replay using book state received by the order arrival."""

from decimal import Decimal, InvalidOperation

from app.services.trading.btc_paper_decision import visible_buy_estimate
from app.services.trading.btc_venue_evidence import taker_fee


def level(raw):
    price, size = Decimal(raw["price"]), Decimal(raw["size"])
    if not (price.is_finite() and 0 < price < 1 and size.is_finite() and size >= 0):
        raise ValueError("Invalid price level")
    return price, size


def replay(rows, *, token, arrival_ms, limit, notional, max_age_ms=20_000):
    if not rows:
        raise ValueError("Capture is empty")
    header = rows[0]
    if header.get("kind") != "start" or token not in header["tokens"]:
        raise ValueError("Replay token is not in capture header")
    if not (limit.is_finite() and 0 < limit <= 1 and notional.is_finite() and notional > 0):
        raise ValueError("Invalid replay order")
    if arrival_ms >= header["end"] * 1000:
        return {"status": "unknown", "reason": "MARKET_WINDOW_ENDED"}
    # Receiver order is the causal clock. Later frames may not repair earlier gaps.
    if not header["received_ms"] <= arrival_ms <= rows[-1]["received_ms"]:
        return {"status": "unknown", "reason": "OUTSIDE_CAPTURE"}
    bids, asks = {}, {}
    tick = Decimal(header["tick"])
    minimum = Decimal(header["minimum"])
    if not tick.is_finite() or tick <= 0 or not minimum.is_finite() or minimum <= 0:
        raise ValueError("Invalid venue size rules")
    ready, source_ms, trades = False, None, 0
    previous_ms = header["received_ms"]
    reason = "NO_SNAPSHOT"
    for index, row in enumerate(rows[1:], 1):
        received = row["received_ms"]
        if row["index"] != index or received < previous_ms:
            return {"status": "unknown", "reason": "CAPTURE_SEQUENCE_GAP"}
        previous_ms = received
        if received > arrival_ms:
            if row["kind"] == "gap":
                return {"status": "unknown", "reason": "ARRIVAL_BEFORE_DETECTED_GAP"}
            break
        if row["kind"] == "gap":
            ready, reason = False, "CAPTURE_GAP"
            continue
        if row["kind"] != "event":
            continue
        event = row["event"]
        if event.get("market") != header["condition"]:
            continue
        kind = event.get("event_type")
        if (
            kind in {"book", "last_trade_price", "tick_size_change"}
            and event.get("asset_id") != token
        ):
            continue
        changes = event.get("price_changes", [])
        changes = [change for change in changes if change.get("asset_id") == token]
        if kind == "price_change" and not changes:
            continue
        if kind == "market_resolved":
            return {"status": "unknown", "reason": "MARKET_RESOLVED"}
        if kind == "last_trade_price":
            trades += 1
            continue  # Reported trades do not convey queue position or depth updates.
        try:
            if kind == "tick_size_change":
                tick = Decimal(event["new_tick_size"])
                if not tick.is_finite() or tick <= 0:
                    raise ValueError("Invalid tick")
                continue
            if kind not in {"book", "price_change"}:
                continue
            timestamp = int(event["timestamp"])
            if timestamp > received + 1000 or (
                kind == "price_change" and source_ms is not None and timestamp < source_ms
            ):
                raise ValueError("Book timestamp inconsistent")
            if kind == "book":
                bids = dict(level(raw) for raw in event["bids"])
                asks = dict(level(raw) for raw in event["asks"])
                bids = {price: size for price, size in bids.items() if size}
                asks = {price: size for price, size in asks.items() if size}
                ready = True
            elif ready:
                for change in changes:
                    price, size = level(change)
                    side = {"BUY": bids, "SELL": asks}[change["side"]]
                    if size:
                        side[price] = size
                    else:
                        side.pop(price, None)
                for key, book, best in (("best_bid", bids, max), ("best_ask", asks, min)):
                    reported = changes[-1].get(key)
                    actual = best(book) if book else Decimal(0 if key == "best_bid" else 1)
                    if reported is not None and Decimal(reported) != actual:
                        raise ValueError("Book top disagrees with venue")
            if ready and bids and asks and max(bids) >= min(asks):
                raise ValueError("Crossed book")
            source_ms = timestamp
        except (KeyError, ValueError, TypeError, InvalidOperation):
            ready, reason = False, "INCONSISTENT_BOOK"
    if not ready:
        return {"status": "unknown", "reason": reason}
    if source_ms is None or not 0 <= arrival_ms - source_ms <= max_age_ms:
        return {"status": "unknown", "reason": "STALE_OR_FUTURE_BOOK"}
    if limit % tick:
        return {"status": "unknown", "reason": "INVALID_TICK"}
    estimate = visible_buy_estimate(list(asks.items()), limit, notional)
    rate = header.get("venue_taker_rate")
    fee = (
        sum(
            (taker_fee(price, size, Decimal(rate)) for price, size in estimate["levels"]),
            Decimal(0),
        )
        if rate is not None
        else None
    )
    status = "modeled" if estimate["shares"] >= minimum else "below_minimum"
    return {
        "status": status,
        "book_source_ms": source_ms,
        "trade_ticks": trades,
        "estimate": estimate,
        "venue_fee_estimate": fee,
    }
