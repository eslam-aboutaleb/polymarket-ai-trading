"""Supplement gateway evidence with canonical chain costs and scoped balance invariants."""

from datetime import UTC, datetime
from decimal import Decimal

from app.services.analytics.chain_evidence import (
    COLLATERAL,
    CTF,
    ChainReader,
    address,
    confirmed_order_cost,
    transfer_movements,
    wallet_logs,
)
from app.services.analytics.replay.ledger import accounting


@accounting
def verify_window(opening, closing, fills, *, transport=None, prior_state=None):
    """No network on GET: this is invoked only by explicit artifact verification POST."""
    a, b = opening.get("chain_snapshot"), closing.get("chain_snapshot")
    if not a or not b:
        raise ValueError("FINALIZED_BALANCE_OBSERVATIONS_REQUIRED")
    if any(
        s["chain_id"] != 137 or s["collateral"] != COLLATERAL or s["token_contract"] != CTF
        for s in (a, b)
    ):
        raise ValueError("UNSUPPORTED_CHAIN_ASSET_SCOPE")
    wallet = address(a["wallet"])
    if address(b["wallet"]) != wallet or int(a["block"]["number"], 16) >= int(
        b["block"]["number"], 16
    ):
        raise ValueError("INVALID_FINALIZED_CHAIN_WINDOW")
    reader = ChainReader(transport=transport, seconds=30)
    try:
        final = reader.finalized()
        if int(final["number"], 16) < int(b["block"]["number"], 16):
            raise ValueError("CLOSING_BLOCK_NOT_FINAL")
        for observation in (a, b):
            reader.canonical(observation["block"])
            reader.validate_assets(observation["block"])
        logs = wallet_logs(reader, wallet, a["block"], b["block"])
        movements = transfer_movements(logs, wallet)
        tokens = sorted(set(a["positions"]) | set(b["positions"]) | set(movements["positions"]))
        if len(tokens) > 20:
            raise ValueError("CHAIN_TOKEN_BOUND_EXCEEDED")
        bookends = []
        for source, observation in ((opening, a), (closing, b)):
            cash = reader.balance(wallet, None, observation["block"])
            positions = {
                token: str(reader.balance(wallet, token, observation["block"])) for token in tokens
            }
            if cash != Decimal(observation["cash_usdc"]) or any(
                Decimal(positions[token]) != Decimal(quantity)
                for token, quantity in observation["positions"].items()
            ):
                raise ValueError("PINNED_BALANCE_OBSERVATION_CHANGED")
            bookends.append(
                {
                    **source,
                    "cash_usdc": str(cash),
                    "positions": positions,
                    "capture_observed_at": source["observed_at"],
                    "observed_at": datetime.fromtimestamp(
                        int(observation["block"]["timestamp"], 16), UTC
                    ).isoformat(),
                    "coverage": "finalized_supported_asset_scope",
                }
            )
        expected_cash_change = Decimal(bookends[1]["cash_usdc"]) - Decimal(bookends[0]["cash_usdc"])
        if movements["cash"] != expected_cash_change or any(
            Decimal(bookends[1]["positions"][token]) - Decimal(bookends[0]["positions"][token])
            != movements["positions"].get(token, Decimal(0))
            for token in tokens
        ):
            raise ValueError("CHAIN_TRANSFER_BALANCE_INVARIANT_FAILED")
        # Venue trade IDs are not chain log IDs. Verify complete order/tx groups;
        # retain their member references rather than inventing individual fee splits.
        prior_sources = set((prior_state or {}).get("verified_gateway_source_ids", []))
        token_conditions = dict((prior_state or {}).get("token_conditions", {}))
        groups = {}
        for fill in fills:
            previous_condition = token_conditions.get(fill["token_id"])
            if previous_condition is not None and previous_condition != fill["condition_id"]:
                raise ValueError("TOKEN_CONDITION_LINEAGE_CONFLICT")
            token_conditions[fill["token_id"]] = fill["condition_id"]
            groups.setdefault((fill["order_hash"], fill["transaction_hash"].lower()), []).append(
                fill
            )
        if len(groups) > 50:
            raise ValueError("CHAIN_RECEIPT_BOUND_EXCEEDED")
        verified, period_cash, reasons = [], {}, []
        for (order, tx), rows in groups.items():
            if all(row["source_id"] in prior_sources for row in rows):
                continue
            first = rows[0]
            if any(
                any(
                    row[key] != first[key]
                    for key in ("token_id", "side", "strategy", "condition_id", "intent_source_id")
                )
                for row in rows
            ):
                raise ValueError("ORDER_TRANSACTION_IDENTITY_CONFLICT")
            shares = sum((Decimal(row["shares"]) for row in rows), Decimal(0))
            principal = sum((Decimal(row["notional"]) for row in rows), Decimal(0))
            proof = confirmed_order_cost(
                reader,
                wallet=wallet,
                order_hash=order,
                token=first["token_id"],
                side=first["side"],
                shares=shares,
                notional=principal,
                tx=tx,
                final_block=final,
            )
            if proof["block_number"] > int(b["block"]["number"], 16):
                continue
            value = {
                **first,
                "fill_id": "chain:" + tx + ":" + order,
                "shares": str(shares),
                "notional": str(principal),
                "fee_usdc": proof["fee_usdc"],
                # V2 OrderFilled has a nonnegative collateral fee and no inline rebate.
                # Other credits are retained as separate chain cash movements below.
                "confirmed_rebate_usdc": "0",
                "chain_cost_evidence": proof,
                "gateway_source_ids": sorted(row["source_id"] for row in rows),
                "gateway_fill_ids": sorted(row["fill_id"] for row in rows),
                "market_matched_at": first["matched_at"],
                "matched_at": datetime.fromtimestamp(proof["block_timestamp"], UTC).isoformat(),
                "execution_sequence": [
                    proof["block_number"],
                    proof["transaction_index"],
                    min(proof["log_indices"]),
                ],
            }
            value["estimated_rebate_usdc"] = str(
                sum((Decimal(row.get("estimated_rebate_usdc") or 0) for row in rows), Decimal(0))
            )
            verified.append(value)
            prior_sources.update(value["gateway_source_ids"])
            if proof["block_number"] > int(a["block"]["number"], 16):
                signed = 1 if first["side"] == "BUY" else -1
                period_cash[tx] = (
                    period_cash.get(tx, Decimal(0))
                    - signed * principal
                    - Decimal(proof["fee_usdc"])
                )
        from app.services.analytics.chain_redemptions import redemption_fills

        redemptions, redemption_proofs = redemption_fills(
            reader,
            wallet,
            movements,
            [
                *fills,
                *(
                    {"token_id": token, "condition_id": condition}
                    for token, condition in token_conditions.items()
                ),
            ],
            b["block"],
        )
        verified.extend(redemptions)
        for redemption in redemptions:
            tx = redemption["transaction_hash"]
            period_cash[tx] = period_cash.get(tx, Decimal(0)) + Decimal(redemption["notional"])
        residuals, external = {}, []
        for tx, change in movements["cash_by_tx"].items():
            if tx in period_cash:
                residual = change - period_cash[tx]
                if residual:
                    residuals[tx] = str(residual)
                    reasons.append("UNEXPLAINED_CASH_IN_TRADING_TRANSACTION")
            else:
                external.append(
                    {
                        "transaction_hash": tx,
                        "cashflow": str(change),
                        "classification": "unattributed_external_cash_not_strategy_profit",
                    }
                )
        if any(tx not in movements["cash_by_tx"] for tx, change in period_cash.items() if change):
            raise ValueError("CONFIRMED_FILL_CASH_TRANSFER_MISSING")
        if any(
            entry["token_id"] is not None
            and Decimal(entry["quantity_change"]) != 0
            and entry["transaction_hash"] not in period_cash
            for entry in movements["entries"]
        ):
            reasons.append("UNATTRIBUTED_TOKEN_MOVEMENT")
        activity = closing.get("wallet_evidence", {}).get("activity", [])
        for row in external:
            candidates = [
                item
                for item in activity
                if str(item.get("transaction_hash", "")).lower() == row["transaction_hash"]
            ]
            row["kind"] = "UNCLASSIFIED"
            if len(candidates) == 1:
                item = candidates[0]
                kind = item.get("type")
                if kind in {"DEPOSIT", "WITHDRAWAL", "MAKER_REBATE", "TAKER_REBATE"}:
                    amount = Decimal(str(item.get("usdc_size")))
                    signed = -amount if kind == "WITHDRAWAL" else amount
                    if amount.is_finite() and amount >= 0 and signed == Decimal(row["cashflow"]):
                        row.update(
                            kind=kind,
                            activity_evidence=item,
                            classification="chain_transfer_with_venue_activity_classification",
                            activity_receipt_hashes=[
                                item["sha256"]
                                for item in closing.get("wallet_evidence", {}).get("receipts", [])
                            ],
                        )
            if row["kind"] == "UNCLASSIFIED" and Decimal(row["cashflow"]) != 0:
                reasons.append("UNCLASSIFIED_EXTERNAL_COLLATERAL_FLOW")
        for observation in (a, b):
            reader.canonical(observation["block"])
        return {
            "opening": bookends[0],
            "closing": bookends[1],
            "fills": verified,
            "version": "chain-attribution-ctf-v2-v1",
            "verified_gateway_source_ids": sorted(prior_sources)[-5000:],
            "token_conditions": {
                token: condition
                for token, condition in token_conditions.items()
                if Decimal(bookends[1]["positions"].get(token, "0")) > 0
            },
            "chain_id": 137,
            "asset_scope": {"collateral": COLLATERAL, "token_contract": CTF, "token_ids": tokens},
            "balance_invariant_verified": True,
            "scope_reconciled": not reasons,
            "full_wallet_inventory_verified": False,
            "cash_transaction_residuals": residuals,
            "external_cashflows": external,
            "transfer_entries": movements["entries"],
            "redemption_proofs": redemption_proofs,
            "rpc_evidence": reader.responses,
            "reasons": sorted(set(reasons)),
        }
    finally:
        reader.close()
