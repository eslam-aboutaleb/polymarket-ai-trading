"""Classify native-indexed external flows without assigning them to strategy profit."""

from datetime import UTC, datetime
from decimal import Decimal

from app.services.analytics.replay.ledger import accounting


@accounting
def external_flows(opening, closing, fills):
    evidence = closing.get("wallet_evidence")
    base = {
        "quality": "unverified",
        "funding_cashflow": None,
        "unattributed_rebates": None,
        "other_income": None,
        "rows": [],
        "reasons": [],
    }
    if not evidence or evidence.get("activity_from") != opening["observed_at"]:
        base["reasons"].append("EXTERNAL_ACTIVITY_WINDOW_INCOMPLETE")
        return base
    start, end = map(datetime.fromisoformat, (opening["observed_at"], closing["observed_at"]))
    until = datetime.fromisoformat(evidence["activity_to"])
    # Observing activity before balances cannot establish activity through closing.
    # Preserve the evidence and range rather than inventing final indexer completeness.
    if until < end:
        base["reasons"].append("ACTIVITY_INDEXER_FINALITY_UNVERIFIED")
    funding, rebates, income = Decimal(0), Decimal(0), Decimal(0)
    txs, known_fills = set(), {row["transaction_hash"] for row in fills}
    for raw in evidence["activity"]:
        instant = datetime.fromtimestamp(int(raw["timestamp"]), UTC)
        if not start < instant <= end:
            continue
        kind, tx = raw.get("type"), raw.get("transaction_hash")
        if not isinstance(tx, str) or not tx:
            base["reasons"].append("ACTIVITY_TRANSACTION_UNVERIFIED")
            continue
        identity = (tx, kind)
        if identity in txs:
            base["reasons"].append("ACTIVITY_IDENTITY_AMBIGUOUS")
            continue
        txs.add(identity)
        if kind == "TRADE":
            if tx not in known_fills:
                base["reasons"].append("EXTERNAL_TRADE_NOT_ATTRIBUTED")
            continue
        if kind not in {"DEPOSIT", "WITHDRAWAL", "MAKER_REBATE", "TAKER_REBATE", "REWARD"}:
            base["reasons"].append("EXTERNAL_POSITION_OR_SETTLEMENT_ACTIVITY")
            continue
        amount = Decimal(str(raw.get("usdc_size")))
        if not amount.is_finite() or amount < 0:
            raise ValueError("INVALID_EXTERNAL_FLOW_AMOUNT")
        base["rows"].append(
            {
                "transaction_hash": tx,
                "kind": kind,
                "amount": str(amount),
                "occurred_at": instant.isoformat(),
                "quality": "venue_indexed",
            }
        )
        if kind in {"DEPOSIT", "WITHDRAWAL"}:
            funding += amount if kind == "DEPOSIT" else -amount
        elif kind in {"MAKER_REBATE", "TAKER_REBATE"}:
            rebates += amount
        else:
            income += amount
    if (
        "ACTIVITY_IDENTITY_AMBIGUOUS" not in base["reasons"]
        and "ACTIVITY_TRANSACTION_UNVERIFIED" not in base["reasons"]
    ):
        base.update(
            quality="venue_indexed_not_chain_final",
            funding_cashflow=funding,
            unattributed_rebates=rebates,
            other_income=income,
        )
    base["reasons"] = sorted(set(base["reasons"]))
    return base
