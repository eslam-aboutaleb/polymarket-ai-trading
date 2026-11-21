"""Point-in-time native-book attribution; benchmarks never become actual earnings."""

from decimal import Decimal

from app.services.analytics.paper_evidence import EvidenceUnavailable, current_book
from app.services.analytics.replay.ledger import accounting
from app.services.market_data.replay_capture import aware, digest

BENCHMARK_VERSION = "arrival-midpoint-spread-v1"
MARK_VERSION = "indicative-exit-bid-60s-v1"


@accounting
def benchmark_fill(db, order, fill, *, condition_id):
    """Only information already received at the immutable order decision is eligible."""
    base = {
        "version": BENCHMARK_VERSION,
        "source_id": fill["source_id"],
        "decision_at": aware(order.created_at).isoformat(),
        "capture_id": None,
        "midpoint": None,
        "spread_cost": None,
        "implementation_shortfall": None,
        "execution_difference": None,
        "profitability_claim": False,
    }
    try:
        evidence = current_book(
            db, order.token_id, aware(order.created_at), require_size_rules=False
        )
        if evidence.condition_id != condition_id:
            raise EvidenceUnavailable("BENCHMARK_CONDITION_MISMATCH")
        bids = [level.price for level in evidence.book.bids if level.shares > 0]
        asks = [level.price for level in evidence.book.asks if level.shares > 0]
        if not bids or not asks or max(bids) >= min(asks):
            raise EvidenceUnavailable("BENCHMARK_TWO_SIDED_BOOK_REQUIRED")
        bid, ask = max(bids), min(asks)
        midpoint = (bid + ask) / 2
        quantity, notional = Decimal(fill["shares"]), Decimal(fill["notional"])
        shortfall = (1 if order.side == "BUY" else -1) * (notional - quantity * midpoint)
        spread = quantity * (ask - bid) / 2
        base.update(
            status="observed_comparison",
            reason=None,
            capture_id=evidence.capture_id,
            rule_hash=evidence.rule_hash,
            received_at=evidence.book.observed_at.isoformat(),
            source_at=evidence.book.source_at.isoformat(),
            midpoint=midpoint,
            spread_cost=spread,
            implementation_shortfall=shortfall,
            execution_difference=shortfall - spread,
        )
    except (EvidenceUnavailable, ValueError, ArithmeticError) as exc:
        base.update(status="abstain", reason=str(exc))
    # Include the exact inputs rather than hashing only rounded output figures.
    base["benchmark_id"] = digest(
        [
            BENCHMARK_VERSION,
            fill,
            {
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in base.items()
            },
        ]
    )
    return base


@accounting
def mark_observation(db, observation, lots):
    """Indicative exit bids, separately labelled and never called a liquidation fill."""
    provenance, strategies, reasons = {}, {}, []
    value, gross_basis, net_basis = Decimal(0), Decimal(0), Decimal(0)
    costs_known = True
    for token, quantity_text in sorted(observation["positions"].items()):
        quantity = Decimal(quantity_text)
        if not quantity:
            continue
        entries = lots.get(token, [])
        if sum((Decimal(entry["shares"]) for entry in entries), Decimal(0)) != quantity:
            reasons.append("MARK_COST_BASIS_UNVERIFIED")
            continue
        try:
            from datetime import datetime

            evidence = current_book(
                db,
                token,
                datetime.fromisoformat(observation["observed_at"]),
                require_size_rules=False,
            )
            bids = [level.price for level in evidence.book.bids if level.shares > 0]
            if not bids:
                raise EvidenceUnavailable("MISSING_EXIT_BID")
            price = max(bids)
            provenance[token] = {
                "capture_id": evidence.capture_id,
                "price": str(price),
                "rule_hash": evidence.rule_hash,
                "received_at": evidence.book.observed_at.isoformat(),
                "source_at": evidence.book.source_at.isoformat(),
                "version": MARK_VERSION,
            }
            value += quantity * price
            for entry in entries:
                shares, entry_price = Decimal(entry["shares"]), Decimal(entry["entry_price"])
                basis = shares * entry_price
                gross_basis += basis
                cost = entry["net_cost_per_share"]
                net_basis += basis + (shares * Decimal(cost) if cost is not None else 0)
                costs_known &= cost is not None
                name = entry["strategy"]
                previous = strategies.setdefault(name, Decimal(0))
                strategies[name] = (
                    previous + shares * price - basis - shares * Decimal(cost)
                    if previous is not None and cost is not None
                    else None
                )
        except (EvidenceUnavailable, ValueError, ArithmeticError) as exc:
            reasons.append(str(exc))
    complete = not reasons
    return {
        "version": MARK_VERSION,
        "quality": "indicative_bid_not_liquidation_value",
        "position_value": value if complete else None,
        "observed_nav": Decimal(observation["cash_usdc"]) + value if complete else None,
        "gross_unrealized_pnl": value - gross_basis if complete else None,
        "unrealized_pnl": value - net_basis if complete and costs_known else None,
        "strategies": strategies,
        "provenance": provenance,
        "reasons": sorted(set(reasons)),
    }


@accounting
def settlement_observation(db, observation):
    """Venue-final payout evidence is distinct from a chain-final redemption receipt."""
    from datetime import datetime

    from app.services.analytics.paper_evidence import final_payout, latest

    as_of = datetime.fromisoformat(observation["observed_at"])
    rows = []
    for token, quantity in sorted(observation["positions"].items()):
        if Decimal(quantity) == 0:
            continue
        book = latest(db, "book", as_of, token_id=token)
        if book is None:
            rows.append(
                {"token_id": token, "status": "unknown", "reason": "MISSING_TOKEN_RULE_LINEAGE"}
            )
            continue
        try:
            payout = final_payout(db, token, book.condition_id, book.rule_hash, as_of)
            sources = [
                latest(db, kind, as_of, condition_id=book.condition_id).id
                for kind in ("clob_market", "gamma")
            ]
            rows.append(
                {
                    "token_id": token,
                    "status": "venue_final_unredeemed",
                    "payout_per_share": str(payout),
                    "shares": quantity,
                    "unredeemed_payout": str(Decimal(quantity) * payout),
                    "source_ids": sources,
                    "rule_hash": book.rule_hash,
                    "chain_receipt": None,
                    "cash_credit": None,
                }
            )
        except EvidenceUnavailable as exc:
            rows.append({"token_id": token, "status": "unknown", "reason": str(exc)})
    return {
        "version": "venue-final-versus-redemption-v1",
        "positions": rows,
        "chain_final_redemption_verified": False,
    }
