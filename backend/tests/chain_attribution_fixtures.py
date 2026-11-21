"""Synthetic JSON-RPC boundary fixtures; never evidence of actual funded performance."""

import json
from datetime import UTC, timedelta
from decimal import Decimal

import httpx
from eth_abi import decode, encode
from eth_utils import keccak
from test_live_account_reconciliation import T0

from app.services.analytics.chain_evidence import (
    COLLATERAL,
    CTF,
    EXCHANGES,
    ORDER_FILLED,
    SINGLE,
    TRANSFER,
    topic_address,
)

WALLET = "0x" + "1" * 40
OTHER = "0x" + "2" * 40
EXCHANGE = sorted(EXCHANGES)[0]
BUY_ORDER, SELL_ORDER = "0x" + "b" * 64, "0x" + "c" * 64
BUY_TX, SELL_TX = "0x" + "a" * 64, "0x" + "d" * 64


def block(number):
    return {
        "number": hex(number),
        "hash": "0x" + f"{number:064x}",
        "timestamp": hex(int(T0.timestamp()) + number - 10),
    }


def log(contract, topics, data, number, tx, index):
    return {
        "address": contract,
        "topics": topics,
        "data": "0x" + data.hex(),
        "blockNumber": hex(number),
        "blockHash": block(number)["hash"],
        "transactionHash": tx,
        "transactionIndex": "0x0",
        "logIndex": hex(index),
        "removed": False,
    }


def cash(sender, recipient, amount, number, tx, index):
    return log(
        COLLATERAL,
        [TRANSFER, topic_address(sender), topic_address(recipient)],
        encode(["uint256"], [int(Decimal(amount) * 1000000)]),
        number,
        tx,
        index,
    )


def shares(sender, recipient, amount, number, tx, index):
    return log(
        CTF,
        [SINGLE, topic_address(OTHER), topic_address(sender), topic_address(recipient)],
        encode(["uint256", "uint256"], [11, int(Decimal(amount) * 1000000)]),
        number,
        tx,
        index,
    )


def filled(side, order, principal, fee, number, tx, index):
    quantity = 10000000
    making, taking = (
        (int(Decimal(principal) * 1000000), quantity)
        if side == 0
        else (quantity, int(Decimal(principal) * 1000000))
    )
    return log(
        EXCHANGE,
        [ORDER_FILLED, order, topic_address(WALLET), topic_address(EXCHANGE)],
        encode(
            ["uint8", "uint256", "uint256", "uint256", "uint256", "bytes32", "bytes32"],
            [side, 11, making, taking, int(Decimal(fee) * 1000000), bytes(32), bytes(32)],
        ),
        number,
        tx,
        index,
    )


class VenueRPC:
    def __init__(self):
        self.requests = []
        self.chain_id = "0x89"
        self.reorg = False
        self.closing_cash = 101700000
        self.yes_wins = True
        self.logs = [
            cash(WALLET, EXCHANGE, "4", 12, BUY_TX, 0),
            cash(WALLET, OTHER, ".1", 12, BUY_TX, 1),
            shares(OTHER, WALLET, "10", 12, BUY_TX, 2),
            filled(0, BUY_ORDER, "4", ".1", 12, BUY_TX, 3),
            cash(EXCHANGE, WALLET, "5.8", 18, SELL_TX, 0),
            shares(WALLET, OTHER, "10", 18, SELL_TX, 1),
            filled(1, SELL_ORDER, "6", ".2", 18, SELL_TX, 2),
        ]

    def provider(self, request):
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.requests.append(body)
        if method == "eth_chainId":
            result = self.chain_id
        elif method == "eth_getBlockByNumber":
            result = block(30 if params[0] == "finalized" else int(params[0], 16))
            if self.reorg and params[0] == "0xc":
                result["hash"] = "0x" + "f" * 64
        elif method == "eth_call":
            assert params[1]["requireCanonical"] is True
            number = int(params[1]["blockHash"], 16)
            selector = params[0]["data"][2:10]

            def sig(value):
                return keccak(text=value)[:4].hex()

            if selector == sig("decimals()"):
                result = "0x" + encode(["uint8"], [6]).hex()
            elif selector in {sig("getCollateral()"), sig("getCtf()")}:
                result = (
                    "0x"
                    + encode(
                        ["address"], [COLLATERAL if selector == sig("getCollateral()") else CTF]
                    ).hex()
                )
            elif selector == sig("getOutcomeSlotCount(bytes32)"):
                result = "0x" + encode(["uint256"], [2]).hex()
            elif selector == sig("payoutDenominator(bytes32)"):
                result = "0x" + encode(["uint256"], [1]).hex()
            elif selector == sig("payoutNumerators(bytes32,uint256)"):
                _, index = decode(["bytes32", "uint256"], bytes.fromhex(params[0]["data"][10:]))
                amount = int((index == 0) == self.yes_wins)
                result = "0x" + encode(["uint256"], [amount]).hex()
            elif selector == sig("getCollectionId(bytes32,bytes32,uint256)"):
                _, _, index = decode(
                    ["bytes32", "bytes32", "uint256"], bytes.fromhex(params[0]["data"][10:])
                )
                result = "0x" + encode(["bytes32"], [index.to_bytes(32, "big")]).hex()
            elif selector == sig("getPositionId(address,bytes32)"):
                _, collection = decode(
                    ["address", "bytes32"], bytes.fromhex(params[0]["data"][10:])
                )
                result = (
                    "0x"
                    + encode(
                        ["uint256"], [11 if int.from_bytes(collection, "big") == 1 else 22]
                    ).hex()
                )
            else:
                amount = (
                    (100000000 if number <= 10 else self.closing_cash)
                    if params[0]["to"].lower() == COLLATERAL
                    else 0
                )
                result = "0x" + encode(["uint256"], [amount]).hex()
        elif method == "eth_getTransactionReceipt":
            entries = [row for row in self.logs if row["transactionHash"] == params[0]]
            number = int(entries[0]["blockNumber"], 16)
            result = {
                "transactionHash": params[0],
                "blockNumber": hex(number),
                "blockHash": block(number)["hash"],
                "transactionIndex": "0x0",
                "status": "0x1",
                "logs": entries,
            }
        elif method == "eth_getLogs":
            query = params[0]

            def matches(row):
                if row["address"].lower() != query["address"].lower():
                    return False
                if (
                    not int(query["fromBlock"], 16)
                    <= int(row["blockNumber"], 16)
                    <= int(query["toBlock"], 16)
                ):
                    return False
                return all(
                    wanted is None or row["topics"][i] in wanted
                    if isinstance(wanted, list)
                    else wanted is None or row["topics"][i] == wanted
                    for i, wanted in enumerate(query["topics"])
                )

            result = [row for row in self.logs if matches(row)]
        else:
            raise AssertionError("mutation or unsupported RPC: " + method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def transport(self):
        return httpx.MockTransport(self.provider)


def observation(number, cash_amount):
    return {
        "observed_at": __import__("datetime")
        .datetime.fromtimestamp(int(block(number)["timestamp"], 16), UTC)
        .isoformat(),
        "event_watermark": 0,
        "cash_usdc": cash_amount,
        "positions": {"11": "0"},
        "chain_snapshot": {
            "chain_id": 137,
            "wallet": WALLET,
            "collateral": COLLATERAL,
            "token_contract": CTF,
            "block": block(number),
            "cash_usdc": cash_amount,
            "positions": {"11": "0"},
        },
    }


def gateway_fills():
    rows = []
    for source_id, side, order, tx, principal, timestamp in [
        (1, "BUY", BUY_ORDER, BUY_TX, "4", 1),
        (2, "SELL", SELL_ORDER, SELL_TX, "6", 7),
    ]:
        rows.append(
            {
                "source_id": source_id,
                "intent_source_id": source_id,
                "order_hash": order,
                "transaction_hash": tx,
                "fill_id": str(source_id),
                "token_id": "11",
                "condition_id": "0x" + "ab" * 32,
                "strategy": "manual",
                "side": side,
                "shares": "10",
                "notional": principal,
                "matched_at": (T0 + timedelta(seconds=timestamp)).isoformat(),
                "decision_at": T0.isoformat(),
            }
        )
    return rows


def seed_rpc_account(db, rpc, *, reset=False, activity=None):
    """Synthetic account evidence shared by API/SQL and browser tests."""
    from datetime import timedelta

    from paper_account_fixtures import evidence
    from test_attribution_acceptance import confirmed

    from app.models import AppEvent, LiveOrder, UserTrade
    from app.models.analytics.live_balance_snapshot import (
        LiveAttributionReport,
        LiveBalanceSnapshot,
    )
    from app.services.analytics.live_account_reconciliation import canonical, digest

    if reset:
        for model in (LiveAttributionReport, LiveBalanceSnapshot, AppEvent, LiveOrder, UserTrade):
            db.query(model).delete(synchronize_session=False)
        db.commit()
    confirmed(
        db,
        "b",
        "BUY",
        decision=T0 + timedelta(milliseconds=100),
        matched=T0 + timedelta(seconds=1),
        fee=None,
    )
    has_sell = any(
        row["address"] == EXCHANGE
        and row["topics"][0] == ORDER_FILLED
        and row["topics"][1] == SELL_ORDER
        for row in rpc.logs
    )
    if has_sell:
        confirmed(
            db,
            "s",
            "SELL",
            decision=T0 + timedelta(seconds=6),
            matched=T0 + timedelta(seconds=7),
            notional="6",
            fee=None,
        )
    for old, order_hash, tx in (("b", BUY_ORDER, BUY_TX), ("s", SELL_ORDER, SELL_TX)):
        order = db.query(LiveOrder).filter_by(order_hash=old).first()
        if order is None:
            continue
        order.order_hash = order_hash
        for event in db.query(AppEvent).filter_by(aggregate_id=old):
            event.aggregate_id = order_hash
            if event.event_type == "order_fill_confirmed":
                payload = json.loads(event.payload_json)
                payload["transaction_hash"] = tx
                event.payload_json = json.dumps(payload)
    db.commit()
    watermark = max(row.id for row in db.query(AppEvent).all())
    evidence(db, [T0], bid=".39", ask=".41")
    identities = []
    for number, amount, mark, at in (
        (10, "100", 0, T0 + timedelta(milliseconds=10)),
        (20, str(Decimal(rpc.closing_cash) / 1000000), watermark, T0 + timedelta(seconds=11)),
    ):
        source = observation(number, amount)
        value = {
            "user_id": 1,
            "account_id": 11,
            "wallet_identity": "synthetic-wallet",
            "observed_at": at.isoformat(),
            "cash_usdc": amount,
            "positions": {"11": "0"},
            "coverage": "venue_indexed_wallet_tokens",
            "event_watermark": mark,
            "active_or_unsettled_orders": False,
            "chain_snapshot": source["chain_snapshot"],
        }
        if number == 20 and activity is not None:
            value["wallet_evidence"] = {"activity": activity, "receipts": []}
        identity = digest(value)
        db.add(
            LiveBalanceSnapshot(
                id=identity,
                user_id=1,
                account_id=11,
                observed_at=at,
                captured_at=at,
                evidence_json=canonical(value),
            )
        )
        identities.append(identity)
    db.commit()
    return identities
