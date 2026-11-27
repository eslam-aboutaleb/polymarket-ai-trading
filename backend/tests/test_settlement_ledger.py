"""Persisted transitions, exact accounting reconciliation and audit (#24).

The database is real (SQLite here, PostgreSQL in the integration module). Chain
JSON-RPC uses a deterministic in-process transport that answers like a node;
no balance, receipt or payout is stubbed at a Python function boundary.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from eth_abi import encode
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DatabaseError
from test_settlement_acceptance import BLOCK, TX, WALLET
from test_valuation_recorded_evidence import valuation_db as _valuation_db

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.portfolio.settlement import router
from app.config import get_settings
from app.models.analytics.live_balance_snapshot import LiveBalanceSnapshot
from app.models.analytics.settlement_transition import SettlementTransition
from app.models.platform.app_event import AppEvent
from app.models.trading.live_order import LiveOrder
from app.models.trading.redemption_attempt import RedemptionAttempt
from app.models.trading.user_trade import UserTrade
from app.models.users.account import Account
from app.models.users.user import User
from app.services.analytics import settlement_ledger as ledger
from app.services.analytics.live_account_reconciliation import canonical, digest
from app.services.analytics.settlement_accounting import reconcile_accounting
from app.services.analytics.settlement_lifecycle import SettlementState as S
from app.services.analytics.settlement_receipts import (
    COLLATERAL,
    CTF,
    PAYOUT_DENOMINATOR,
    PAYOUT_NUMERATORS,
    PAYOUT_TOPIC,
    SLOT_COUNT,
)
from app.utils.database import get_db

valuation_db = _valuation_db
REAL_CLIENT = httpx.AsyncClient
BLOCK_TIME = 1_000


def receipt_for(condition, payout_micro, *, index_sets=(1,), status="0x1"):
    receipt = {
        "transactionHash": TX,
        "blockHash": BLOCK,
        "blockNumber": "0x64",
        "status": status,
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
                    ["bytes32", "uint256[]", "uint256"],
                    [bytes.fromhex(condition[2:]), list(index_sets), payout_micro],
                ).hex(),
                "transactionHash": TX,
                "blockHash": BLOCK,
                "logIndex": "0x1",
            }
        ],
    }
    return receipt, {"hash": BLOCK, "number": "0x64", "timestamp": hex(BLOCK_TIME)}, 300


def chain_node(condition, payout_micro, numerators=(1, 0), denominator=None, **receipt_kwargs):
    receipt, block, head = receipt_for(condition, payout_micro, **receipt_kwargs)
    denominator = sum(numerators) if denominator is None else denominator
    calls = []

    def handler(request):
        data = json.loads(request.content)
        method, params = data["method"], data.get("params", [])
        calls.append(method)
        if method == "eth_call":
            payload = params[0]["data"]
            selector, args = payload[2:10], payload[10:]
            if selector == SLOT_COUNT:
                result = 2
            elif selector == PAYOUT_DENOMINATOR:
                result = denominator
            elif selector == PAYOUT_NUMERATORS:
                result = numerators[int(args[64:], 16)]
            else:
                raise AssertionError(selector)
            return httpx.Response(
                200, json={"id": 1, "jsonrpc": "2.0", "result": "0x" + f"{result:064x}"}
            )
        result = {
            "eth_chainId": "0x89",
            "eth_getTransactionReceipt": receipt,
            "eth_getBlockByNumber": block,
            "eth_blockNumber": hex(head),
        }[method]
        return httpx.Response(200, json={"id": 1, "jsonrpc": "2.0", "result": result})

    return handler, calls


def build_app(db, monkeypatch, inp, handler):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return inp.as_of.astimezone(tz) if tz else inp.as_of

    monkeypatch.setattr("app.services.analytics.settlement_read_side.datetime", Clock)
    monkeypatch.setattr(get_settings(), "settlement_redemption_enabled", True)
    original = REAL_CLIENT
    monkeypatch.setattr(
        "app.services.analytics.settlement_read_side.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    app = FastAPI()
    app.include_router(router)

    def get():
        yield db

    app.dependency_overrides[get_db] = get
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    return app


def seed_position(db, inp, *, shares=100, price=0.55, fee=0.5, with_snapshot=True, cash="123.5"):
    """One recorded buy, one confirmed redemption attempt and a later wallet snapshot."""
    db.execute(delete(LiveOrder))
    db.execute(delete(UserTrade))
    db.add(
        UserTrade(
            id=1,
            user_id=1,
            account_id=1,
            market_id=inp.condition_id,
            token_id="11",
            action="buy",
            status="executed",
            amount=shares * price,
            price=price,
            filled_size=shares,
            filled_price=price,
            fee_paid=fee,
            created_at=inp.as_of - timedelta(seconds=10),
            executed_at=inp.as_of - timedelta(seconds=5),
            updated_at=inp.as_of - timedelta(seconds=1),
        )
    )
    db.add(
        RedemptionAttempt(
            id=1,
            user_id=1,
            account_id=1,
            market_id=inp.condition_id,
            collection_id="0x" + "00" * 32,
            status="confirmed",
            amount=shares,
            tx_hash=TX,
            created_at=inp.as_of,
        )
    )
    db.get(User, 1).wallet_address = WALLET
    if with_snapshot:
        add_snapshot(db, inp, cash=cash)
    db.commit()


def add_snapshot(db, inp, *, cash="123.5", positions=None, age=60, active=False):
    observed = inp.as_of - timedelta(seconds=age)
    evidence = {
        "schema_version": "live-attribution-v2",
        "user_id": 1,
        "account_id": 1,
        "observed_at": observed.isoformat(),
        "cash_usdc": cash,
        "positions": positions if positions is not None else {"11": "0"},
        "active_or_unsettled_orders": active,
        "event_watermark": 0,
    }
    db.add(
        LiveBalanceSnapshot(
            id=digest(evidence),
            user_id=1,
            account_id=1,
            observed_at=observed,
            captured_at=observed,
            evidence_json=canonical(evidence),
        )
    )


# ---------------------------------------------------------------- accounting


def test_reconciled_account_recognizes_exact_payout_pnl_and_cash(valuation_db, monkeypatch):
    db, _, inp = valuation_db
    seed_position(db, inp)
    handler, calls = chain_node(inp.condition_id, 100_000_000)
    app = build_app(db, monkeypatch, inp, handler)
    with TestClient(app) as client:
        data = client.get("/api/settlement/recorded").json()
    assert data["accounting_status"] == "reconciled", data["accounting_reasons"]
    assert data["accounting_abstain_reason"] is None
    # payout 100.000000 - cost basis (100 * 0.55 + 0.5 fee) = 44.5
    assert Decimal(data["realized_pnl_usdc"]) == Decimal("44.5")
    assert Decimal(data["available_cash_usdc"]) == Decimal("123.5")
    assert Decimal(data["locked_collateral_usdc"]) == 0
    assert data["realized_pnl_excludes_native_gas"] is True
    condition = data["conditions"][0]
    assert condition["status"] == "reconciled"
    assert Decimal(condition["gas_fee_native"]) == Decimal("0.000021")
    shadow = data["shadow_comparison"]
    assert Decimal(shadow["baseline_realized_pnl_usdc"]) == 0
    assert Decimal(shadow["combined_realized_pnl_usdc"]) == Decimal("44.5")
    assert data["execution_eligible"] is False


def test_losing_token_redeems_zero_and_recognizes_the_loss(valuation_db, monkeypatch):
    db, _, inp = valuation_db
    seed_position(db, inp)
    handler, _ = chain_node(inp.condition_id, 0, numerators=(0, 1))
    app = build_app(db, monkeypatch, inp, handler)
    with TestClient(app) as client:
        data = client.get("/api/settlement/recorded").json()
    assert data["accounting_status"] == "reconciled", data["accounting_reasons"]
    assert Decimal(data["realized_pnl_usdc"]) == Decimal("-55.5")


@pytest.mark.parametrize(
    ("scenario", "code"),
    [
        ("payout_disagrees", "REDEMPTION_PAYOUT_DISAGREES_WITH_RECORDED_INVENTORY"),
        ("not_final", "CONDITION_PAYOUT_NOT_FINAL_ON_CHAIN"),
        ("index_not_covered", "REDEEMED_INDEX_SETS_DO_NOT_COVER_RECORDED_INVENTORY"),
    ],
)
def test_condition_abstains_and_logs_every_discrepancy(valuation_db, monkeypatch, scenario, code):
    db, _, inp = valuation_db
    seed_position(db, inp)
    kwargs = {"payout_disagrees": {}, "not_final": {"numerators": (0, 0), "denominator": 0}}.get(
        scenario, {"index_sets": (2,)} if scenario == "index_not_covered" else {}
    )
    payout = 40_000_000 if scenario == "payout_disagrees" else 100_000_000
    handler, _ = chain_node(inp.condition_id, payout, **kwargs)
    app = build_app(db, monkeypatch, inp, handler)
    with TestClient(app) as client:
        data = client.get("/api/settlement/recorded").json()
    assert data["accounting_status"] == "abstain"
    assert data["realized_pnl_usdc"] is None
    assert data["available_cash_usdc"] is None
    assert any(code in reason for reason in data["accounting_reasons"]), data
    assert data["conditions"][0]["status"] == "abstain"


def test_cash_excludes_locked_collateral_and_requires_fresh_post_redemption_snapshot(
    valuation_db, monkeypatch
):
    db, _, inp = valuation_db
    seed_position(db, inp, with_snapshot=False)
    handler, _ = chain_node(inp.condition_id, 100_000_000)
    app = build_app(db, monkeypatch, inp, handler)
    with TestClient(app) as client:
        no_snapshot = client.get("/api/settlement/recorded").json()
        assert no_snapshot["available_cash_usdc"] is None
        assert "cash:NO_BALANCE_SNAPSHOT" in no_snapshot["accounting_reasons"]
        # Stale snapshot.
        add_snapshot(db, inp, age=3600)
        db.commit()
        stale = client.get("/api/settlement/recorded").json()
        assert "cash:STALE_BALANCE_SNAPSHOT" in stale["accounting_reasons"]
    # A snapshot taken before the redemption block cannot include proceeds.
    old = inp.as_of - timedelta(seconds=60)
    block_time = int((old + timedelta(seconds=10)).timestamp())
    receipt, block, head = receipt_for(inp.condition_id, 100_000_000)
    block["timestamp"] = hex(block_time)

    def predating(request):
        data = json.loads(request.content)
        if data["method"] == "eth_getBlockByNumber":
            return httpx.Response(200, json={"id": 1, "jsonrpc": "2.0", "result": block})
        return handler(request)

    app = build_app(db, monkeypatch, inp, predating)
    db.execute(delete(LiveBalanceSnapshot))
    add_snapshot(db, inp, age=60)
    db.commit()
    with TestClient(app) as client:
        early = client.get("/api/settlement/recorded").json()
    assert "cash:SNAPSHOT_PREDATES_REDEMPTION" in early["accounting_reasons"]
    assert early["available_cash_usdc"] is None


def test_wallet_snapshot_must_match_recorded_inventory(valuation_db, monkeypatch):
    db, _, inp = valuation_db
    seed_position(db, inp, with_snapshot=False)
    add_snapshot(db, inp, positions={"11": "0", "99": "5"})
    db.commit()
    handler, _ = chain_node(inp.condition_id, 100_000_000)
    app = build_app(db, monkeypatch, inp, handler)
    with TestClient(app) as client:
        data = client.get("/api/settlement/recorded").json()
    assert "cash:WALLET_INVENTORY_DISAGREES_WITH_RECORDS" in data["accounting_reasons"]
    assert data["available_cash_usdc"] is None


def test_open_order_reservation_reduces_cash_and_unsettled_stays_locked(valuation_db, monkeypatch):
    db, _, inp = valuation_db
    seed_position(db, inp)
    # A second, still-unredeemed condition stays locked and never becomes cash.
    other = "0x" + "cd" * 32
    result = reconcile_accounting(
        legs=[],
        proofs={},
        resolutions={},
        snapshot=None,
        reserved_open_orders_usdc=Decimal(0),
        token_index={},
        now=inp.as_of,
    )
    assert result.status == "abstain" and result.locked_collateral_usdc == 0
    from app.services.analytics.event_correlation_risk import ExposureLeg

    leg = ExposureLeg(
        condition_id=other,
        token_id="77",
        outcome="YES",
        shares=Decimal(10),
        entry_price=Decimal("0.4"),
        fee_per_share=Decimal("0.01"),
    )
    snap = {
        "observed_at": (inp.as_of - timedelta(seconds=30)).isoformat(),
        "cash_usdc": "50",
        "positions": {"77": "10"},
        "active_or_unsettled_orders": False,
    }
    done = reconcile_accounting(
        legs=[leg],
        proofs={},
        resolutions={},
        snapshot=snap,
        reserved_open_orders_usdc=Decimal("7.25"),
        token_index={"77": 0},
        now=inp.as_of,
    )
    assert done.locked_collateral_usdc == Decimal("4.100000")
    assert done.available_cash_usdc == Decimal("42.750000")
    over = reconcile_accounting(
        legs=[leg],
        proofs={},
        resolutions={},
        snapshot=snap,
        reserved_open_orders_usdc=Decimal("51"),
        token_index={"77": 0},
        now=inp.as_of,
    )
    assert over.available_cash_usdc is None
    assert over.cash_reasons == ["RESERVATIONS_EXCEED_OBSERVED_CASH"]


def test_duplicate_and_partial_redemptions_need_manual_review(valuation_db, monkeypatch):
    db, _, inp = valuation_db
    seed_position(db, inp)
    second = "0x" + "99" * 32
    db.add(
        RedemptionAttempt(
            id=2,
            user_id=1,
            account_id=1,
            market_id=inp.condition_id,
            collection_id="0x" + "00" * 32,
            status="confirmed",
            amount=10,
            tx_hash=second,
            created_at=inp.as_of,
        )
    )
    db.commit()
    handler, _ = chain_node(inp.condition_id, 100_000_000)
    inner = handler

    def two_txs(request):
        data = json.loads(request.content)
        if data["method"] == "eth_getTransactionReceipt":
            receipt, _, _ = receipt_for(inp.condition_id, 10_000_000)
            receipt["transactionHash"] = second
            receipt["logs"][0]["transactionHash"] = second
            if data["params"][0] == TX:
                receipt, _, _ = receipt_for(inp.condition_id, 100_000_000)
            return httpx.Response(200, json={"id": 1, "jsonrpc": "2.0", "result": receipt})
        return inner(request)

    app = build_app(db, monkeypatch, inp, two_txs)
    with TestClient(app) as client:
        data = client.get("/api/settlement/recorded").json()
    assert data["accounting_status"] == "abstain"
    assert data["realized_pnl_usdc"] is None
    joined = " ".join(data["accounting_reasons"])
    assert "MULTIPLE_NONZERO_REDEMPTIONS_NEED_MANUAL_REVIEW" in joined


# -------------------------------------------------------------------- ledger


def test_sync_appends_server_derived_chain_history_once_and_audits(valuation_db, monkeypatch):
    db, _, inp = valuation_db
    seed_position(db, inp)
    handler, _ = chain_node(inp.condition_id, 100_000_000)
    app = build_app(db, monkeypatch, inp, handler)
    # Make accounting abstain too so a discrepancy is audited.
    db.execute(delete(LiveBalanceSnapshot))
    db.commit()
    with TestClient(app) as client:
        first = client.post("/api/settlement/sync")
        assert first.status_code == 200, first.text
        body = first.json()
        assert [row["state"] for row in body["created"]] == ["FINAL", "TX_CONFIRMED"]
        assert body["created"][1]["previous_state"] == "FINAL"
        assert body["created"][1]["evidence_class"] == "chain_verified"
        assert body["created"][1]["evidence"]["payout_usdc"] == "100"
        assert body["discrepancies_flagged_for_manual_review"] >= 1
        again = client.post("/api/settlement/sync").json()
        assert again["created"] == [] and again["discrepancies_flagged_for_manual_review"] == 0
        history = client.get(f"/api/settlement/transitions?condition_id={inp.condition_id}").json()
        assert history["integrity"] == "verified"
        assert [row["sequence"] for row in history["transitions"]] == [1, 2]
        assert (
            client.get(
                f"/api/settlement/transitions?condition_id={inp.condition_id}",
                headers={"X-Account-Id": "3"},
            ).status_code
            == 403
        )
    audits = db.scalars(
        select(AppEvent).where(AppEvent.event_type == "settlement_discrepancy_flagged")
    ).all()
    assert audits and len({a.aggregate_id for a in audits}) == len(audits)
    assert all(a.account_id == 1 and a.source == "settlement_sync" for a in audits)


def test_api_cannot_claim_chain_evidence_or_supply_previous_state(valuation_db, monkeypatch):
    db, _, inp = valuation_db
    handler, _ = chain_node(inp.condition_id, 0)
    app = build_app(db, monkeypatch, inp, handler)
    base = {
        "condition_id": inp.condition_id,
        "state": "RESOLUTION_PROPOSED",
        "observed_at": (inp.as_of - timedelta(minutes=5)).isoformat(),
        "evidence_class": "operator_asserted",
        "evidence": {"note": "UMA proposal observed"},
    }
    with TestClient(app) as client:
        ok = client.post("/api/settlement/transitions", json=base)
        assert ok.status_code == 200, ok.text
        assert ok.json()["transition"]["previous_state"] is None
        assert (
            client.post("/api/settlement/transitions", json={**base, "previous_state": "FINAL"})
        ).status_code == 422
        chain = client.post(
            "/api/settlement/transitions", json={**base, "evidence_class": "chain_verified"}
        )
        assert chain.status_code == 422
        confirmed = client.post(
            "/api/settlement/transitions",
            json={
                **base,
                "state": "TX_CONFIRMED",
                "observed_at": inp.as_of.isoformat(),
                "evidence_class": "operator_asserted",
            },
        )
        assert confirmed.status_code == 422
        assert confirmed.json()["detail"] == "CHAIN_EVIDENCE_MUST_BE_SERVER_DERIVED"
        disputed = client.post(
            "/api/settlement/transitions",
            json={**base, "state": "DISPUTED", "observed_at": inp.as_of.isoformat()},
        )
        assert disputed.status_code == 200
        final = client.post(
            "/api/settlement/transitions",
            json={**base, "state": "FINAL", "observed_at": inp.as_of.isoformat()},
        )
        assert final.json()["transition"]["previous_state"] == "DISPUTED"
        # Replaying the same observation is idempotent; a stale clock is refused.
        replay = client.post(
            "/api/settlement/transitions",
            json={**base, "state": "FINAL", "observed_at": inp.as_of.isoformat()},
        )
        assert replay.json()["created"] is False
        stale = client.post(
            "/api/settlement/transitions",
            json={**base, "state": "REVERSED", "observed_at": base["observed_at"]},
        )
        assert stale.json()["detail"] == "OBSERVATION_BEFORE_PREVIOUS_TRANSITION"
        future = client.post(
            "/api/settlement/transitions",
            json={
                **base,
                "state": "REVERSED",
                "observed_at": (inp.as_of + timedelta(days=400)).isoformat(),
            },
        )
        assert future.json()["detail"] == "FUTURE_OBSERVATION"


def test_state_machine_rejects_regression_and_unproven_jumps(valuation_db):
    db, _, inp = valuation_db
    account = db.get(Account, 1)
    now = inp.as_of
    t0 = now - timedelta(hours=1)
    cid = inp.condition_id

    def add(state, minutes, **kw):
        return ledger.append_transition(
            db,
            account,
            condition_id=cid,
            state=state,
            observed_at=t0 + timedelta(minutes=minutes),
            evidence_class=kw.pop("cls", "operator_asserted"),
            evidence=kw.pop("evidence", {"minute": minutes}),
            now=now,
            **kw,
        )

    add(S.RESOLUTION_PROPOSED, 0)
    with pytest.raises(ledger.TransitionRejected) as bad:
        add(S.TRADING_OPEN, 1)
    assert bad.value.code == "ILLEGAL_TRANSITION_RESOLUTION_PROPOSED_TO_TRADING_OPEN"
    add(S.FINAL, 2)
    row, _ = add(S.REVERSED, 3)  # a reversal is explicit, never a silent rewrite
    assert row.previous_state == "FINAL"
    with pytest.raises(ledger.TransitionRejected) as bad:
        add(S.TX_CONFIRMED, 4)
    assert bad.value.code == "CHAIN_EVIDENCE_MUST_BE_SERVER_DERIVED"
    with pytest.raises(ledger.TransitionRejected) as bad:
        add(S.REVERSED, 4, evidence={"minute": 99})
    assert bad.value.code == "STATE_UNCHANGED_WITH_DIFFERENT_EVIDENCE"
    rows = ledger.history(db, account, cid)
    assert ledger.verify_chain(rows) == []
    # Tampering below the ORM is detected by the hash chain even without triggers.
    rows[1].evidence_json = '{"minute": 7}'
    assert any(p.startswith("HASH_MISMATCH") for p in ledger.verify_chain(rows))
    db.rollback()


def test_database_refuses_update_and_delete_of_transitions(valuation_db):
    db, _, inp = valuation_db
    account = db.get(Account, 1)
    row, _ = ledger.append_transition(
        db,
        account,
        condition_id=inp.condition_id,
        state=S.FINAL,
        observed_at=inp.as_of - timedelta(minutes=1),
        evidence_class="operator_asserted",
        evidence={},
        now=inp.as_of,
    )
    db.commit()
    with pytest.raises(DatabaseError, match="append-only"):
        db.execute(update(SettlementTransition).values(state="VOID"))
    db.rollback()
    with pytest.raises(DatabaseError, match="append-only"):
        db.execute(delete(SettlementTransition))
    db.rollback()
    assert (
        db.scalar(select(SettlementTransition.state).where(SettlementTransition.id == row.id))
        == "FINAL"
    )


def test_ledger_is_account_isolated(valuation_db):
    db, _, inp = valuation_db
    mine, foreign = db.get(Account, 1), db.get(Account, 3)
    ledger.append_transition(
        db,
        mine,
        condition_id=inp.condition_id,
        state=S.FINAL,
        observed_at=inp.as_of - timedelta(minutes=1),
        evidence_class="operator_asserted",
        evidence={},
        now=inp.as_of,
    )
    assert ledger.history(db, foreign, inp.condition_id) == []
    row, created = ledger.append_transition(
        db,
        foreign,
        condition_id=inp.condition_id,
        state=S.VOID,
        observed_at=inp.as_of - timedelta(minutes=1),
        evidence_class="operator_asserted",
        evidence={},
        now=inp.as_of,
    )
    assert created and row.sequence == 1 and row.prev_hash is None
    assert text  # keep import used by the PostgreSQL module that shares helpers
    assert UTC
