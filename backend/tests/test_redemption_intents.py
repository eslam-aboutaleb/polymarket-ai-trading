"""Durable transaction fault injection using a funded synthetic RPC boundary."""

import json
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redemption_fixtures import CONDITION, TEST_KEY, TEST_WALLET, FundedRedemptionRPC
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.portfolio.redemption_intents import router
from app.models import Account, AppEvent, Base, TradingControlState, User, UserSettings
from app.models.trading.redemption_intent import RedemptionIntent, RedemptionObservation
from app.services.trading import redemption_workflow as workflow
from app.services.trading.live_trading_policy import LiveTradingBlocked
from app.utils.database import get_db


@pytest.fixture
def redemption_db():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add_all(
            [User(id=1, wallet_address=TEST_WALLET), User(id=2, wallet_address="0x" + "2" * 40)]
        )
        db.flush()
        db.add_all(
            [
                Account(
                    id=11,
                    owner_user_id=1,
                    name="Funded synthetic",
                    kind="personal",
                    is_default=True,
                ),
                Account(
                    id=12, owner_user_id=2, name="Other owner", kind="personal", is_default=True
                ),
                Account(id=13, owner_user_id=1, name="Unallocated", kind="sub"),
                UserSettings(user_id=1, simulation_mode=False, trading_halted=False),
                TradingControlState(
                    scope="global", state="RUNNING", reason="synthetic test fixture"
                ),
                TradingControlState(
                    scope="user:1", user_id=1, state="RUNNING", reason="synthetic test fixture"
                ),
            ]
        )
        db.commit()
        yield db
    engine.dispose()


@pytest.fixture
def synthetic_release(monkeypatch):
    # This is explicitly test-only authority. The production release gate is
    # exercised unpatched in the separate barrier regression below.
    monkeypatch.setattr(workflow, "block_legacy_live_execution", lambda strategy: None)
    monkeypatch.setattr(
        workflow, "load_wallet_credentials", lambda wallet: {"private_key": TEST_KEY}
    )


def intent(db, native, key="one", mode="submit"):
    return workflow.create_intent(db, db.get(Account, 11), CONDITION, key, mode=mode, chain=native)


def test_preview_is_immutable_idempotent_and_cannot_build_or_submit(redemption_db):
    db, rpc = redemption_db, FundedRedemptionRPC()
    native = rpc.native()
    try:
        first = intent(db, native, mode="preview")
        assert intent(db, native, mode="preview").id == first.id
        assert db.query(RedemptionIntent).count() == 1
        assert rpc.nonce_reads == 0 and rpc.broadcasts == []
        assert first.nonce is None and first.transaction_hash is None
        assert json.loads(first.snapshot_json)["expected_payout"] == "10"
        with pytest.raises(ValueError, match="PREVIEW_CANNOT_SUBMIT"):
            workflow.submit_intent(db, db.get(Account, 11), first.id, chain=native)
        with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
            intent(db, native, mode="submit")
    finally:
        native.close()


def test_existing_release_gate_blocks_before_credentials_nonce_or_signing(
    redemption_db, monkeypatch
):
    db, rpc = redemption_db, FundedRedemptionRPC()
    native = rpc.native()
    try:
        row = intent(db, native)
        monkeypatch.setattr(
            workflow, "load_wallet_credentials", lambda wallet: pytest.fail("credentials touched")
        )
        with pytest.raises(LiveTradingBlocked):
            workflow.submit_intent(db, db.get(Account, 11), row.id, chain=native)
        assert row.nonce is None and rpc.nonce_reads == 0 and rpc.broadcasts == []
    finally:
        native.close()


@pytest.mark.parametrize("ambiguous", [False, True])
def test_hash_is_durable_before_send_and_restart_never_rebroadcasts(
    redemption_db, synthetic_release, ambiguous
):
    db, rpc = redemption_db, FundedRedemptionRPC()
    rpc.fail_broadcast = ambiguous
    native = rpc.native()
    account = db.get(Account, 11)
    try:
        row = intent(db, native)

        def before_send(tx_hash):
            with Session(db.bind) as independent:
                saved = independent.get(RedemptionIntent, row.id)
                assert (
                    saved.transaction_hash == tx_hash
                    and saved.nonce == 7
                    and saved.state == "unknown"
                )

        rpc.on_broadcast = before_send
        workflow.submit_intent(db, account, row.id, chain=native)
        assert row.state == ("unknown" if ambiguous else "submitted")
        assert len(rpc.broadcasts) == 1
        workflow.submit_intent(db, account, row.id, chain=native)
        assert len(rpc.broadcasts) == 1
        # Receipt observed before finalized height must not recognize cash.
        workflow.reconcile_intent(db, account, row.id, chain=native)
        assert row.state == "submitted" and row.payout is None
        rpc.final_number = 30
        workflow.reconcile_intent(db, account, row.id, chain=native)
        assert row.state == "confirmed" and row.payout == Decimal(10)
        workflow.reconcile_intent(db, account, row.id, chain=native)
        assert db.query(AppEvent).filter_by(event_type="redemption_finalized").count() == 1
        assert len(rpc.broadcasts) == rpc.nonce_reads == 1
    finally:
        native.close()


@pytest.mark.parametrize(
    "fault", ["revert", "reorg", "no-credit", "wrong-wallet", "wrong-transaction", "missing"]
)
def test_bad_or_ambiguous_receipt_never_credits_cash(redemption_db, synthetic_release, fault):
    db, rpc = redemption_db, FundedRedemptionRPC()
    native = rpc.native()
    account = db.get(Account, 11)
    try:
        row = intent(db, native)
        workflow.submit_intent(db, account, row.id, chain=native)
        rpc.final_number = 30
        if fault == "revert":
            rpc.receipt["status"] = "0x0"
        elif fault == "reorg":
            rpc.receipt["blockHash"] = "0x" + "f" * 64
        elif fault == "no-credit":
            rpc.receipt["logs"].pop()
        elif fault == "wrong-wallet":
            rpc.eth.get_transaction = lambda tx: {**rpc.transaction(tx), "from": "0x" + "2" * 40}
        elif fault == "wrong-transaction":
            rpc.receipt["transactionHash"] = "0x" + "f" * 64
        else:
            rpc.receipt = None
        workflow.reconcile_intent(db, account, row.id, chain=native)
        assert row.state != "confirmed" and row.payout is None
        assert db.query(AppEvent).filter_by(event_type="redemption_finalized").count() == 0
        assert len(rpc.broadcasts) == 1
    finally:
        native.close()


def test_reorg_withdraws_projection_preserves_original_proof_and_never_double_credits(
    redemption_db, synthetic_release
):
    db, rpc = redemption_db, FundedRedemptionRPC()
    native = rpc.native()
    account = db.get(Account, 11)
    try:
        row = intent(db, native)
        workflow.submit_intent(db, account, row.id, chain=native)
        rpc.final_number = 30
        workflow.reconcile_intent(db, account, row.id, chain=native)
        original = row.receipt_hash
        rpc.receipt["blockHash"] = "0x" + "f" * 64
        workflow.reconcile_intent(db, account, row.id, chain=native)
        assert row.state == "reorged" and row.payout is None
        assert db.query(RedemptionObservation).filter_by(proof_hash=original).count() == 1
        from chain_attribution_fixtures import block

        rpc.receipt["blockHash"] = block(18)["hash"]
        workflow.reconcile_intent(db, account, row.id, chain=native)
        assert row.state == "confirmed"
        assert db.query(AppEvent).filter_by(event_type="redemption_finalized").count() == 1
    finally:
        native.close()


def test_wallet_lease_blocks_a_second_inflight_intent(redemption_db, synthetic_release):
    db, rpc = redemption_db, FundedRedemptionRPC()
    native = rpc.native()
    account = db.get(Account, 11)
    try:
        first = intent(db, native, "first")
        second = intent(db, native, "second")
        workflow.submit_intent(db, account, first.id, chain=native)
        with pytest.raises(ValueError, match="WALLET_RECONCILIATION_REQUIRED"):
            workflow.submit_intent(db, account, second.id, chain=native)
        assert len(rpc.broadcasts) == 1
    finally:
        native.close()


def test_api_owner_isolation_and_no_submit_mode(redemption_db, monkeypatch):
    db, rpc = redemption_db, FundedRedemptionRPC()
    monkeypatch.setattr(workflow, "NativeRedemption", rpc.native)
    api = FastAPI()
    api.include_router(router)
    api.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    api.dependency_overrides[get_db] = lambda: db
    client = TestClient(api)
    body = {"condition_id": CONDITION, "idempotency_key": "api-preview"}
    created = client.post(
        "/api/portfolio/redemption-intents", json=body, headers={"X-Account-Id": "11"}
    )
    assert created.status_code == 200, created.text
    assert created.json()["preview_only"] and not created.json()["on_chain_submitted"]
    assert created.headers["cache-control"] == "private, no-store"
    assert (
        client.post(
            "/api/portfolio/redemption-intents", json={**body, "execution_mode": "submit"}
        ).status_code
        == 422
    )
    identity = created.json()["intent_id"]
    assert (
        client.get(
            f"/api/portfolio/redemption-intents/{identity}", headers={"X-Account-Id": "12"}
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/portfolio/redemption-intents/{identity}", headers={"X-Account-Id": "13"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/portfolio/redemption-intents", json=body, headers={"X-Account-Id": "13"}
        ).status_code
        == 409
    )
    assert client.get(f"/api/portfolio/redemption-intents/{identity}/evidence").status_code == 200
    assert rpc.nonce_reads == 0 and rpc.broadcasts == []


def test_legacy_entrypoints_cannot_bypass_durable_release_barrier(redemption_db, monkeypatch):
    from app.services.trading import redemption_service as legacy

    db = redemption_db
    monkeypatch.setattr(
        workflow, "load_wallet_credentials", lambda _: pytest.fail("credentials touched")
    )
    with pytest.raises(LiveTradingBlocked):
        legacy._execute_redeem(db, 1, TEST_WALLET, {"condition_id": CONDITION}, dry_run=False)


def test_legacy_preview_uses_chain_evidence_without_nonce(redemption_db, monkeypatch):
    from app.services.trading import redemption_service as legacy

    rpc = FundedRedemptionRPC()
    monkeypatch.setattr(workflow, "NativeRedemption", rpc.native)
    row = legacy._execute_redeem(
        redemption_db, 1, TEST_WALLET, {"condition_id": CONDITION}, dry_run=True
    )
    assert row.status == "preview" and row.intent_id
    assert row.tx_hash is None and rpc.nonce_reads == 0 and rpc.broadcasts == []


@pytest.mark.parametrize("setting", ["simulation_mode", "trading_halted"])
def test_account_release_settings_prevent_nonce_or_broadcast(
    redemption_db, synthetic_release, setting
):
    db, rpc = redemption_db, FundedRedemptionRPC()
    native = rpc.native()
    try:
        row = intent(db, native)
        settings = db.query(UserSettings).filter_by(user_id=1).one()
        setattr(settings, setting, True)
        db.commit()
        with pytest.raises(ValueError, match="ACCOUNT_RELEASE_BLOCKED"):
            workflow.submit_intent(db, db.get(Account, 11), row.id, chain=native)
        assert rpc.nonce_reads == 0 and rpc.broadcasts == []
    finally:
        native.close()


def test_corrupt_snapshot_cannot_be_reused(redemption_db):
    db, rpc = redemption_db, FundedRedemptionRPC()
    native = rpc.native()
    try:
        row = intent(db, native, mode="preview")
        row.snapshot_json = "{}"
        db.commit()
        with pytest.raises(ValueError, match="SNAPSHOT_INTEGRITY_MISMATCH"):
            workflow.read_intent(db, db.get(Account, 11), row.id)
        assert rpc.nonce_reads == 0 and rpc.broadcasts == []
    finally:
        native.close()


def test_restart_recovery_reads_saved_transaction_without_resend(
    redemption_db, synthetic_release, monkeypatch
):
    db, rpc = redemption_db, FundedRedemptionRPC()
    native = rpc.native()
    try:
        rpc.fail_broadcast = True
        row = intent(db, native)
        workflow.submit_intent(db, db.get(Account, 11), row.id, chain=native)
        rpc.final_number = 30
        monkeypatch.setattr(workflow, "NativeRedemption", rpc.native)
        assert workflow.reconcile_pending(db, db.get(Account, 11)) == ["confirmed"]
        assert len(rpc.broadcasts) == rpc.nonce_reads == 1
        assert db.get(RedemptionIntent, row.id).payout == Decimal("10")
    finally:
        native.close()


@pytest.mark.parametrize(
    "denominator,numerators,expected", [(2, [1, 1], "5"), (0, [0, 0], None), (2, [2, 2], None)]
)
def test_void_payout_uses_chain_vector_and_unresolved_or_invalid_abstain(
    redemption_db, denominator, numerators, expected
):
    db, rpc = redemption_db, FundedRedemptionRPC()
    rpc.denominator, rpc.numerators = denominator, numerators
    native = rpc.native()
    try:
        if expected is None:
            with pytest.raises(ValueError):
                intent(db, native, mode="preview")
            assert db.query(RedemptionIntent).count() == 0
        else:
            row = intent(db, native, mode="preview")
            assert json.loads(row.snapshot_json)["expected_payout"] == expected
        assert rpc.nonce_reads == 0 and rpc.broadcasts == []
    finally:
        native.close()


def test_external_redemption_invalidates_frozen_entitlement_before_signing(
    redemption_db, synthetic_release
):
    db, rpc = redemption_db, FundedRedemptionRPC()
    native = rpc.native()
    try:
        row = intent(db, native)
        rpc.redeemed = True
        result = workflow.submit_intent(db, db.get(Account, 11), row.id, chain=native)
        assert result.state == "failed" and result.reason == "REDEMPTION_ENTITLEMENT_CHANGED"
        assert rpc.nonce_reads == 0 and rpc.broadcasts == []
    finally:
        native.close()
