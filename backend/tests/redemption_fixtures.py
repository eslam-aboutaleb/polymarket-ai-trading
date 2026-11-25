"""Synthetic funded CTF wallet and JSON-RPC receipts; no real wallet or network."""

import json
from types import SimpleNamespace

import httpx
from chain_attribution_fixtures import VenueRPC, block, cash, log, shares
from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak
from hexbytes import HexBytes

from app.services.analytics.chain_evidence import CTF, ChainReader, topic_address
from app.services.analytics.chain_redemptions import PAYOUT, ZERO
from app.services.trading.redemption_chain import (
    CTF_ADAPTER,
    UNDERLYING,
    NativeRedemption,
    calldata,
)

TEST_KEY = bytes(range(1, 33))
TEST_WALLET = Account.from_key(TEST_KEY).address.lower()
CONDITION = "0x" + "ab" * 32


class FundedRedemptionRPC(VenueRPC):
    def __init__(self):
        super().__init__()
        self.final_number = 10
        self.denominator = 1
        self.numerators = [1, 0]
        self.shares = 10
        self.approved = True
        self.receipt = None
        self.receipt_fault = None
        self.redeemed = False
        self.tx_hash = None
        self.nonce_reads = 0
        self.finalized_nonce_count = 7
        self.signals = []
        self.broadcasts = []
        self.fail_broadcast = False
        self.on_broadcast = None
        self.logs = []
        self.eth = SimpleNamespace(
            chain_id=137,
            gas_price=10**9,
            get_transaction_count=self.nonce,
            estimate_gas=lambda tx: 100000,
            get_balance=lambda wallet: 10**18,
            send_raw_transaction=self.broadcast,
            get_transaction=self.transaction,
        )

    def nonce(self, wallet, mode):
        assert mode == "pending"
        self.nonce_reads += 1
        return 7

    def native(self):
        return NativeRedemption(
            reader=ChainReader(transport=httpx.MockTransport(self.provider)), w3=self
        )

    def provider(self, request):
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        response = None
        if method == "eth_getBlockByNumber" and params[0] == "finalized":
            response = block(self.final_number)
        elif method == "eth_getTransactionReceipt":
            response = self.receipt
        elif method == "eth_getTransactionCount":
            response = hex(self.finalized_nonce_count)
        elif method == "eth_call":
            selector = params[0]["data"][2:10]
            if selector == keccak(text="payoutDenominator(bytes32)")[:4].hex():
                response = "0x" + encode(["uint256"], [self.denominator]).hex()
            elif selector == keccak(text="payoutNumerators(bytes32,uint256)")[:4].hex():
                _, index = decode(["bytes32", "uint256"], bytes.fromhex(params[0]["data"][10:]))
                response = "0x" + encode(["uint256"], [self.numerators[index]]).hex()
            elif selector == keccak(text="isApprovedForAll(address,address)")[:4].hex():
                response = "0x" + encode(["bool"], [self.approved]).hex()
            elif selector == keccak(text="balanceOf(address,uint256)")[:4].hex():
                _, token = decode(["address", "uint256"], bytes.fromhex(params[0]["data"][10:]))
                quantity = self.shares * 1000000 if token == 11 and not self.redeemed else 0
                response = "0x" + encode(["uint256"], [quantity]).hex()
        if response is not None or method == "eth_getTransactionReceipt":
            self.requests.append(body)
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "result": response}
            )
        return super().provider(request)

    def broadcast(self, raw):
        self.tx_hash = "0x" + keccak(raw).hex()
        if self.on_broadcast:
            self.on_broadcast(self.tx_hash)
        self.broadcasts.append(raw)
        adapter = CTF_ADAPTER
        entries = [
            shares(TEST_WALLET, adapter, str(self.shares), 18, self.tx_hash, 0),
            shares(adapter, ZERO, str(self.shares), 18, self.tx_hash, 1),
            log(
                CTF,
                [PAYOUT, topic_address(adapter), topic_address(UNDERLYING), "0x" + "0" * 64],
                encode(
                    ["bytes32", "uint256[]", "uint256"],
                    [bytes.fromhex(CONDITION[2:]), [1, 2], self.shares * 1000000],
                ),
                18,
                self.tx_hash,
                2,
            ),
            cash(ZERO, TEST_WALLET, str(self.shares), 18, self.tx_hash, 3),
        ]
        for entry in entries[:2]:
            entry["topics"][1] = topic_address(adapter)
        self.receipt = {
            "transactionHash": self.tx_hash,
            "blockNumber": "0x12",
            "blockHash": block(18)["hash"],
            "transactionIndex": "0x0",
            "status": "0x1",
            "logs": entries,
            "gasUsed": hex(100000),
            "effectiveGasPrice": hex(10**9),
        }
        self.logs = entries
        if self.fail_broadcast:
            raise TimeoutError("synthetic timeout after node accepted transaction")
        return HexBytes(self.tx_hash)

    def transaction(self, tx_hash):
        return {
            "hash": HexBytes(tx_hash),
            "from": TEST_WALLET,
            "to": CTF_ADAPTER,
            "nonce": 7,
            "value": 0,
            "input": HexBytes(calldata(CONDITION)),
            "chainId": 137,
        }
