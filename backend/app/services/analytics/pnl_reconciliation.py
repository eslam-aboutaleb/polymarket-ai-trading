"""Realized PnL reconciliation.

Every loss control in the bot reads ``UserTrade.pnl``: the daily loss limit, the
monthly loss limit, the maximum-drawdown layer and the total-loss halt in
``copy_trade_service._apply_global_safety_caps``. Nothing in the codebase ever
wrote that column, so all four layers evaluated against a permanent zero and the
bot had **no working loss circuit breaker**.

This module closes that gap. It walks a user's trades oldest-first, matches
sells against open buy lots with FIFO, and writes realized profit/loss onto the
closing sell row.

Design notes
------------
- **Idempotent.** Sells that already carry a ``pnl`` value are skipped, so the
  reconciler can run on every monitoring cycle without double-counting.
- **FIFO.** Longest-held lots are consumed first, which matches how the
  position ledger presents cost basis elsewhere.
- **USDC-denominated.** ``UserTrade.amount`` is a USDC notional and ``price`` is
  the execution price, so share quantity is ``amount / price``.
- **Partial closes** allocate PnL pro-rata across the lots they consume.
- Anything that cannot be interpreted (zero/negative price, missing timestamp)
  is skipped and logged rather than raising, because a single malformed row must
  not leave the risk controls without data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.models.trading.user_trade import UserTrade
from app.repositories.analytics.trades import AnalyticsTradeRepository
from app.services.analytics.replay.ledger import accounting
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

# Only these statuses represent a trade that actually reached the exchange.
# Anything pending/failed/cancelled must never contribute to realised PnL.
_REALISED_STATUSES = ("executed", "filled")


@dataclass
class OpenLot:
    """A remaining open buy lot, consumed FIFO by later sells."""

    trade_id: int
    remaining_shares: Decimal
    entry_price: Decimal
    strategy: str = "unknown"
    fee_per_share: Decimal | None = None


@accounting
def consume_fifo(lots: list[OpenLot], shares: Decimal, exit_price: Decimal):
    """One FIFO cost-basis primitive for legacy projections and verified reports."""
    remaining, gross, allocations = shares, Decimal(0), []
    for lot in lots:
        if remaining <= 0:
            break
        take = min(lot.remaining_shares, remaining)
        if take <= 0:
            continue
        basis = take * lot.entry_price
        pnl = take * exit_price - basis
        gross += pnl
        allocations.append(
            {
                "entry_id": lot.trade_id,
                "strategy": lot.strategy,
                "shares": take,
                "basis": basis,
                "gross_pnl": pnl,
                "entry_fee": take * lot.fee_per_share if lot.fee_per_share is not None else None,
            }
        )
        lot.remaining_shares -= take
        remaining -= take
    lots[:] = [lot for lot in lots if lot.remaining_shares > 0]
    return gross, remaining, allocations


@dataclass
class ReconciliationResult:
    """Outcome of reconciling one user's trades."""

    sells_processed: int = 0
    lots_consumed: int = 0
    realized_pnl: float = 0.0
    unmatched_sell_shares: float = 0.0
    open_lots_remaining: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


def _shares_for(trade: UserTrade) -> Decimal | None:
    try:
        price = Decimal(str(trade.filled_price if trade.filled_price is not None else trade.price))
        amount = Decimal(str(trade.amount))
        shares = (
            Decimal(str(trade.filled_size)) if trade.filled_size is not None else amount / price
        )
        if any(not value.is_finite() or value <= 0 for value in (price, amount, shares)):
            return None
        return shares
    except (InvalidOperation, ArithmeticError, ValueError, TypeError):
        return None


@accounting
def reconcile_realized_pnl(db: Session, user_id: int) -> ReconciliationResult:
    """Write realized PnL onto sell trades that close (part of) a buy position.

    Args:
        db: Open database session.
        user_id: Owner whose trades should be reconciled.

    Returns:
        A summary of what was reconciled, including realized PnL for the call.
    """
    result = ReconciliationResult()

    trades = AnalyticsTradeRepository(db).reconciliation_trades(user_id)

    open_lots: dict[tuple, list[OpenLot]] = {}

    for trade in trades:
        status = (trade.status or "").lower()
        if status not in _REALISED_STATUSES:
            result.skipped += 1
            continue

        shares = _shares_for(trade)
        if shares is None:
            result.skipped += 1
            result.warnings.append(f"trade {trade.id}: unusable price/amount")
            continue

        market_id = trade.market_id or ""
        identity = (
            trade.account_id,
            ("token", trade.token_id) if trade.token_id else ("legacy_market", market_id),
        )

        if trade.action == "buy":
            open_lots.setdefault(identity, []).append(
                OpenLot(
                    trade_id=trade.id,
                    remaining_shares=shares,
                    entry_price=Decimal(
                        str(trade.filled_price if trade.filled_price is not None else trade.price)
                    ),
                )
            )
            continue

        # ── Sell: match against open lots ──────────────────────────────
        already_reconciled = trade.pnl is not None
        lots = open_lots.get(identity, [])
        if not lots:
            # A sell with no recorded buy: cannot compute a basis. Flag it so
            # the gap is visible instead of silently treating it as break-even.
            result.unmatched_sell_shares += float(shares)
            result.warnings.append(f"sell {trade.id} on {market_id[:20]}: no open buy lot matched")
            continue

        sell_price = Decimal(
            str(trade.filled_price if trade.filled_price is not None else trade.price)
        )
        realized, remaining_to_close, allocations = consume_fifo(lots, shares, sell_price)
        consumed = len(allocations)
        # Existing closes still consume inventory on every replay. Their stored
        # legacy projection is not rewritten or counted as new profit.
        if already_reconciled:
            continue

        # Any sell quantity beyond the available basis is unmatched; it still
        # contributes PnL for the portion that did match.
        if remaining_to_close > 0:
            result.unmatched_sell_shares += float(remaining_to_close)
            result.warnings.append(
                f"sell {trade.id} on {market_id[:20]}: {remaining_to_close:.4f} shares "
                "exceeded the recorded buy lots"
            )

        trade.pnl = float(round(realized, 6))
        trade.updated_at = utc_now()
        result.sells_processed += 1
        result.lots_consumed += consumed
        result.realized_pnl += float(realized)

    result.open_lots_remaining = sum(len(v) for v in open_lots.values())

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    if result.sells_processed or result.warnings:
        logger.info(
            "PnL reconciliation user=%d: %d sells, %d lots consumed, "
            "realized=%.4f, unmatched=%.4f shares, %d still open",
            user_id,
            result.sells_processed,
            result.lots_consumed,
            result.realized_pnl,
            result.unmatched_sell_shares,
            result.open_lots_remaining,
        )
    for warning in result.warnings[:5]:
        logger.warning("PnL reconciliation user=%d: %s", user_id, warning)

    return result


def total_realized_pnl(db: Session, user_id: int) -> float:
    """Sum of every recorded realized PnL for a user.

    Returns 0.0 when nothing has been reconciled yet, which is why the loss
    controls must not treat a zero as "no loss occurred" without also checking
    whether any PnL data exists at all.
    """
    return AnalyticsTradeRepository(db).total_realized_pnl(user_id)


def has_pnl_history(db: Session, user_id: int) -> bool:
    """Whether any PnL value has ever been recorded for this user.

    Used to distinguish "the user has not lost money" from "we have no data",
    which are very different states for a circuit breaker.
    """
    return AnalyticsTradeRepository(db).has_pnl_history(user_id)


def unreconciled_sell_count(db: Session, user_id: int) -> int:
    """Number of realised sells still awaiting a ``pnl`` value.

    A persistently non-zero result means the reconciler is not keeping up.
    """
    return AnalyticsTradeRepository(db).unreconciled_sell_count(user_id, _REALISED_STATUSES)


def reconciliation_summary(db: Session, user_id: int) -> dict[str, Any]:
    """Compact health view of a user's PnL bookkeeping, for admin endpoints."""
    return {
        "user_id": user_id,
        "total_realized_pnl": round(total_realized_pnl(db, user_id), 4),
        "has_pnl_history": has_pnl_history(db, user_id),
        "unreconciled_sells": unreconciled_sell_count(db, user_id),
    }


@accounting
def reconcile_confirmed_cashflows(fills, opening, closing, *, opening_lots=None):
    """Read-only exact FIFO attribution between immutable balance observations.

    Uses the same consume_fifo primitive as the legacy projection. Missing costs,
    opening basis, external flows or incomplete coverage never become net profit.
    """
    for observation in (opening, closing):
        values = [
            Decimal(observation["cash_usdc"]),
            *map(Decimal, observation["positions"].values()),
        ]
        if any(not value.is_finite() or value < 0 for value in values):
            raise ValueError("INVALID_BALANCE_OBSERVATION")
    lots, strategies, seen, sources = {}, {}, {}, []
    ledger_rows, realized_allocations = [], []
    if opening_lots is not None:
        for token, entries in opening_lots.items():
            lots[token] = [
                OpenLot(
                    item["entry_source_id"],
                    Decimal(item["shares"]),
                    Decimal(item["entry_price"]),
                    item["strategy"],
                    Decimal(item["net_cost_per_share"])
                    if item["net_cost_per_share"] is not None
                    else None,
                )
                for item in entries
            ]
        expected = {
            token: sum((lot.remaining_shares for lot in entries), Decimal(0))
            for token, entries in lots.items()
        }
        actual = {token: Decimal(quantity) for token, quantity in opening["positions"].items()}
        if any(
            expected.get(token, 0) != actual.get(token, 0) for token in set(expected) | set(actual)
        ):
            raise ValueError("OPENING_LOT_QUANTITY_MISMATCH")
    reasons = []
    cashflow = Decimal(0)
    fees_known = rebates_known = True
    known_fees = confirmed_rebates = estimated_rebates = Decimal(0)
    quantity_changes = {}
    gross = Decimal(0)
    complete_basis = True
    if opening_lots is None and any(Decimal(value) != 0 for value in opening["positions"].values()):
        reasons.append("OPENING_COST_BASIS_UNVERIFIED")
        complete_basis = False

    def execution_time(row):
        instant = datetime.fromisoformat(row["matched_at"])
        if instant.tzinfo is None:
            raise ValueError("FILL_EXECUTION_TIME_UNVERIFIED")
        return instant.astimezone(UTC), tuple(row.get("execution_sequence", [row["source_id"]]))

    for fill in sorted(fills, key=execution_time):
        identity = (fill["order_hash"], fill["fill_id"])
        fingerprint = {k: v for k, v in fill.items() if k != "source_id"}
        if identity in seen:
            if seen[identity] != fingerprint:
                raise ValueError("CONFLICTING_FILL_IDENTITY")
            continue
        seen[identity] = fingerprint
        sources.append(fill["source_id"])
        token, side, strategy = fill["token_id"], fill["side"], fill["strategy"]
        shares, notional = Decimal(fill["shares"]), Decimal(fill["notional"])
        if (
            side not in {"BUY", "SELL"}
            or not shares.is_finite()
            or not notional.is_finite()
            or shares <= 0
            or (
                not 0 <= notional <= shares
                if fill.get("kind") == "SETTLEMENT"
                else not 0 < notional < shares
            )
        ):
            raise ValueError("INVALID_CONFIRMED_FILL")
        fee = Decimal(fill["fee_usdc"]) if fill.get("fee_usdc") is not None else None
        rebate = (
            Decimal(fill["confirmed_rebate_usdc"])
            if fill.get("confirmed_rebate_usdc") is not None
            else None
        )
        estimated = Decimal(fill.get("estimated_rebate_usdc") or 0)
        if any(
            not value.is_finite() or value < 0
            for value in (fee, rebate, estimated)
            if value is not None
        ):
            raise ValueError("INVALID_COST_EVIDENCE")
        fees_known &= fee is not None
        rebates_known &= rebate is not None
        known_fees += fee or 0
        confirmed_rebates += rebate or 0
        estimated_rebates += estimated
        cost = fee - rebate if fee is not None and rebate is not None else None
        signed = 1 if side == "BUY" else -1
        quantity_changes[token] = quantity_changes.get(token, Decimal(0)) + signed * shares
        cashflow += -signed * notional
        ledger_rows.append(
            {
                "source_id": fill["source_id"],
                "fill_id": fill["fill_id"],
                "order_hash": fill["order_hash"],
                "token_id": token,
                "side": side,
                "strategy": strategy,
                "gross_cashflow": -signed * notional,
                "fee_usdc": fee,
                "confirmed_rebate_usdc": rebate,
                "net_cashflow": -signed * notional - cost if cost is not None else None,
                "quantity_change": signed * shares,
            }
        )
        bucket = strategies.setdefault(
            strategy,
            {
                "gross_realized": Decimal(0),
                "allocated_costs": Decimal(0),
                "net_realized": Decimal(0),
                "fill_count": 0,
            },
        )
        bucket["fill_count"] += 1
        if side == "BUY":
            lots.setdefault(token, []).append(
                OpenLot(
                    fill["source_id"],
                    shares,
                    notional / shares,
                    strategy,
                    cost / shares if cost is not None else None,
                )
            )
        else:
            realized, unmatched, allocations = consume_fifo(
                lots.setdefault(token, []), shares, notional / shares
            )
            gross += realized
            if unmatched:
                complete_basis = False
                reasons.append("UNMATCHED_SELL_QUANTITY")
            for item in allocations:
                entry = strategies.setdefault(
                    item["strategy"],
                    {
                        "gross_realized": Decimal(0),
                        "allocated_costs": Decimal(0),
                        "net_realized": Decimal(0),
                        "fill_count": 0,
                    },
                )
                realized_allocations.append(
                    {
                        **item,
                        "exit_source_id": fill["source_id"],
                        "exit_fee": cost * item["shares"] / shares if cost is not None else None,
                    }
                )
                entry["gross_realized"] += item["gross_pnl"]
                if item["entry_fee"] is None or cost is None:
                    entry["net_realized"] = None
                    entry["allocated_costs"] = None
                elif entry["net_realized"] is not None:
                    allocated_cost = item["entry_fee"] + cost * item["shares"] / shares
                    entry["allocated_costs"] += allocated_cost
                    entry["net_realized"] += item["gross_pnl"] - allocated_cost
    tokens = set(opening["positions"]) | set(closing["positions"]) | set(quantity_changes)
    residuals = {
        token: Decimal(closing["positions"].get(token, "0"))
        - Decimal(opening["positions"].get(token, "0"))
        - quantity_changes.get(token, 0)
        for token in sorted(tokens)
    }
    if not fees_known:
        reasons.append("MISSING_CONFIRMED_FEES")
    if not rebates_known:
        reasons.append("MISSING_CONFIRMED_REBATES")
    net_cashflow = (
        cashflow - known_fees + confirmed_rebates if fees_known and rebates_known else None
    )
    cash_residual = (
        Decimal(closing["cash_usdc"]) - Decimal(opening["cash_usdc"]) - net_cashflow
        if net_cashflow is not None
        else None
    )
    if cash_residual is not None and abs(cash_residual) > Decimal("0.000001"):
        reasons.append("UNEXPLAINED_CASH_CHANGE")
    if any(abs(value) > Decimal("0.000001") for value in residuals.values()):
        reasons.append("UNEXPLAINED_TOKEN_CHANGE")
    if any(
        observation.get("coverage") not in {"complete_wallet", "finalized_supported_asset_scope"}
        for observation in (opening, closing)
    ):
        reasons.append("KNOWN_APPLICATION_TOKENS_ONLY")
    if not complete_basis:
        for item in strategies.values():
            item["net_realized"] = None
    net_realized = (
        sum((item["net_realized"] for item in strategies.values()), Decimal(0))
        if complete_basis
        and fees_known
        and rebates_known
        and all(item["net_realized"] is not None for item in strategies.values())
        else None
    )
    return {
        "schema_version": "live-attribution-v2",
        "ledger_rows": ledger_rows,
        "realized_allocations": realized_allocations,
        "closing_lots": {
            token: [
                {
                    "entry_source_id": lot.trade_id,
                    "shares": lot.remaining_shares,
                    "entry_price": lot.entry_price,
                    "strategy": lot.strategy,
                    "net_cost_per_share": lot.fee_per_share,
                }
                for lot in entries
            ]
            for token, entries in lots.items()
            if entries
        },
        "execution_eligible": False,
        "status": "reconciled" if not reasons else "abstain",
        "reasons": sorted(set(reasons)),
        "gross_realized_pnl": gross if complete_basis else None,
        "net_realized_pnl": net_realized,
        "gross_trading_cashflow": cashflow,
        "net_trading_cashflow": net_cashflow,
        "known_fees_usdc": known_fees,
        "total_fees_usdc": known_fees if fees_known else None,
        "confirmed_rebates_usdc": confirmed_rebates if rebates_known else None,
        "estimated_rebates_usdc": estimated_rebates,
        "cash_residual_usdc": cash_residual,
        "position_residuals": residuals,
        "unrealized_pnl": None,
        "available_cash_usdc": None,
        "source_ids": sources,
        "strategies": strategies,
        "cost_basis_policy": "fifo-entry-strategy-with-pro-rata-exit-costs-v1",
        "tolerance_usdc": "0.000001",
        "tolerance_shares": "0.000001",
    }
