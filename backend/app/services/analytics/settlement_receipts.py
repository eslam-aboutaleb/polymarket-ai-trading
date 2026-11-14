"""Read-only Polygon receipt verification; fixed RPC, no signer or user URLs.

CTF event ABI: gnosis/conditional-tokens-contracts/contracts/ConditionalTokens.sol.
Only root legacy USDC.e redemption events are supported; other collateral,
proxy recipients, nested positions or unavailable evidence abstain.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext

from eth_abi import decode
from eth_abi.exceptions import DecodingError
from web3 import Web3

RPC_URL = "https://polygon.drpc.org"
CTF = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
COLLATERAL = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
PAYOUT_TOPIC = Web3.keccak(
    text="PayoutRedemption(address,address,bytes32,bytes32,uint256[],uint256)"
).to_0x_hex()
HEX32 = re.compile(r"0x[0-9a-fA-F]{64}")


async def rpc(client, method, params):
    """Bound every response; JSON-RPC error never becomes a successful empty result."""
    async with client.stream(
        "POST",
        RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        follow_redirects=False,
    ) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > 524288:
                raise ValueError("RECEIPT_RESPONSE_TOO_LARGE")
    result = json.loads(body)
    if (
        not isinstance(result, dict)
        or result.get("id") != 1
        or result.get("error")
        or "result" not in result
    ):
        raise ValueError("RPC_ERROR")
    return result["result"]


def verify_redemption_receipt(receipt, block, head, *, tx_hash, condition_id, wallet):
    try:
        with localcontext(Context(prec=80)):
            return _verify_redemption_receipt(
                receipt, block, head, tx_hash=tx_hash, condition_id=condition_id, wallet=wallet
            )
    except (DecodingError, KeyError, TypeError, AttributeError, ArithmeticError) as exc:
        raise ValueError("MALFORMED_CHAIN_RECEIPT") from exc


def _verify_redemption_receipt(receipt, block, head, *, tx_hash, condition_id, wallet):
    """Verify canonical block and event identities; return observed cash flow only.

    This does not infer realized P&L, remaining shares or account cash balances.
    Gas is reported in native units, never guessed into USDC.
    """
    if (
        not HEX32.fullmatch(tx_hash)
        or not HEX32.fullmatch(condition_id)
        or not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet)
    ):
        raise ValueError("INVALID_RECEIPT_IDENTITY")
    if not receipt or not block:
        raise ValueError("RECEIPT_OR_BLOCK_MISSING")
    if receipt.get("transactionHash", "").lower() != tx_hash.lower():
        raise ValueError("RECEIPT_TRANSACTION_MISMATCH")
    if not HEX32.fullmatch(receipt.get("blockHash", "")) or not HEX32.fullmatch(
        block.get("hash", "")
    ):
        raise ValueError("INVALID_BLOCK_IDENTITY")
    number = int(receipt["blockNumber"], 16)
    if (
        block.get("hash", "").lower() != receipt.get("blockHash", "").lower()
        or int(block["number"], 16) != number
    ):
        raise ValueError("NONCANONICAL_RECEIPT")
    if head - number + 1 < 128:
        raise ValueError("INSUFFICIENT_FINALITY")
    if int(receipt["status"], 16) != 1:
        raise ValueError("REVERTED_REDEMPTION")
    payout, indices = 0, []
    seen = set()
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if log.get("address", "").lower() != CTF or not topics or topics[0].lower() != PAYOUT_TOPIC:
            continue
        if (
            log.get("removed", False)
            or log.get("transactionHash", "").lower() != tx_hash.lower()
            or log.get("blockHash", "").lower() != block["hash"].lower()
        ):
            raise ValueError("REDEMPTION_LOG_IDENTITY_MISMATCH")
        identity = int(log["logIndex"], 16)
        if identity in seen:
            raise ValueError("DUPLICATE_REDEMPTION_LOG")
        seen.add(identity)
        if len(topics) != 4:
            raise ValueError("INVALID_REDEMPTION_TOPICS")
        condition, sets, amount = decode(
            ["bytes32", "uint256[]", "uint256"], bytes.fromhex(log["data"].removeprefix("0x"))
        )
        if "0x" + condition.hex() != condition_id.lower():
            continue
        if topics[1].lower() != "0x" + wallet.lower().removeprefix("0x").zfill(64):
            raise ValueError("REDEEMER_WALLET_NOT_VERIFIED")
        if topics[2].lower() != "0x" + COLLATERAL[2:].zfill(64) or int(topics[3], 16) != 0:
            raise ValueError("UNSUPPORTED_COLLATERAL_OR_PARENT")
        if not sets or any(index not in (1, 2) for index in sets) or len(set(sets)) != len(sets):
            raise ValueError("UNSUPPORTED_REDEMPTION_INDEX_SET")
        payout += amount
        indices.extend(sets)
    if not indices:
        raise ValueError("MATCHING_PAYOUT_EVENT_MISSING")
    digest = hashlib.sha256(
        json.dumps(
            {"receipt": receipt, "canonical_block": block, "head": head},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "status": "confirmed",
        "chain_id": 137,
        "transaction_hash": tx_hash,
        "condition_id": condition_id,
        "payout_usdc": str(Decimal(payout) / Decimal(1000000)),
        "gas_fee_native": str(
            Decimal(int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16))
            / Decimal(10**18)
        ),
        "block_hash": block["hash"],
        "confirmations": head - number + 1,
        "source_sha256": digest,
        "index_sets": indices,
    }


async def read_redemption_receipt(client, *, tx_hash, condition_id, wallet):
    if not HEX32.fullmatch(tx_hash) or not HEX32.fullmatch(condition_id):
        raise ValueError("INVALID_RECEIPT_IDENTITY")
    if int(await rpc(client, "eth_chainId", []), 16) != 137:
        raise ValueError("WRONG_CHAIN")
    receipt = await rpc(client, "eth_getTransactionReceipt", [tx_hash])
    if not isinstance(receipt, dict) or not receipt.get("blockNumber"):
        raise ValueError("RECEIPT_MISSING")
    block = await rpc(client, "eth_getBlockByNumber", [receipt["blockNumber"], False])
    head = int(await rpc(client, "eth_blockNumber", []), 16)
    result = verify_redemption_receipt(
        receipt, block, head, tx_hash=tx_hash, condition_id=condition_id, wallet=wallet
    )
    result["receipt_observed_at"] = datetime.now(UTC).isoformat()
    return result
