"""Independent settlement accounting, receipt identity and read-side regressions."""

import asyncio
import copy
import json
from datetime import timedelta
from decimal import Decimal, localcontext

import httpx
import pytest
from eth_abi import encode
from pydantic import ValidationError
from test_settlement_lifecycle import _now

from app.services.analytics.settlement_lifecycle import (
    RedemptionAttemptView,
    SettlementAnalysisRequest,
    analyze_settlement_lifecycle,
)
from app.services.analytics.settlement_receipts import (
    COLLATERAL,
    CTF,
    PAYOUT_TOPIC,
    read_redemption_receipt,
    verify_redemption_receipt,
)

TX = "0x" + "11" * 32
BLOCK = "0x" + "22" * 32
CID = "0x" + "33" * 32
WALLET = "0x" + "44" * 20


def receipt_fixture():
    receipt = {
        "transactionHash": TX,
        "blockHash": BLOCK,
        "blockNumber": "0x64",
        "status": "0x1",
        "gasUsed": "0x5208",
        "effectiveGasPrice": "0x3b9aca00",
        "logs": [
            {
                "address": CTF,
                "topics": [
                    PAYOUT_TOPIC,
                    "0x" + WALLET[2:].zfill(64),
                    "0x" + COLLATERAL[2:].zfill(64),
                    "0x" + "00" * 32,
                ],
                "data": "0x"
                + encode(
                    ["bytes32", "uint256[]", "uint256"], [bytes.fromhex(CID[2:]), [1], 40_000_000]
                ).hex(),
                "transactionHash": TX,
                "blockHash": BLOCK,
                "logIndex": "0x1",
            }
        ],
    }
    return receipt, {"hash": BLOCK, "number": "0x64"}, 300


def request(**kwargs):
    return SettlementAnalysisRequest(
        market_id=CID,
        state="TX_CONFIRMED",
        position_shares="100",
        entry_cost_usdc="60",
        locked_collateral_usdc="60",
        starting_available_cash_usdc="200",
        now=_now(),
        **kwargs,
    )


def claim(identity, shares, **kwargs):
    return RedemptionAttemptView(
        attempt_id=str(identity),
        dry_run=False,
        tx_hash="0x" + str(identity).zfill(64),
        chain_receipt_status="confirmed",
        confirmations=128,
        redeemed_shares=str(shares),
        payout_usdc=str(shares),
        **kwargs,
    )


def test_partial_claims_release_only_proportional_cost_and_actual_cash():
    result = analyze_settlement_lifecycle(
        request(redemption_attempts=[claim(1, 40, gas_fee_usdc="0.25")])
    )
    assert result.status == "ok"
    assert result.available_cash_usdc == Decimal("239.75")
    assert result.realized_pnl_usdc == Decimal("15.75")
    assert result.locked_collateral_usdc == Decimal("36")
    assert result.unrealized_payout_usdc == Decimal("60")
    complete = analyze_settlement_lifecycle(
        request(redemption_attempts=[claim(1, 40), claim(2, 60)])
    )
    assert complete.status == "ok"
    assert complete.available_cash_usdc == 300
    assert complete.locked_collateral_usdc == 0
    assert complete.realized_pnl_usdc == 40
    assert complete.provenance_class == "declared_hypothesis"
    assert complete.source_verified is False


def test_dispute_window_and_future_receipts_never_release_cash():
    for args in [
        {"dispute_window_ends_at": _now() + timedelta(seconds=1)},
        {},
    ]:
        attempts = (
            [claim(1, 100, confirmed_at=_now() + timedelta(seconds=1))]
            if not args
            else [claim(1, 100)]
        )
        report = analyze_settlement_lifecycle(request(redemption_attempts=attempts, **args))
        assert report.status == "abstain"
        assert report.available_cash_usdc == 200
        assert report.released_collateral_usdc == 0
        assert report.realized_pnl_usdc == 0


def test_decimal_context_cannot_change_partial_accounting():
    req = request(redemption_attempts=[claim(1, 33)])
    with localcontext() as ctx:
        ctx.prec = 6
        low = analyze_settlement_lifecycle(req).model_dump()
        ctx.prec = 80
        high = analyze_settlement_lifecycle(req).model_dump()
    assert low == high


@pytest.mark.parametrize(
    "updates",
    [
        {"position_shares": "NaN"},
        {"position_shares": "1e40"},
        {"now": "2026-11-13T12:00:00"},
        {"expected_payout_per_share": "1.1"},
        {"expected_lockup_days": "10", "stressed_lockup_days": "2"},
    ],
)
def test_invalid_or_naive_inputs_rejected(updates):
    data = request().model_dump()
    data.update(updates)
    with pytest.raises(ValidationError):
        SettlementAnalysisRequest(**data)


def verify(receipt, block, head):
    return verify_redemption_receipt(
        receipt, block, head, tx_hash=TX, condition_id=CID, wallet=WALLET
    )


def test_receipt_observation_has_exact_payout_native_fee_and_source_hash():
    proof = verify(*receipt_fixture())
    assert proof["payout_usdc"] == "40"
    assert proof["gas_fee_native"] == "0.000021"
    assert len(proof["source_sha256"]) == 64


@pytest.mark.parametrize(
    "case",
    [
        "wrong_tx",
        "reorg",
        "revert",
        "depth",
        "wrong_condition",
        "wrong_wallet",
        "wrong_collateral",
        "removed",
        "duplicate_log",
        "forged_contract",
        "empty_block_hash",
    ],
)
def test_receipt_never_accepts_wrong_identity_or_nonfinal_cash(case):
    receipt, block, head = copy.deepcopy(receipt_fixture())
    log = receipt["logs"][0]
    if case == "wrong_tx":
        receipt["transactionHash"] = BLOCK
    if case == "reorg":
        block["hash"] = TX
    if case == "revert":
        receipt["status"] = "0x0"
    if case == "depth":
        head = 110
    if case == "wrong_condition":
        log["data"] = (
            "0x"
            + encode(
                ["bytes32", "uint256[]", "uint256"], [bytes.fromhex(TX[2:]), [1], 40_000_000]
            ).hex()
        )
    if case == "wrong_wallet":
        log["topics"][1] = TX
    if case == "wrong_collateral":
        log["topics"][2] = TX
    if case == "removed":
        log["removed"] = True
    if case == "duplicate_log":
        receipt["logs"].append(copy.deepcopy(log))
    if case == "forged_contract":
        log["address"] = WALLET
    if case == "empty_block_hash":
        receipt["blockHash"] = block["hash"] = ""
    with pytest.raises(ValueError):
        verify(receipt, block, head)


def test_real_http_transport_rpc_queries_are_fixed_and_read_only():
    asyncio.run(_http_transport_case())


async def _http_transport_case():
    receipt, block, head = receipt_fixture()
    calls = []

    def handler(req):
        assert str(req.url) == "https://polygon.drpc.org"
        body = json.loads(req.content)
        calls.append(body["method"])
        result = {
            "eth_chainId": "0x89",
            "eth_getTransactionReceipt": receipt,
            "eth_getBlockByNumber": block,
            "eth_blockNumber": hex(head),
        }[body["method"]]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await read_redemption_receipt(client, tx_hash=TX, condition_id=CID, wallet=WALLET)
    assert result["payout_usdc"] == "40"
    assert calls == [
        "eth_chainId",
        "eth_getTransactionReceipt",
        "eth_getBlockByNumber",
        "eth_blockNumber",
    ]


def test_schema_and_shadow_report_reproduce_without_network():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).parents[2]
    schema = json.loads(
        (root / "services/mcps/research/schemas/settlement-request.json").read_text()
    )
    assert schema == SettlementAnalysisRequest.model_json_schema()
    spec = importlib.util.spec_from_file_location(
        "settlement_shadow", root / "scripts/settlement-shadow.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = json.loads(
        (root / "user guide/skills/settlement-and-redemption/shadow-baseline.json").read_text()
    )
    with localcontext() as ctx:
        ctx.prec = 80
        assert module.report() == expected
    assert expected["sample_count"] > 0
    assert all(
        row["verified_receipt_required_cash_release_usdc"] == "0"
        for row in expected["observations"]
    )


def test_partial_claim_balance_matches_remaining_shares_and_split_wallets():
    result = analyze_settlement_lifecycle(
        request(
            redemption_attempts=[claim(1, 40)], proxy_token_balance="20", eoa_token_balance="40"
        )
    )
    assert result.status == "ok"
    assert result.locked_collateral_usdc == 36


def test_wrong_chain_and_rpc_error_fail_closed():
    async def run():
        for payload in [
            {"jsonrpc": "2.0", "id": 1, "result": "0x1"},
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -1}},
        ]:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _, value=payload: httpx.Response(200, json=value)
                )
            ) as client:
                with pytest.raises(ValueError):
                    await read_redemption_receipt(
                        client, tx_hash=TX, condition_id=CID, wallet=WALLET
                    )

    asyncio.run(run())
