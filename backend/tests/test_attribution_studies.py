"""Synthetic prospective-policy acceptance, not a claim about funded forward results."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from test_live_account_reconciliation import T0
from test_live_account_reconciliation import account_db as _account_db

from app.models import Account, AppEvent
from app.services.analytics import attribution_studies as studies
from app.services.analytics.attribution_contracts import acceptance_checks
from app.services.analytics.live_account_reconciliation import canonical, digest
from app.services.analytics.pnl_reconciliation import reconcile_confirmed_cashflows
from app.services.analytics.replay.follower import Book, Level
from app.services.market_data.replay_capture import append_capture

account_db = _account_db
FINGERPRINT = "f" * 64
CONFIGURATION = {"classification": "synthetic_configuration_fixture"}
CONFIG_FINGERPRINT = digest(CONFIGURATION)
RULE = "a" * 64


def capture_condition(db, i, instant, *, event=None, final=False):
    condition = "0x" + f"{i + 100:064x}"
    yes, no = str(100 + i * 2), str(101 + i * 2)
    clob = {
        "active": not final,
        "closed": final,
        "accepting_orders": not final,
        "fees_enabled": False,
        "tokens": [
            {"token_id": yes, "outcome": "Yes", "winner": False},
            {"token_id": no, "outcome": "No", "winner": final},
        ],
    }
    gamma = {
        "active": not final,
        "closed": final,
        "feesEnabled": False,
        "outcomes": ["Yes", "No"],
        "clobTokenIds": [yes, no],
        "outcomePrices": ["0", "1"] if final else [".4", ".6"],
        "umaResolutionStatus": "resolved" if final else "proposed",
        "events": [{"id": str(i if event is None else event)}],
    }
    for kind, raw in (("clob_market", clob), ("gamma", gamma)):
        append_capture(
            db,
            kind=kind,
            source_key=condition,
            raw=raw,
            observed_at=instant,
            condition_id=condition,
            rule_hash=RULE,
        )
    if not final:
        for token in (yes, no):
            book = Book(
                snapshot_id=f"study-{i}-{token}",
                liquidity_id=f"study-budget-{i}-{token}",
                token_id=token,
                source_at=instant,
                observed_at=instant,
                bids=[Level(price=Decimal(".39"), shares=Decimal(100))],
                asks=[Level(price=Decimal(".4"), shares=Decimal(100))],
            )
            append_capture(
                db,
                kind="book",
                source_key=book.snapshot_id,
                raw={
                    "book": book.model_dump(mode="json"),
                    "venue": {
                        "asset_id": token,
                        "market": condition,
                        "tick_size": ".01",
                        "min_order_size": ".01",
                    },
                },
                observed_at=instant,
                source_at=instant,
                condition_id=condition,
                token_id=token,
                rule_hash=RULE,
            )
    return condition, no


def fixture_study(db, monkeypatch, *, grouped=False):
    clock = [T0]
    monkeypatch.setattr(studies, "utc_now", lambda: clock[0])
    monkeypatch.setattr(studies, "implementation_fingerprint", lambda: FINGERPRINT)
    monkeypatch.setattr(studies, "configuration_snapshot", lambda *args: CONFIGURATION)
    account = db.get(Account, 11)
    registered = studies.register_study(
        db,
        account,
        studies.RegisterStudy(
            strategy="manual",
            enrollment_end=T0 + timedelta(hours=1),
            settlement_deadline=T0 + timedelta(hours=2),
        ),
    )
    fills = []
    for i in range(20):
        decision = T0 + timedelta(seconds=10 * (i + 1))
        condition, token = capture_condition(db, i, decision, event=0 if grouped else i)
        event = AppEvent(
            user_id=1,
            account_id=11,
            source="live_gateway",
            aggregate_type="order",
            aggregate_id=f"order-{i}",
            event_type="order_intent_created",
            occurred_at=decision,
            payload_json=json.dumps(
                {
                    "strategy": "manual",
                    "side": "BUY",
                    "condition_id": condition,
                    "token_id": token,
                    "implementation_fingerprint": FINGERPRINT,
                    "configuration_fingerprint": CONFIG_FINGERPRINT,
                }
            ),
        )
        db.add(event)
        db.flush()
        fills.append(
            {
                "source_id": i + 1000,
                "intent_source_id": event.id,
                "order_hash": f"order-{i}",
                "fill_id": f"fill-{i}",
                "side": "BUY",
                "strategy": "manual",
                "token_id": token,
                "condition_id": condition,
                "decision_at": decision.isoformat(),
                "matched_at": (decision + timedelta(seconds=1)).isoformat(),
                "shares": "10",
                "notional": "4",
                "fee_usdc": ".1",
                "confirmed_rebate_usdc": "0",
            }
        )
        fills.append(
            {
                "source_id": f"redemption-{i}",
                "order_hash": f"redemption-{i}",
                "fill_id": f"redemption-{i}",
                "side": "SELL",
                "strategy": "settlement",
                "token_id": token,
                "condition_id": condition,
                "matched_at": (T0 + timedelta(hours=2, seconds=-20 + i)).isoformat(),
                "shares": "10",
                "notional": "10",
                "fee_usdc": "0",
                "confirmed_rebate_usdc": "0",
                "kind": "SETTLEMENT",
            }
        )
        capture_condition(
            db, i, T0 + timedelta(hours=2, seconds=-30), event=0 if grouped else i, final=True
        )
    db.commit()
    opening = {"cash_usdc": "1000", "positions": {}, "coverage": "finalized_supported_asset_scope"}
    closing = {"cash_usdc": "1118", "positions": {}, "coverage": "finalized_supported_asset_scope"}
    report = reconcile_confirmed_cashflows(fills, opening, closing, opening_lots={})
    report.update(
        opening_observed_at=T0.isoformat(),
        closing_observed_at=(T0 + timedelta(hours=2, seconds=1)).isoformat(),
        history_evidence=[],
        fill_evidence=fills,
        input_hash=digest(fills),
        chain_reconciliation={"scope_reconciled": True, "external_cashflows": []},
        opening_mark={"observed_nav": "1000"},
        closing_mark={"observed_nav": "1118"},
        full_wallet_reconciled=False,
        benchmarks=[{"status": "observed_comparison"}],
        fixture_only=True,
    )
    clock[0] = T0 + timedelta(hours=2, seconds=2)
    return registered, json.loads(canonical(report)), clock


def test_complete_disjoint_forward_acceptance(account_db, monkeypatch):
    registered, report, _ = fixture_study(account_db, monkeypatch)
    result = studies.evaluate_study(
        account_db, account_db.get(Account, 11), registered["study_id"], report
    )
    assert result["status"] == "complete", result["reasons"]
    assert result["event_count"] == result["eligible_entry_decision_count"] == 20
    assert len(result["pairs"]) == 20 and Decimal(result["confidence_interval"][0]) > 0
    assert Decimal(result["scoped_nav_drawdown"]["maximum_drawdown"]) == 0
    assert result["release_decision"] == "SHADOW_EVIDENCE_SUPPORTS_SCOPED_REVIEW"
    assert not result["profitability_claim"]
    report["forward_study"] = result
    checks = acceptance_checks(report)
    assert all(check["status"] == "pass" for check in checks), checks
    for pair in result["pairs"]:
        assert pair["control"]["classification"] == "modeled_control_not_actual_fills"
        assert Decimal(pair["control"]["capital"]) == Decimal("4.1")


def test_many_trades_in_one_event_do_not_manufacture_independent_samples(account_db, monkeypatch):
    registered, report, _ = fixture_study(account_db, monkeypatch, grouped=True)
    result = studies.evaluate_study(
        account_db, account_db.get(Account, 11), registered["study_id"], report
    )
    assert result["eligible_entry_decision_count"] == 20 and result["event_count"] == 1
    assert result["status"] == "incomplete" and result["confidence_interval"] is None


@pytest.mark.parametrize("fault", ["future", "cost", "code", "overlap", "nav-gap"])
def test_missing_or_changed_forward_inputs_remain_incomplete(account_db, monkeypatch, fault):
    registered, report, clock = fixture_study(account_db, monkeypatch)
    if fault == "future":
        clock[0] = T0 + timedelta(minutes=30)
    elif fault == "cost":
        report["realized_allocations"][0]["entry_fee"] = None
    elif fault == "code":
        row = account_db.query(AppEvent).order_by(AppEvent.id).first()
        raw = json.loads(row.payload_json)
        raw["implementation_fingerprint"] = "changed"
        row.payload_json = json.dumps(raw)
        account_db.commit()
    elif fault == "nav-gap":
        report["closing_mark"]["observed_nav"] = None
    else:
        # Registering after an event's development decision cannot bless it as holdout.
        prior = registered.copy()
        prior.pop("study_id")
        prior["development_events"] = {"0": "captured"}
        from app.models import AttributionStudy

        identity = digest(prior)
        account_db.add(
            AttributionStudy(
                id=identity,
                user_id=1,
                account_id=11,
                registered_at=T0,
                policy_json=canonical(prior),
            )
        )
        account_db.commit()
        registered["study_id"] = identity
    result = studies.evaluate_study(
        account_db, account_db.get(Account, 11), registered["study_id"], report
    )
    assert result["status"] == "incomplete" and result["reasons"]


def test_study_registration_rejects_retrospective_window_and_foreign_reads(account_db, monkeypatch):
    monkeypatch.setattr(studies, "utc_now", lambda: T0)
    with pytest.raises(ValueError, match="FUTURE_WINDOW"):
        studies.register_study(
            account_db,
            account_db.get(Account, 11),
            studies.RegisterStudy(
                strategy="manual", enrollment_end=T0, settlement_deadline=T0 + timedelta(hours=2)
            ),
        )
    registered, _, _ = fixture_study(account_db, monkeypatch)
    with pytest.raises(LookupError):
        studies.read_study(account_db, account_db.get(Account, 21), registered["study_id"])


def test_configuration_fingerprint_tracks_parameters_not_runtime_counters(account_db):
    from app.models import UserSettings
    from app.models.trading.market_maker_config import MarketMakerConfig
    from app.services.analytics.implementation_provenance import configuration_fingerprint

    db = account_db
    wallet = UserSettings(user_id=1)
    maker = MarketMakerConfig(
        user_id=1, account_id=11, condition_id="condition", token_id_yes="11", token_id_no="22"
    )
    db.add_all([wallet, maker])
    db.commit()
    for strategy in ("manual", "copy", "inverse", "market_maker", "latency_arb"):
        assert configuration_fingerprint(db, 1, 11, strategy) == configuration_fingerprint(
            db, 1, None, strategy
        )
    original = configuration_fingerprint(db, 1, 11, "market_maker")
    wallet.consecutive_wins = 3
    maker.current_open_orders = 7
    db.commit()
    assert configuration_fingerprint(db, 1, 11, "market_maker") == original
    maker.band_order_size = 21
    db.commit()
    assert configuration_fingerprint(db, 1, 11, "market_maker") != original


@pytest.mark.parametrize(
    "terminal_fault", [None, "missing-size", "before-decision", "after-deadline"]
)
def test_zero_fill_requires_explicit_terminal_proof(account_db, monkeypatch, terminal_fault):
    registered, report, _ = fixture_study(account_db, monkeypatch)
    decision = T0 + timedelta(minutes=20)
    condition, token = capture_condition(account_db, 30, decision)
    capture_condition(account_db, 30, T0 + timedelta(hours=1, minutes=30), final=True)
    row = AppEvent(
        user_id=1,
        account_id=11,
        source="live_gateway",
        aggregate_type="order",
        aggregate_id="zero-fill",
        event_type="order_intent_created",
        occurred_at=decision,
        payload_json=json.dumps(
            {
                "strategy": "manual",
                "side": "BUY",
                "condition_id": condition,
                "token_id": token,
                "implementation_fingerprint": FINGERPRINT,
                "configuration_fingerprint": CONFIG_FINGERPRINT,
                "notional": "4.1",
            }
        ),
    )
    account_db.add(row)
    account_db.commit()
    missing = studies.evaluate_study(
        account_db, account_db.get(Account, 11), registered["study_id"], report
    )
    assert "NO_FILL_DECISION_TERMINAL_EVIDENCE_MISSING" in missing["reasons"]
    terminal = {"state": "CANCELLED", "size_matched": "0"}
    terminal_at = decision + timedelta(seconds=10)
    if terminal_fault == "missing-size":
        terminal.pop("size_matched")
    elif terminal_fault == "before-decision":
        terminal_at = decision - timedelta(seconds=1)
    elif terminal_fault == "after-deadline":
        terminal_at = T0 + timedelta(hours=3)
    account_db.add(
        AppEvent(
            user_id=1,
            account_id=11,
            source="live_gateway",
            aggregate_type="order",
            aggregate_id="zero-fill",
            event_type="order_state_changed",
            occurred_at=terminal_at,
            payload_json=json.dumps({"after": terminal}),
        )
    )
    account_db.commit()
    result = studies.evaluate_study(
        account_db, account_db.get(Account, 11), registered["study_id"], report
    )
    if terminal_fault:
        assert result["status"] == "incomplete"
        assert "NO_FILL_DECISION_TERMINAL_EVIDENCE_MISSING" in result["reasons"]
        assert result["zero_fill_decisions"] == []
        return
    assert result["status"] == "complete", result["reasons"]
    assert result["eligible_entry_decision_count"] == 21 and result["event_count"] == 21
    assert result["executed_event_count"] == 20
    assert len(result["zero_fill_decisions"]) == 1


@pytest.mark.parametrize("fault", ["late-exit", "early-exit", "missing-exit", "late-entry"])
def test_study_enforces_execution_window(account_db, monkeypatch, fault):
    registered, report, clock = fixture_study(account_db, monkeypatch)
    entry, exit_fill = report["fill_evidence"][:2]
    if fault == "late-exit":
        exit_fill["matched_at"] = (T0 + timedelta(hours=3)).isoformat()
    elif fault == "early-exit":
        exit_fill["matched_at"] = T0.isoformat()
    elif fault == "missing-exit":
        report["fill_evidence"].remove(exit_fill)
    else:
        entry["matched_at"] = (T0 + timedelta(hours=3)).isoformat()
    report["closing_observed_at"] = (T0 + timedelta(hours=3, seconds=1)).isoformat()
    clock[0] = T0 + timedelta(hours=3, seconds=2)
    result = studies.evaluate_study(
        account_db, account_db.get(Account, 11), registered["study_id"], report
    )
    expected = (
        "PROSPECTIVE_ENTRY_OUTSIDE_SETTLEMENT_WINDOW"
        if fault == "late-entry"
        else "PROSPECTIVE_EXIT_TIME_UNVERIFIED"
        if fault == "missing-exit"
        else "PROSPECTIVE_EXIT_OUTSIDE_SETTLEMENT_WINDOW"
    )
    assert expected in result["reasons"]
    assert result["status"] == "incomplete"
    assert result["release_decision"] == "ABSTAIN_PENDING_FORWARD_EVIDENCE"


def test_study_accepts_exit_at_exact_preregistered_deadline(account_db, monkeypatch):
    registered, report, _ = fixture_study(account_db, monkeypatch)
    report["fill_evidence"][1]["matched_at"] = (T0 + timedelta(hours=2)).isoformat()
    result = studies.evaluate_study(
        account_db, account_db.get(Account, 11), registered["study_id"], report
    )
    assert result["status"] == "complete", result["reasons"]
