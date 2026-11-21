"""Recognize supported binary CTF redemptions without ever submitting one.

PayoutRedemption/position derivation follow the deployed ConditionalTokens ABI.
Adapter payouts require wallet outflows, adapter burns and actual collateral
credit to agree. Unknown adapters, nested collections and mixed payouts abstain.
"""

from datetime import UTC, datetime
from decimal import Decimal

from eth_abi import decode
from eth_utils import keccak

from app.services.analytics.chain_evidence import (
    BATCH,
    COLLATERAL,
    CTF,
    MICRO,
    SINGLE,
    hex32,
    topic_address,
    transfer_movements,
)
from app.services.analytics.replay.ledger import accounting

PAYOUT = (
    "0x" + keccak(text="PayoutRedemption(address,address,bytes32,bytes32,uint256[],uint256)").hex()
)
ADAPTERS = {
    "0xada100db00ca00073811820692005400218fce1f",
    "0xada2005600dec949baf300f4c6120000bdb6eaab",
}
ZERO = "0x" + "0" * 40


@accounting
def redemption_fills(reader, wallet, movements, known_fills, final_block):
    conditions = {}
    for fill in known_fills:
        if fill["token_id"] in conditions and conditions[fill["token_id"]] != fill["condition_id"]:
            raise ValueError("TOKEN_CONDITION_LINEAGE_CONFLICT")
        conditions[fill["token_id"]] = fill["condition_id"]
    by_tx = {}
    for entry in movements["entries"]:
        if (
            entry["contract"] == CTF
            and Decimal(entry["quantity_change"]) < 0
            and entry["recipient"] in ADAPTERS | {ZERO}
        ):
            by_tx.setdefault(entry["transaction_hash"], []).append(entry)
    outputs, proofs = [], []
    for tx, entries in by_tx.items():
        if len(entries) > 20 or any(entry["token_id"] not in conditions for entry in entries):
            raise ValueError("REDEMPTION_BASIS_LINEAGE_UNAVAILABLE")
        receipt, block = reader.receipt(tx, final_block)
        payout_events = [
            log
            for log in receipt["logs"]
            if log.get("address", "").lower() == CTF
            and log.get("topics")
            and log["topics"][0].lower() == PAYOUT
        ]
        # Burning shares alone does not prove redemption: do not mistake a merge
        # or direct token transfer for a realized result.
        if len(payout_events) != 1:
            raise ValueError("REDEMPTION_EVENT_AMBIGUOUS")
        event = payout_events[0]
        topics = event["topics"]
        if len(topics) != 4 or topics[3].lower() != "0x" + "0" * 64:
            raise ValueError("NESTED_REDEMPTION_UNSUPPORTED")
        redeemer, underlying = "0x" + topics[1][-40:].lower(), "0x" + topics[2][-40:].lower()
        peer = entries[0]["recipient"]
        if any(
            entry["operator"].lower() != (wallet if peer == ZERO else peer) for entry in entries
        ):
            raise ValueError("REDEMPTION_OPERATOR_MISMATCH")
        if (peer == ZERO and redeemer != wallet) or (
            peer != ZERO and (peer not in ADAPTERS or redeemer != peer)
        ):
            raise ValueError("REDEMPTION_RECIPIENT_MISMATCH")
        condition, index_sets, total_payout = decode(
            ["bytes32", "uint256[]", "uint256"], bytes.fromhex(event["data"][2:]), strict=True
        )
        condition_hex = "0x" + condition.hex()
        if set(index_sets) - {1, 2} or not index_sets or len(set(index_sets)) != len(index_sets):
            raise ValueError("NONBINARY_REDEMPTION_UNSUPPORTED")
        if any(
            conditions[entry["token_id"]].lower() != condition_hex or entry["recipient"] != peer
            for entry in entries
        ):
            raise ValueError("REDEMPTION_CONDITION_MISMATCH")
        slots = reader.view(
            CTF, "getOutcomeSlotCount(bytes32)", ["bytes32"], [condition], ["uint256"], block
        )[0]
        denominator = reader.view(
            CTF, "payoutDenominator(bytes32)", ["bytes32"], [condition], ["uint256"], block
        )[0]
        if slots != 2 or denominator <= 0:
            raise ValueError("REDEMPTION_NOT_RESOLVED_BINARY")
        numerators = [
            reader.view(
                CTF,
                "payoutNumerators(bytes32,uint256)",
                ["bytes32", "uint256"],
                [condition, i],
                ["uint256"],
                block,
            )[0]
            for i in (0, 1)
        ]
        if sum(numerators) != denominator:
            raise ValueError("REDEMPTION_PAYOUT_VECTOR_INVALID")
        derived = {}
        for index in index_sets:
            collection = reader.view(
                CTF,
                "getCollectionId(bytes32,bytes32,uint256)",
                ["bytes32", "bytes32", "uint256"],
                [bytes(32), condition, index],
                ["bytes32"],
                block,
            )[0]
            token = reader.view(
                CTF,
                "getPositionId(address,bytes32)",
                ["address", "bytes32"],
                [underlying, collection],
                ["uint256"],
                block,
            )[0]
            derived[str(token)] = numerators[index - 1]
        quantities = {}
        for entry in entries:
            token = entry["token_id"]
            if token not in derived:
                raise ValueError("REDEMPTION_TOKEN_DERIVATION_MISMATCH")
            quantities[token] = quantities.get(token, Decimal(0)) - Decimal(
                entry["quantity_change"]
            )
        # The adapter must burn exactly this wallet's transferred quantities.
        if peer != ZERO:
            adapter_logs = [
                log
                for log in receipt["logs"]
                if log.get("address", "").lower() == CTF
                and log.get("topics")
                and log["topics"][0].lower() in {SINGLE, BATCH}
                and topic_address(peer) in [topic.lower() for topic in log["topics"][2:4]]
            ]
            adapter = transfer_movements(adapter_logs, peer)
            burns = {}
            for entry in adapter["entries"]:
                if entry["recipient"] == ZERO:
                    burns[entry["token_id"]] = burns.get(entry["token_id"], Decimal(0)) - Decimal(
                        entry["quantity_change"]
                    )
            if burns != quantities:
                raise ValueError("ADAPTER_REDEMPTION_BURN_MISMATCH")
        payouts = {
            token: int(quantity * MICRO) * derived[token] // denominator
            for token, quantity in quantities.items()
        }
        if sum(payouts.values()) != total_payout:
            raise ValueError("REDEMPTION_PAYOUT_MISMATCH")
        if movements["cash_by_tx"].get(tx, Decimal(0)) != Decimal(total_payout) / MICRO:
            raise ValueError("REDEMPTION_COLLATERAL_CREDIT_MISMATCH")
        expected_sender = CTF if peer == ZERO else ZERO
        cash_entries = [
            entry
            for entry in movements["entries"]
            if entry["transaction_hash"] == tx
            and entry["contract"] == COLLATERAL
            and Decimal(entry["quantity_change"]) != 0
        ]
        if any(
            Decimal(entry["quantity_change"]) < 0 or entry["sender"] != expected_sender
            for entry in cash_entries
        ):
            raise ValueError("REDEMPTION_CREDIT_SOURCE_AMBIGUOUS")
        # Require canonical receipt identity for each supporting transfer/event.
        for log in receipt["logs"]:
            if (
                hex32(log["blockHash"]) != hex32(receipt["blockHash"])
                or hex32(log["transactionHash"]) != hex32(tx)
                or log.get("removed") is True
            ):
                raise ValueError("REDEMPTION_LOG_IDENTITY_MISMATCH")
        proof = {
            "transaction_hash": tx,
            "receipt": receipt,
            "block": block,
            "payout_event_log_index": int(event["logIndex"], 16),
            "condition_id": condition_hex,
            "collateral_credit": str(Decimal(total_payout) / MICRO),
            "fee_asset": COLLATERAL,
            "version": "ctf-binary-redemption-credit-v1",
            "wallet": wallet,
        }
        proofs.append(proof)
        for index, (token, quantity) in enumerate(sorted(quantities.items())):
            outputs.append(
                {
                    "source_id": "redemption:" + tx + ":" + token,
                    "fill_id": "redemption:" + tx + ":" + token,
                    "order_hash": "redemption:" + tx,
                    "transaction_hash": tx,
                    "token_id": token,
                    "side": "SELL",
                    "strategy": "settlement",
                    "condition_id": condition_hex,
                    "shares": str(quantity),
                    "notional": str(Decimal(payouts[token]) / MICRO),
                    "fee_usdc": "0",
                    "confirmed_rebate_usdc": "0",
                    "kind": "SETTLEMENT",
                    "matched_at": datetime.fromtimestamp(
                        int(block["timestamp"], 16), UTC
                    ).isoformat(),
                    "execution_sequence": [
                        int(block["number"], 16),
                        int(receipt["transactionIndex"], 16),
                        int(event["logIndex"], 16),
                        index,
                    ],
                    "chain_redemption_evidence": proof,
                }
            )
    return outputs, proofs
