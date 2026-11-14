"""Tests for read-only settlement lifecycle, capital lockup, and redemption risk (#24)."""

from __future__ import annotations

import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.portfolio import settlement as settlement_route
from app.config import get_settings
from app.services.analytics.settlement_lifecycle import (
    RedemptionAttemptView,
    SettlementAnalysisRequest,
    SettlementState,
    analyze_settlement_lifecycle,
)


def _now() -> datetime:
    return datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)


def test_proposed_disputed_final_lifecycle():
    now = _now()
    proposed = analyze_settlement_lifecycle(
        SettlementAnalysisRequest(
            market_id="mkt-1",
            state=SettlementState.RESOLUTION_PROPOSED,
            position_shares=Decimal("100"),
            entry_cost_usdc=Decimal("40"),
            starting_available_cash_usdc=Decimal("500"),
            locked_collateral_usdc=Decimal("40"),
            dispute_window_ends_at=now + timedelta(hours=2),
            now=now,
        )
    )
    assert proposed.status == "abstain"
    assert not proposed.is_final_settled
    assert proposed.released_collateral_usdc == Decimal("0")
    assert proposed.realized_pnl_usdc == Decimal("0")
    assert proposed.available_cash_usdc == Decimal("500")
    assert proposed.execution_eligible is False

    disputed = analyze_settlement_lifecycle(
        SettlementAnalysisRequest(
            market_id="mkt-1",
            state=SettlementState.DISPUTED,
            previous_state=SettlementState.RESOLUTION_PROPOSED,
            position_shares=Decimal("100"),
            entry_cost_usdc=Decimal("40"),
            starting_available_cash_usdc=Decimal("500"),
            locked_collateral_usdc=Decimal("40"),
            now=now,
        )
    )
    assert disputed.status == "abstain"
    assert not disputed.is_final_settled
    assert any("dispute" in r.lower() for r in disputed.operational_risks)

    final = analyze_settlement_lifecycle(
        SettlementAnalysisRequest(
            market_id="mkt-1",
            state=SettlementState.FINAL,
            previous_state=SettlementState.DISPUTED,
            position_shares=Decimal("100"),
            entry_cost_usdc=Decimal("40"),
            starting_available_cash_usdc=Decimal("500"),
            locked_collateral_usdc=Decimal("40"),
            dispute_window_ends_at=now - timedelta(minutes=1),
            now=now,
        )
    )
    assert final.status == "ok"
    assert final.is_final_settled is True
    assert final.has_confirmed_redemption is False
    assert final.released_collateral_usdc == Decimal("0")
    assert final.realized_pnl_usdc == Decimal("0")


def test_void_and_reversed_states_abstain_and_do_not_release():
    now = _now()
    void_report = analyze_settlement_lifecycle(
        SettlementAnalysisRequest(
            market_id="mkt-void",
            state=SettlementState.VOID,
            position_shares=Decimal("50"),
            entry_cost_usdc=Decimal("25"),
            starting_available_cash_usdc=Decimal("100"),
            locked_collateral_usdc=Decimal("25"),
            now=now,
        )
    )
    assert void_report.status == "abstain"
    assert void_report.released_collateral_usdc == Decimal("0")
    assert void_report.available_cash_usdc == Decimal("100")

    reversed_report = analyze_settlement_lifecycle(
        SettlementAnalysisRequest(
            market_id="mkt-rev",
            state=SettlementState.REVERSED,
            previous_state=SettlementState.FINAL,
            position_shares=Decimal("50"),
            entry_cost_usdc=Decimal("25"),
            starting_available_cash_usdc=Decimal("100"),
            locked_collateral_usdc=Decimal("25"),
            now=now,
        )
    )
    assert reversed_report.status == "abstain"
    assert any(d.code == "OUTCOME_REVERSAL" for d in reversed_report.discrepancies)
    assert reversed_report.realized_pnl_usdc == Decimal("0")


def test_dry_run_vs_confirmed_tx_separation():
    now = _now()
    dry_run_only = analyze_settlement_lifecycle(
        SettlementAnalysisRequest(
            market_id="mkt-dr",
            state=SettlementState.REDEEMABLE,
            position_shares=Decimal("100"),
            entry_cost_usdc=Decimal("60"),
            starting_available_cash_usdc=Decimal("200"),
            locked_collateral_usdc=Decimal("60"),
            now=now,
            redemption_attempts=[
                RedemptionAttemptView(
                    attempt_id="dry-1",
                    dry_run=True,
                    tx_hash=None,
                    payout_usdc=Decimal("100"),
                    gas_fee_usdc=Decimal("0.50"),
                )
            ],
        )
    )
    assert dry_run_only.has_dry_run_only is True
    assert dry_run_only.has_confirmed_redemption is False
    assert dry_run_only.released_collateral_usdc == Decimal("0")
    assert dry_run_only.available_cash_usdc == Decimal("200.000000")
    assert dry_run_only.realized_pnl_usdc == Decimal("0")

    confirmed = analyze_settlement_lifecycle(
        SettlementAnalysisRequest(
            market_id="mkt-dr",
            state=SettlementState.TX_CONFIRMED,
            position_shares=Decimal("100"),
            entry_cost_usdc=Decimal("60"),
            venue_reported_payout_usdc=Decimal("100"),
            starting_available_cash_usdc=Decimal("200"),
            locked_collateral_usdc=Decimal("60"),
            now=now,
            redemption_attempts=[
                RedemptionAttemptView(
                    attempt_id="dry-1",
                    dry_run=True,
                    tx_hash=None,
                    payout_usdc=Decimal("100"),
                    gas_fee_usdc=Decimal("0.50"),
                ),
                RedemptionAttemptView(
                    attempt_id="tx-1",
                    dry_run=False,
                    tx_hash="0xabc123",
                    chain_receipt_status="confirmed",
                    confirmations=15,
                    required_confirmations=12,
                    payout_usdc=Decimal("100"),
                    gas_fee_usdc=Decimal("0.25"),
                ),
            ],
        )
    )
    assert confirmed.status == "ok"
    assert confirmed.has_dry_run_only is False
    assert confirmed.has_confirmed_redemption is True
    assert confirmed.released_collateral_usdc == Decimal("60.000000")
    assert confirmed.locked_collateral_usdc == Decimal("0")
    assert confirmed.total_gas_fee_usdc == Decimal("0.250000")
    assert confirmed.available_cash_usdc == Decimal("299.750000")
    assert confirmed.realized_pnl_usdc == Decimal("39.750000")


def test_discrepancies_duplicate_reverted_missing_receipt_and_balance_mismatch():
    now = _now()
    dup = analyze_settlement_lifecycle(
        SettlementAnalysisRequest(
            market_id="mkt-dup",
            state=SettlementState.TX_CONFIRMED,
            position_shares=Decimal("10"),
            entry_cost_usdc=Decimal("5"),
            now=now,
            redemption_attempts=[
                RedemptionAttemptView(
                    attempt_id="a1",
                    dry_run=False,
                    tx_hash="0x111",
                    chain_receipt_status="confirmed",
                    confirmations=12,
                    payout_usdc=Decimal("10"),
                ),
                RedemptionAttemptView(
                    attempt_id="a2",
                    dry_run=False,
                    tx_hash="0x222",
                    chain_receipt_status="confirmed",
                    confirmations=12,
                    payout_usdc=Decimal("10"),
                ),
            ],
        )
    )
    assert dup.status == "abstain"
    assert any(d.code == "DUPLICATE_REDEMPTION" for d in dup.discrepancies)
    assert dup.has_confirmed_redemption is False

    reverted_and_missing = analyze_settlement_lifecycle(
        SettlementAnalysisRequest(
            market_id="mkt-bad",
            state=SettlementState.TX_FAILED,
            position_shares=Decimal("10"),
            entry_cost_usdc=Decimal("5"),
            proxy_token_balance=Decimal("5"),
            eoa_token_balance=Decimal("4"),
            now=now,
            redemption_attempts=[
                RedemptionAttemptView(
                    attempt_id="r1",
                    dry_run=False,
                    tx_hash="0xdead",
                    chain_receipt_status="reverted",
                ),
                RedemptionAttemptView(
                    attempt_id="m1",
                    dry_run=False,
                    tx_hash="0xmiss",
                    chain_receipt_status="missing",
                ),
            ],
        )
    )
    codes = {d.code for d in reverted_and_missing.discrepancies}
    assert reverted_and_missing.status == "abstain"
    assert {"REVERTED_TX", "MISSING_RECEIPT", "PROXY_EOA_BALANCE_MISMATCH"} <= codes


def test_capital_lockup_opportunity_cost():
    now = _now()
    report = analyze_settlement_lifecycle(
        SettlementAnalysisRequest(
            market_id="mkt-lock",
            state=SettlementState.FINAL,
            position_shares=Decimal("1000"),
            entry_cost_usdc=Decimal("730"),
            starting_available_cash_usdc=Decimal("1000"),
            locked_collateral_usdc=Decimal("730"),
            annual_funding_rate=Decimal("0.10"),
            expected_lockup_days=Decimal("5"),
            stressed_lockup_days=Decimal("20"),
            now=now,
        )
    )
    # 730 * 0.10 * 5 / 365 = 1.000000; stressed = 4.000000
    assert report.expected_opportunity_cost_usdc == Decimal("1.000000")
    assert report.stressed_opportunity_cost_usdc == Decimal("4.000000")
    assert report.available_cash_after_lockup_usdc == Decimal("999.000000")


def test_settlement_api_endpoints_and_feature_flag(monkeypatch):
    app = FastAPI()
    app.include_router(settlement_route.router)
    app.dependency_overrides[get_current_user_from_token] = lambda: SimpleNamespace(
        id="user-1", email="test@example.com"
    )
    client = TestClient(app)

    settings = get_settings()
    monkeypatch.setattr(settings, "settlement_redemption_enabled", False)
    assert client.get("/api/settlement/schema").status_code == 503

    monkeypatch.setattr(settings, "settlement_redemption_enabled", True)
    schema_resp = client.get("/api/settlement/schema")
    assert schema_resp.status_code == 200
    assert "input_schema" in schema_resp.json()

    analyze_resp = client.post(
        "/api/settlement/analyze",
        json={
            "market_id": "mkt-api",
            "state": "DISPUTED",
            "locked_collateral_usdc": "100",
        },
    )
    assert analyze_resp.status_code == 200
    body = analyze_resp.json()
    assert body["status"] == "abstain"
    assert body["execution_eligible"] is False


def test_research_mcp_settlement_tool(monkeypatch):
    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=object))
    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(repo_root))
    server = importlib.import_module("services.mcps.research.server")

    desc = server.TOOLS["describe_settlement_and_redemption"]({})
    assert desc["name"] == "settlement_and_redemption"
    assert desc["read_only"] is True

    class _FakeResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            yield (
                b'{"schema_version":"settlement-lifecycle-v1","market_id":"mkt-mcp","state":"REDEEMABLE",'
                b'"status":"ok","execution_eligible":false}'
            )

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return _FakeResponse()

    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeClient)
    result = server.TOOLS["settlement_and_redemption"](
        {"market_id": "mkt-mcp", "state": "REDEEMABLE"}
    )
    assert result["state"] == "REDEEMABLE"
    assert result["execution_eligible"] is False
