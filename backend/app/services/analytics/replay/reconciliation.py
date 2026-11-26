"""Independent cash/share/fee reconciliation of a follower replay ledger (#58).

Net metrics are published only when the journal can be replayed from the funding
amount and lands on exactly the cash, fees, realized PnL and positions that the
ledger reports. The replay uses only the journal's recorded events, so a corrupted
or hand-edited stored result fails the check instead of showing a made-up number.
"""

from decimal import Decimal, localcontext

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT

ZERO = Decimal(0)


def _dec(value: str) -> Decimal:
    return Decimal(value)


def reconcile(funding: Decimal, journal: list[dict], valuation: dict) -> dict:
    """Replay ``journal`` from ``funding``; compare every step and the final state."""
    problems: list[str] = []
    with localcontext(ACCOUNTING_CONTEXT):
        cash, fees, realized = funding, ZERO, ZERO
        positions: dict[str, list[Decimal]] = {}
        orders: dict[str, dict] = {}
        try:
            for entry in journal:
                kind, details, event_id = entry["kind"], entry["details"], entry["event_id"]
                if kind == "ORDER_ACCEPTED":
                    token, side, shares, limit, fee_bps = details
                    orders[event_id] = {
                        "token": token,
                        "side": side,
                        "shares": _dec(shares),
                        "limit": _dec(limit),
                        "fee_bps": _dec(fee_bps),
                    }
                elif kind == "FILL":
                    order = orders[details[0]]
                    shares, price = _dec(details[1]), _dec(details[2])
                    notional = shares * price
                    fee = notional * order["fee_bps"] / 10000
                    position = positions.setdefault(order["token"], [ZERO, ZERO])
                    if order["side"] == "BUY":
                        cash -= notional + fee
                        position[0] += shares
                        position[1] += notional + fee
                    else:
                        released = (
                            position[1]
                            if shares == position[0]
                            else position[1] * shares / position[0]
                        )
                        cash += notional - fee
                        position[0] -= shares
                        position[1] -= released
                        realized += notional - fee - released
                    fees += fee
                    order["shares"] -= shares
                    if order["shares"] <= 0:
                        del orders[details[0]]
                elif kind == "CANCELED":
                    del orders[details[0]]
                elif kind == "SETTLED":
                    position = positions.setdefault(details[0], [ZERO, ZERO])
                    fee_bps = _dec(details[2]) if len(details) > 2 else ZERO
                    payout = position[0] * _dec(details[1])
                    fee = payout * fee_bps / 10000
                    cash += payout - fee
                    fees += fee
                    realized += payout - fee - position[1]
                    positions[details[0]] = [ZERO, ZERO]
                elif kind == "CASH_FLOW":
                    cash += _dec(details[0])
                    funding += _dec(details[0])
                else:
                    problems.append(f"{event_id}: unknown journal kind")
                    continue
                if (
                    _dec(entry["cash"]) != cash
                    or _dec(entry["fees"]) != fees
                    or _dec(entry["realized_pnl"]) != realized
                ):
                    problems.append(f"{event_id}: journal snapshot differs from replay")
        except (KeyError, ValueError, ArithmeticError, IndexError, ZeroDivisionError) as exc:
            problems.append(f"journal could not be replayed: {type(exc).__name__}")
        final = {
            "cash": cash,
            "fees": fees,
            "realized_pnl": realized,
        }
        for key, value in final.items():
            if _dec(valuation[key]) != value:
                problems.append(f"valuation {key} differs from replay")
        shown = {
            token: (_dec(row["shares"]), _dec(row["cost_basis"]))
            for token, row in valuation["positions"].items()
        }
        replayed = {t: (p[0], p[1]) for t, p in positions.items() if p[0]}
        if shown != replayed:
            problems.append("valuation positions differ from replay")
    return {
        "status": "reconciled" if not problems else "mismatch",
        "events": len(journal),
        "problems": problems,
    }


def withhold_unreconciled(scenario: dict) -> dict:
    """Net metrics are unknown unless the ledger reconciled; never show an unchecked number."""
    if scenario["reconciliation"]["status"] == "reconciled":
        return scenario
    valuation = dict(scenario["valuation"])
    for key in ("nav", "net_pnl", "realized_pnl", "unrealized_pnl", "position_value"):
        valuation[key] = None
    return {**scenario, "valuation": valuation, "metrics_status": "withheld_unreconciled"}
