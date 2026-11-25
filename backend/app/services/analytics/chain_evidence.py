"""Bounded, read-only finalized Polygon evidence for CTF Exchange V2.

ABI source: Polymarket/ctf-exchange-v2 ccc0596074f4dfd62c944fbca4de252893b82b4b,
ITrading.sol. Fees are collateral, not fee-rate estimates. Unsupported contracts
and ambiguous order/transaction groupings abstain; no signer is imported.
"""

import json
import re
import time
from decimal import Decimal

import httpx
from eth_abi import decode, encode
from eth_utils import keccak
from py_clob_client_v2.config import get_contract_config

from app.config import get_settings
from app.services.analytics.replay.ledger import accounting

CONTRACTS = get_contract_config(137)
EXCHANGES = {CONTRACTS.exchange_v2.lower(), CONTRACTS.neg_risk_exchange_v2.lower()}
COLLATERAL = CONTRACTS.collateral.lower()
CTF = CONTRACTS.conditional_tokens.lower()
ORDER_FILLED = (
    "0x"
    + keccak(
        text="OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)"
    ).hex()
)
TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
SINGLE = "0x" + keccak(text="TransferSingle(address,address,address,uint256,uint256)").hex()
BATCH = "0x" + keccak(text="TransferBatch(address,address,address,uint256[],uint256[])").hex()
MICRO = Decimal(1_000_000)


def hex32(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise ValueError("CHAIN_HASH_INVALID")
    return value.lower()


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise ValueError("CHAIN_ADDRESS_INVALID")
    return value.lower()


def topic_address(value):
    return "0x" + "0" * 24 + address(value)[2:]


class ChainReader:
    def __init__(self, *, transport=None, seconds=20):
        self.deadline = time.monotonic() + seconds
        self.client = httpx.Client(transport=transport, follow_redirects=False, trust_env=False)
        self.calls = 0
        self.bytes_read = 0
        self.responses = []
        self.validated_exchanges = set()
        self.validated_asset_blocks = set()

    def close(self):
        self.client.close()

    def call(self, method, params):
        if method not in {
            "eth_chainId",
            "eth_getBlockByNumber",
            "eth_call",
            "eth_getTransactionReceipt",
            "eth_getLogs",
        }:
            raise ValueError("CHAIN_METHOD_FORBIDDEN")
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.calls > 200:
            raise ValueError("CHAIN_EVIDENCE_BOUND_EXCEEDED")
        payload = {"jsonrpc": "2.0", "id": self.calls, "method": method, "params": params}
        with self.client.stream(
            "POST", get_settings().polygon_rpc_url, json=payload, timeout=min(5, remaining)
        ) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 1048576 or time.monotonic() >= self.deadline:
                    raise ValueError("CHAIN_EVIDENCE_BOUND_EXCEEDED")
        self.bytes_read += len(body)
        if self.bytes_read > 8388608:
            raise ValueError("CHAIN_EVIDENCE_BOUND_EXCEEDED")
        data = json.loads(body)
        if data.get("id") != self.calls or data.get("error") or "result" not in data:
            raise ValueError("CHAIN_RPC_UNVERIFIED")
        self.responses.append({"method": method, "params": params, "result": data["result"]})
        return data["result"]

    def finalized(self):
        if self.call("eth_chainId", []) != "0x89":
            raise ValueError("CHAIN_ID_MISMATCH")
        block = self.call("eth_getBlockByNumber", ["finalized", False])
        if not isinstance(block, dict):
            raise ValueError("FINALIZED_BLOCK_UNAVAILABLE")
        hex32(block["hash"])
        int(block["number"], 16)
        int(block["timestamp"], 16)
        return block

    def canonical(self, block):
        current = self.call("eth_getBlockByNumber", [block["number"], False])
        if not current or current["hash"].lower() != block["hash"].lower():
            raise ValueError("CHAIN_BLOCK_REORGED")

    def view(self, contract, signature, types, values, output, block):
        calldata = "0x" + (keccak(text=signature)[:4] + encode(types, values)).hex()
        result = self.call(
            "eth_call",
            [
                {"to": address(contract), "data": calldata},
                {"blockHash": hex32(block["hash"]), "requireCanonical": True},
            ],
        )
        return decode(output, bytes.fromhex(result[2:]), strict=True)

    def validate_assets(self, block):
        if block["hash"] in self.validated_asset_blocks:
            return
        decimals = self.view(COLLATERAL, "decimals()", [], [], ["uint8"], block)[0]
        if decimals != 6:
            raise ValueError("UNSUPPORTED_COLLATERAL_DECIMALS")
        self.validated_asset_blocks.add(block["hash"])

    def validate_exchange(self, contract, block):
        key = (contract.lower(), block["hash"].lower())
        if key in self.validated_exchanges:
            return
        collateral = self.view(contract, "getCollateral()", [], [], ["address"], block)[0]
        ctf = self.view(contract, "getCtf()", [], [], ["address"], block)[0]
        if collateral.lower() != COLLATERAL or ctf.lower() != CTF:
            raise ValueError("EXCHANGE_ASSET_CONFIGURATION_CHANGED")
        self.validated_exchanges.add(key)

    def balance(self, wallet, token, block):
        # EIP-1898 pins every read to the same canonical block, not latest state.
        if token is None:
            target, signature, args, values = (
                COLLATERAL,
                "balanceOf(address)",
                ["address"],
                [address(wallet)],
            )
        else:
            target, signature, args, values = (
                CTF,
                "balanceOf(address,uint256)",
                ["address", "uint256"],
                [address(wallet), int(token)],
            )
        calldata = "0x" + (keccak(text=signature)[:4] + encode(args, values)).hex()
        result = self.call(
            "eth_call",
            [
                {"to": target, "data": calldata},
                {"blockHash": hex32(block["hash"]), "requireCanonical": True},
            ],
        )
        if not isinstance(result, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", result):
            raise ValueError("CHAIN_BALANCE_INVALID")
        return Decimal(int(result, 16)) / MICRO

    def receipt(self, transaction_hash, final_block):
        receipt = self.call("eth_getTransactionReceipt", [hex32(transaction_hash)])
        if (
            not isinstance(receipt, dict)
            or receipt.get("status") != "0x1"
            or hex32(receipt["transactionHash"]) != hex32(transaction_hash)
            or int(receipt["blockNumber"], 16) > int(final_block["number"], 16)
            or not isinstance(receipt.get("logs"), list)
            or len(receipt["logs"]) > 1000
        ):
            raise ValueError("RECEIPT_NOT_FINAL_SUCCESS")
        block = self.call("eth_getBlockByNumber", [receipt["blockNumber"], False])
        if not block or hex32(block["hash"]) != hex32(receipt["blockHash"]):
            raise ValueError("RECEIPT_REORGED")
        return receipt, block


@accounting
def snapshot_chain(wallet, tokens, *, transport=None):
    reader = ChainReader(transport=transport)
    try:
        if len(tokens) > 20:
            raise ValueError("CHAIN_TOKEN_BOUND_EXCEEDED")
        final = reader.finalized()
        reader.validate_assets(final)
        cash = reader.balance(wallet, None, final)
        positions = {token: str(reader.balance(wallet, token, final)) for token in sorted(tokens)}
        reader.canonical(final)
        return {
            "version": "polygon-ctf-v2-finalized-v1",
            "chain_id": 137,
            "wallet": address(wallet),
            "collateral": COLLATERAL,
            "token_contract": CTF,
            "block": final,
            "cash_usdc": str(cash),
            "positions": positions,
            "rpc_evidence": reader.responses,
            "trust": "configured_RPC_finalized_tag_and_EIP1898",
        }
    finally:
        reader.close()


@accounting
def confirmed_order_cost(
    reader, *, wallet, order_hash, token, side, shares, notional, tx, final_block
):
    """Match the complete order/transaction aggregate, never guess per-match fees."""
    receipt, block = reader.receipt(tx, final_block)
    reader.validate_assets(block)
    matching = []
    for log in receipt["logs"]:
        topics = log.get("topics", [])
        if (
            log.get("address", "").lower() not in EXCHANGES
            or not topics
            or topics[0].lower() != ORDER_FILLED
        ):
            continue
        if len(topics) != 4 or hex32(topics[1]) != hex32(order_hash):
            continue
        if topics[2].lower() != topic_address(wallet):
            raise ValueError("FILL_WALLET_MISMATCH")
        if log.get("removed") is True or hex32(log["blockHash"]) != hex32(receipt["blockHash"]):
            raise ValueError("FILL_LOG_REORGED")
        reader.validate_exchange(log["address"], block)
        values = decode(
            ["uint8", "uint256", "uint256", "uint256", "uint256", "bytes32", "bytes32"],
            bytes.fromhex(log["data"][2:]),
            strict=True,
        )
        direction, asset, making, taking, fee, _, _ = values
        if direction != (0 if side == "BUY" else 1) or asset != int(token):
            raise ValueError("FILL_ASSET_SIDE_MISMATCH")
        matching.append(
            (
                making if side == "SELL" else taking,
                taking if side == "SELL" else making,
                fee,
                int(log["logIndex"], 16),
            )
        )
    if not matching or len({row[3] for row in matching}) != len(matching):
        raise ValueError("FILL_RECEIPT_IDENTITY_UNVERIFIED")
    quantity = Decimal(sum(row[0] for row in matching)) / MICRO
    principal = Decimal(sum(row[1] for row in matching)) / MICRO
    if quantity != Decimal(shares) or principal != Decimal(notional):
        raise ValueError("ORDER_TRANSACTION_AGGREGATE_MISMATCH")
    return {
        "fee_usdc": str(Decimal(sum(row[2] for row in matching)) / MICRO),
        "fee_asset": COLLATERAL,
        "transaction_hash": hex32(tx),
        "order_hash": hex32(order_hash),
        "block_number": int(block["number"], 16),
        "block_hash": hex32(block["hash"]),
        "block_timestamp": int(block["timestamp"], 16),
        "transaction_index": int(receipt["transactionIndex"], 16),
        "log_indices": sorted(row[3] for row in matching),
        "receipt": receipt,
    }


def wallet_logs(reader, wallet, opening, closing):
    """Complete bounded interval for collateral and CTF transfers involving this wallet."""
    first, last = int(opening["number"], 16) + 1, int(closing["number"], 16)
    if not 0 <= last - first < 50000:
        raise ValueError("CHAIN_WINDOW_BOUND_EXCEEDED")
    logs = {}
    for start in range(first, last + 1, 10000):
        stop = min(start + 9999, last)
        for contract, topics in (
            (COLLATERAL, [TRANSFER, topic_address(wallet)]),
            (COLLATERAL, [TRANSFER, None, topic_address(wallet)]),
            (CTF, [[SINGLE, BATCH], None, topic_address(wallet)]),
            (CTF, [[SINGLE, BATCH], None, None, topic_address(wallet)]),
        ):
            values = reader.call(
                "eth_getLogs",
                [
                    {
                        "address": contract,
                        "fromBlock": hex(start),
                        "toBlock": hex(stop),
                        "topics": topics,
                    }
                ],
            )
            if not isinstance(values, list) or len(values) >= 1000:
                raise ValueError("CHAIN_LOG_COMPLETENESS_BOUND_EXCEEDED")
            for row in values:
                if (
                    row.get("removed") is True
                    or row.get("address", "").lower() != contract
                    or not start <= int(row["blockNumber"], 16) <= stop
                ):
                    raise ValueError("CHAIN_TRANSFER_SCOPE_MISMATCH")
                identity = (hex32(row["transactionHash"]), int(row["logIndex"], 16))
                if identity in logs and logs[identity] != row:
                    raise ValueError("CONFLICTING_CHAIN_LOG_IDENTITY")
                logs[identity] = row
                if len(logs) > 1000:
                    raise ValueError("CHAIN_LOG_COMPLETENESS_BOUND_EXCEEDED")
    # Verify canonical block identity even for movements not linked to an app order.
    blocks = {}
    for row in logs.values():
        number = row["blockNumber"]
        if number not in blocks:
            blocks[number] = reader.call("eth_getBlockByNumber", [number, False])
        if not blocks[number] or hex32(blocks[number]["hash"]) != hex32(row["blockHash"]):
            raise ValueError("CHAIN_TRANSFER_REORGED")
    return list(logs.values())


@accounting
def transfer_movements(logs, wallet):
    cash, tokens, cash_by_tx = Decimal(0), {}, {}
    entries = []
    for row in logs:
        topics = row["topics"]
        is_cash = row["address"].lower() == COLLATERAL
        if is_cash:
            if len(topics) != 3 or topics[0].lower() != TRANSFER:
                raise ValueError("UNSUPPORTED_COLLATERAL_TRANSFER")
            sender, recipient = topics[1].lower(), topics[2].lower()
            ids, quantities = [None], [int(row["data"], 16)]
        else:
            if len(topics) != 4 or topics[0].lower() not in {SINGLE, BATCH}:
                raise ValueError("UNSUPPORTED_CTF_TRANSFER")
            sender, recipient = topics[2].lower(), topics[3].lower()
            raw = bytes.fromhex(row["data"][2:])
            if topics[0].lower() == SINGLE:
                token, quantity = decode(["uint256", "uint256"], raw, strict=True)
                ids, quantities = [token], [quantity]
            else:
                ids, quantities = decode(["uint256[]", "uint256[]"], raw, strict=True)
            if len(ids) != len(quantities) or len(ids) > 128:
                raise ValueError("CTF_TRANSFER_BATCH_BOUND_EXCEEDED")
        direction = int(recipient == topic_address(wallet)) - int(sender == topic_address(wallet))
        if sender != topic_address(wallet) and recipient != topic_address(wallet):
            raise ValueError("CHAIN_TRANSFER_WALLET_MISMATCH")
        for token, amount in zip(ids, quantities, strict=True):
            change = direction * Decimal(amount) / MICRO
            if is_cash:
                cash += change
                tx = row["transactionHash"].lower()
                cash_by_tx[tx] = cash_by_tx.get(tx, Decimal(0)) + change
            else:
                key = str(token)
                if change:
                    tokens[key] = tokens.get(key, Decimal(0)) + change
            entries.append(
                {
                    "transaction_hash": row["transactionHash"].lower(),
                    "log_index": int(row["logIndex"], 16),
                    "token_id": str(token) if token is not None else None,
                    "quantity_change": str(change),
                    "contract": row["address"].lower(),
                    "operator": "0x" + topics[1][-40:] if not is_cash else None,
                    "sender": "0x" + sender[-40:],
                    "recipient": "0x" + recipient[-40:],
                }
            )
    return {"cash": cash, "positions": tokens, "cash_by_tx": cash_by_tx, "entries": entries}
