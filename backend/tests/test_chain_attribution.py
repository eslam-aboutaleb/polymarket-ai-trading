"""Verify actual-cost provenance and canonical balance identities at the RPC boundary."""

from decimal import Decimal

import pytest
from chain_attribution_fixtures import BUY_ORDER, BUY_TX, VenueRPC, gateway_fills, observation
from test_live_account_reconciliation import account_db as _account_db

from app.services.analytics.chain_attribution import verify_window
from app.services.analytics.chain_evidence import ChainReader, confirmed_order_cost
from app.services.analytics.pnl_reconciliation import reconcile_confirmed_cashflows

account_db = _account_db


def test_finalized_chain_costs_cash_positions_and_fifo_reconcile_without_fee_estimates():
    rpc = VenueRPC()
    result = verify_window(
        observation(10, "100"), observation(20, "101.7"), gateway_fills(), transport=rpc.transport()
    )
    assert result["scope_reconciled"] and result["balance_invariant_verified"]
    assert not result["full_wallet_inventory_verified"]
    assert [row["fee_usdc"] for row in result["fills"]] == ["0.1", "0.2"]
    assert all(row["chain_cost_evidence"]["receipt"]["status"] == "0x1" for row in result["fills"])
    accounting = reconcile_confirmed_cashflows(
        result["fills"], result["opening"], result["closing"], opening_lots={}
    )
    assert accounting["status"] == "reconciled"
    assert (
        accounting["net_realized_pnl"] == Decimal("1.7") and accounting["cash_residual_usdc"] == 0
    )
    assert sum(row["net_realized"] for row in accounting["strategies"].values()) == Decimal("1.7")
    assert {request["method"] for request in rpc.requests} <= {
        "eth_chainId",
        "eth_call",
        "eth_getLogs",
        "eth_getBlockByNumber",
        "eth_getTransactionReceipt",
    }


@pytest.mark.parametrize(
    "fault",
    [
        "reorg",
        "wrong-chain",
        "missing-transfer",
        "wrong-wallet",
        "wrong-order-size",
        "wrong-exchange",
        "duplicate-log",
    ],
)
def test_ambiguous_or_noncanonical_chain_evidence_cannot_verify_earnings(fault):
    rpc, rows = VenueRPC(), gateway_fills()
    if fault == "reorg":
        rpc.reorg = True
    elif fault == "wrong-chain":
        rpc.chain_id = "0x1"
    elif fault == "missing-transfer":
        rpc.logs.pop(1)
    elif fault == "wrong-wallet":
        rpc.logs[3]["topics"][2] = "0x" + "0" * 64
    elif fault == "wrong-order-size":
        rows[0]["shares"] = "11"
    elif fault == "wrong-exchange":
        rpc.logs[3]["address"] = "0x" + "3" * 40
    else:
        rpc.logs.append(rpc.logs[3].copy())
    with pytest.raises(ValueError):
        verify_window(
            observation(10, "100"), observation(20, "101.7"), rows, transport=rpc.transport()
        )


def test_actual_zero_fee_requires_a_matching_receipt_not_a_missing_field():
    rpc = VenueRPC()
    reader = ChainReader(transport=rpc.transport())
    try:
        from chain_attribution_fixtures import filled

        rpc.logs[3] = filled(0, BUY_ORDER, "4", "0", 12, BUY_TX, 3)
        proof = confirmed_order_cost(
            reader,
            wallet="0x" + "1" * 40,
            order_hash=BUY_ORDER,
            token="11",
            side="BUY",
            shares="10",
            notional="4",
            tx=BUY_TX,
            final_block=reader.finalized(),
        )
        assert proof["fee_usdc"] == "0"
    finally:
        reader.close()


@pytest.mark.parametrize("wins", [True, False])
def test_finalized_redemption_requires_burn_payout_vector_and_actual_credit(wins):
    from chain_attribution_fixtures import OTHER, SELL_TX, WALLET, cash, log, shares
    from eth_abi import encode

    from app.services.analytics.chain_evidence import CTF, topic_address
    from app.services.analytics.chain_redemptions import ADAPTERS, PAYOUT, ZERO

    rpc = VenueRPC()
    rpc.yes_wins = wins
    payout = 10 if wins else 0
    rpc.closing_cash = 95900000 + payout * 1000000
    adapter = sorted(ADAPTERS)[0]
    rpc.logs = rpc.logs[:4] + [
        shares(WALLET, adapter, "10", 18, SELL_TX, 0),
        shares(adapter, ZERO, "10", 18, SELL_TX, 1),
        log(
            CTF,
            [PAYOUT, topic_address(adapter), topic_address(OTHER), "0x" + "0" * 64],
            encode(
                ["bytes32", "uint256[]", "uint256"],
                [bytes.fromhex("ab" * 32), [1, 2], payout * 1000000],
            ),
            18,
            SELL_TX,
            2,
        ),
    ]
    rpc.logs[4]["topics"][1] = topic_address(adapter)
    rpc.logs[5]["topics"][1] = topic_address(adapter)
    if wins:
        rpc.logs.append(cash(ZERO, WALLET, str(payout), 18, SELL_TX, 3))
    result = verify_window(
        observation(10, "100"),
        observation(20, str(Decimal(rpc.closing_cash) / 1000000)),
        gateway_fills()[:1],
        transport=rpc.transport(),
    )
    assert result["scope_reconciled"] and len(result["redemption_proofs"]) == 1
    settlement = result["fills"][-1]
    assert settlement["kind"] == "SETTLEMENT" and Decimal(settlement["notional"]) == payout
    report = reconcile_confirmed_cashflows(
        result["fills"], result["opening"], result["closing"], opening_lots={}
    )
    assert report["net_realized_pnl"] == Decimal(payout) - Decimal("4.1")
    assert report["cash_residual_usdc"] == 0 and report["position_residuals"]["11"] == 0
    # A subsequent finalized window carries the redemption result without
    # replaying the old entry or minting its realized P&L a second time.
    before = len(rpc.requests)
    continued = verify_window(
        observation(20, str(Decimal(rpc.closing_cash) / 1000000)),
        observation(30, str(Decimal(rpc.closing_cash) / 1000000)),
        gateway_fills()[:1],
        transport=rpc.transport(),
        prior_state=result,
    )
    assert continued["scope_reconciled"] and continued["fills"] == []
    assert all(row["method"] != "eth_getTransactionReceipt" for row in rpc.requests[before:])
    next_report = reconcile_confirmed_cashflows(
        continued["fills"],
        continued["opening"],
        continued["closing"],
        opening_lots=report["closing_lots"],
    )
    assert next_report["net_realized_pnl"] == next_report["cash_residual_usdc"] == 0
    if wins:
        # An indexer-like claim cannot substitute for a missing collateral credit.
        rpc.logs.pop()
        rpc.closing_cash = 95900000
        with pytest.raises(ValueError, match="COLLATERAL_CREDIT_MISMATCH"):
            verify_window(
                observation(10, "100"),
                observation(20, "95.9"),
                gateway_fills()[:1],
                transport=rpc.transport(),
            )


def test_rpc_transport_cannot_call_transaction_mutation_methods():
    rpc = VenueRPC()
    reader = ChainReader(transport=rpc.transport())
    try:
        for method in ("eth_sendRawTransaction", "eth_sendTransaction", "personal_sign"):
            with pytest.raises(ValueError, match="FORBIDDEN"):
                reader.call(method, [])
        assert rpc.requests == []
    finally:
        reader.close()


@pytest.mark.parametrize("kind", ["DEPOSIT", "MAKER_REBATE", None])
def test_external_cash_and_confirmed_rebates_are_not_forced_into_strategy_profit(account_db, kind):
    from chain_attribution_fixtures import OTHER, WALLET, cash, seed_rpc_account

    from app.models import Account
    from app.services.analytics.live_account_reconciliation import build_reconciliation_report

    rpc = VenueRPC()
    tx = "0x" + "e" * 64
    rpc.logs.append(cash(OTHER, WALLET, "10", 15, tx, 0))
    rpc.closing_cash += 10000000
    activity = [{"type": kind, "transaction_hash": tx, "usdc_size": "10"}] if kind else []
    opening, closing = seed_rpc_account(account_db, rpc, activity=activity)
    result = build_reconciliation_report(
        account_db,
        account_db.get(Account, 11),
        opening,
        closing,
        verify_chain=True,
        chain_transport=rpc.transport(),
    )
    if kind is None:
        assert result["unattributed_external_cashflow"] == "10"
        assert not result["chain_reconciliation"]["scope_reconciled"]
        assert result["verified_net_realized_pnl"] is None
    else:
        assert result["cash_residual_usdc"] == "0.0"
        assert Decimal(result["net_realized_pnl"]) == Decimal(
            "11.7" if kind == "MAKER_REBATE" else "1.7"
        )
        assert sum(
            Decimal(row["net_realized"]) for row in result["strategies"].values()
        ) == Decimal(result["net_realized_pnl"])
        if kind == "MAKER_REBATE":
            assert Decimal(result["confirmed_rebates_usdc"]) == 10
            assert Decimal(result["strategies"]["unattributed_rebates"]["net_realized"]) == 10
            assert result["estimated_rebates_usdc"] == "0"
