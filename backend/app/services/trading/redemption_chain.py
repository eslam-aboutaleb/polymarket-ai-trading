"""Pinned CTF redemption reads and isolated EOA signing boundary.

CTF ABI/targets: docs.polymarket.com/trading/positions/manage (2026-11-22).
Other position systems and smart-wallet execution require a separately verified adapter.
"""

from decimal import Decimal

from eth_abi import encode
from eth_account import Account as EthAccount
from eth_utils import keccak
from web3 import Web3

from app.config import get_settings
from app.services.analytics.chain_evidence import (
    BATCH,
    COLLATERAL,
    CTF,
    MICRO,
    SINGLE,
    TRANSFER,
    ChainReader,
    address,
    hex32,
    topic_address,
    transfer_movements,
)
from app.services.analytics.chain_redemptions import redemption_fills
from app.services.analytics.replay.ledger import accounting

CTF_ADAPTER = "0xada100db00ca00073811820692005400218fce1f"
UNDERLYING = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
ZERO = bytes(32)
ABI_VERSION = "ctf-pusd-binary-redemption-v1"


def calldata(condition):
    return (
        "0x"
        + (
            keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
            + encode(
                ["address", "bytes32", "bytes32", "uint256[]"],
                [COLLATERAL, ZERO, bytes.fromhex(hex32(condition)[2:]), [1, 2]],
            )
        ).hex()
    )


class NativeRedemption:
    def __init__(self, *, reader=None, w3=None):
        self.reader = reader or ChainReader(seconds=30)
        self.w3 = w3  # No signing client, nonce or credentials are touched by preview.

    def close(self):
        self.reader.close()

    def signer(self):
        if self.w3 is None:
            self.w3 = Web3(
                Web3.HTTPProvider(get_settings().polygon_rpc_url, request_kwargs={"timeout": 10})
            )
        if self.w3.eth.chain_id != 137:
            raise ValueError("REDEMPTION_WRONG_CHAIN")
        return self.w3

    @accounting
    def preview(self, wallet, condition):
        wallet, condition = address(wallet), hex32(condition)
        reader = self.reader
        block = reader.finalized()
        reader.validate_assets(block)
        binary = bytes.fromhex(condition[2:])
        slots = reader.view(
            CTF, "getOutcomeSlotCount(bytes32)", ["bytes32"], [binary], ["uint256"], block
        )[0]
        denominator = reader.view(
            CTF, "payoutDenominator(bytes32)", ["bytes32"], [binary], ["uint256"], block
        )[0]
        if slots != 2 or denominator <= 0:
            raise ValueError("REDEMPTION_UNRESOLVED_OR_UNSUPPORTED")
        numerators = [
            reader.view(
                CTF,
                "payoutNumerators(bytes32,uint256)",
                ["bytes32", "uint256"],
                [binary, i],
                ["uint256"],
                block,
            )[0]
            for i in (0, 1)
        ]
        if sum(numerators) != denominator:
            raise ValueError("REDEMPTION_PAYOUT_VECTOR_INVALID")
        tokens, collections, quantities = [], [], []
        for index in (1, 2):
            collection = reader.view(
                CTF,
                "getCollectionId(bytes32,bytes32,uint256)",
                ["bytes32", "bytes32", "uint256"],
                [ZERO, binary, index],
                ["bytes32"],
                block,
            )[0]
            token = reader.view(
                CTF,
                "getPositionId(address,bytes32)",
                ["address", "bytes32"],
                [UNDERLYING, collection],
                ["uint256"],
                block,
            )[0]
            collections.append("0x" + collection.hex())
            tokens.append(str(token))
            quantities.append(int(reader.balance(wallet, str(token), block) * MICRO))
        payout = sum(q * p // denominator for q, p in zip(quantities, numerators, strict=True))
        approved = reader.view(
            CTF,
            "isApprovedForAll(address,address)",
            ["address", "address"],
            [wallet, CTF_ADAPTER],
            ["bool"],
            block,
        )[0]
        reader.canonical(block)
        return {
            "version": ABI_VERSION,
            "chain_id": 137,
            "wallet": wallet,
            "condition_id": condition,
            "target": CTF_ADAPTER,
            "collateral": COLLATERAL,
            "underlying": UNDERLYING,
            "token_ids": tokens,
            "collection_ids": collections,
            "quantities_base": quantities,
            "numerators": numerators,
            "denominator": denominator,
            "expected_payout": str(Decimal(payout) / MICRO),
            "approved": approved,
            "calldata": calldata(condition),
            "block": block,
            "rpc_evidence": reader.responses.copy(),
            "finality_policy": "polygon137-finalized-canonical-v1",
        }

    def prepare(self, wallet, snapshot):
        w3 = self.signer()
        if not snapshot["approved"]:
            raise ValueError("REDEMPTION_OPERATOR_APPROVAL_REQUIRED")
        if snapshot["target"] != CTF_ADAPTER or snapshot["calldata"] != calldata(
            snapshot["condition_id"]
        ):
            raise ValueError("REDEMPTION_CALLDATA_MISMATCH")
        tx = {
            "chainId": 137,
            "from": Web3.to_checksum_address(wallet),
            "to": Web3.to_checksum_address(CTF_ADAPTER),
            "data": snapshot["calldata"],
            "value": 0,
            "nonce": w3.eth.get_transaction_count(Web3.to_checksum_address(wallet), "pending"),
            "gasPrice": w3.eth.gas_price,
        }
        gas = w3.eth.estimate_gas(tx)
        if not 0 < gas <= 750_000 or not tx["gasPrice"] > 0 or gas * tx["gasPrice"] > 5 * 10**16:
            raise ValueError("REDEMPTION_GAS_BOUND_EXCEEDED")
        tx["gas"] = gas
        if w3.eth.get_balance(Web3.to_checksum_address(wallet)) < gas * tx["gasPrice"]:
            raise ValueError("REDEMPTION_GAS_BALANCE_INSUFFICIENT")
        return tx

    def sign(self, tx, private_key):
        signed = EthAccount.sign_transaction(tx, private_key)
        raw = signed.raw_transaction
        if address(EthAccount.recover_transaction(raw)) != address(tx["from"]):
            raise ValueError("REDEMPTION_SIGNER_MISMATCH")
        return raw, "0x" + keccak(raw).hex()

    def broadcast(self, raw):
        return Web3.to_hex(self.signer().eth.send_raw_transaction(raw)).lower()

    @accounting
    def reconcile(self, intent, snapshot):
        reader = self.reader
        final = reader.finalized()
        tx_hash = hex32(intent.transaction_hash)
        raw = reader.call("eth_getTransactionReceipt", [tx_hash])
        if raw is None:
            return {"state": "unknown", "reason": "REDEMPTION_RECEIPT_UNAVAILABLE"}
        if hex32(raw["transactionHash"]) != tx_hash:
            raise ValueError("REDEMPTION_RECEIPT_HASH_MISMATCH")
        if int(raw["blockNumber"], 16) > int(final["number"], 16):
            return {"state": "submitted", "reason": "REDEMPTION_AWAITING_FINALITY"}
        block = reader.call("eth_getBlockByNumber", [raw["blockNumber"], False])
        if block is None or hex32(block["hash"]) != hex32(raw["blockHash"]):
            raise ValueError("REDEMPTION_RECEIPT_REORGED")
        tx = self.signer().eth.get_transaction(tx_hash)
        if (
            Web3.to_hex(tx["hash"]).lower() != tx_hash
            or tx.get("chainId") != 137
            or address(tx["from"]) != address(intent.wallet)
            or address(tx["to"]) != CTF_ADAPTER
            or tx["nonce"] != intent.nonce
            or Web3.to_hex(tx["input"]).lower() != snapshot["calldata"].lower()
            or int(tx.get("value", -1)) != 0
        ):
            raise ValueError("REDEMPTION_TRANSACTION_IDENTITY_MISMATCH")
        if raw["status"] == "0x0":
            reader.canonical(final)
            return {"state": "failed", "reason": "REDEMPTION_CANONICAL_REVERT", "receipt": raw}
        wallet_topic = topic_address(intent.wallet)
        wallet_transfers = [
            entry
            for entry in raw["logs"]
            if entry.get("address", "").lower() in {COLLATERAL, CTF}
            and entry.get("topics")
            and entry["topics"][0].lower() in {TRANSFER, SINGLE, BATCH}
            and wallet_topic
            in [
                topic.lower()
                for topic in (
                    entry["topics"][1:3]
                    if entry["address"].lower() == COLLATERAL
                    else entry["topics"][2:4]
                )
            ]
        ]
        movements = transfer_movements(wallet_transfers, intent.wallet)
        fills, proofs = redemption_fills(
            reader,
            intent.wallet,
            movements,
            [
                {"token_id": token, "condition_id": intent.condition_id}
                for token in snapshot["token_ids"]
            ],
            final,
        )
        actual = {row["token_id"]: int(Decimal(row["shares"]) * MICRO) for row in fills}
        expected = {
            token: q
            for token, q in zip(snapshot["token_ids"], snapshot["quantities_base"], strict=True)
            if q
        }
        payout = sum((Decimal(row["notional"]) for row in fills), Decimal(0))
        if actual != expected or payout != Decimal(snapshot["expected_payout"]):
            raise ValueError("REDEMPTION_ENTITLEMENT_RECEIPT_MISMATCH")
        reader.canonical(final)
        gas = (
            int(raw["gasUsed"], 16) * int(raw["effectiveGasPrice"], 16)
            if raw.get("gasUsed") and raw.get("effectiveGasPrice")
            else None
        )
        return {
            "state": "confirmed",
            "reason": None,
            "payout": str(payout),
            "receipt": raw,
            "proofs": proofs,
            "gas_paid_wei": gas,
        }
