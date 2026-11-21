"""Server-registered prospective studies; missing future observations cannot be faked."""

import json
import random
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import or_, select

from app.models.analytics.live_balance_snapshot import AttributionStudy
from app.models.platform.app_event import AppEvent
from app.models.trading.user_trade import UserTrade
from app.services.analytics.attribution_control import paired_control
from app.services.analytics.execution_analytics import STRATEGY_SOURCES
from app.services.analytics.implementation_provenance import (
    configuration_hash,
    configuration_snapshot,
    implementation_fingerprint,
)
from app.services.analytics.live_account_reconciliation import account_events, canonical, digest
from app.services.analytics.paper_evidence import EvidenceUnavailable, latest, payload
from app.services.analytics.replay.ledger import accounting
from app.services.market_data.replay_capture import aware
from app.utils.time import utc_now


class RegisterStudy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    strategy: str = Field(min_length=1, max_length=30)
    enrollment_end: AwareDatetime
    settlement_deadline: AwareDatetime
    minimum_events: int = Field(default=20, ge=20, le=200)
    max_observation_gap_hours: int = Field(default=24, ge=1, le=48)
    max_scoped_drawdown: Decimal = Field(default=Decimal(".2"), gt=0, le=1)
    max_capital_per_event: Decimal = Field(
        default=Decimal(10000), gt=0, le=1_000_000, decimal_places=6
    )


def event_identity(db, condition, as_of):
    row = latest(db, "gamma", as_of, condition_id=condition)
    if row is None:
        raise EvidenceUnavailable("EVENT_CLUSTER_METADATA_UNAVAILABLE")
    raw = payload(row)
    events = raw.get("events")
    if (
        not isinstance(events, list)
        or len(events) != 1
        or not isinstance(events[0], dict)
        or not str(events[0].get("id", ""))
    ):
        raise EvidenceUnavailable("EVENT_CLUSTER_IDENTITY_AMBIGUOUS")
    return str(events[0]["id"]), row.id


def intents(db, account, until):
    rows = db.scalars(
        select(AppEvent)
        .where(
            AppEvent.user_id == account.owner_user_id,
            account_events(account),
            AppEvent.source == "live_gateway",
            AppEvent.event_type == "order_intent_created",
            AppEvent.occurred_at <= until,
        )
        .order_by(AppEvent.id)
        .limit(2001)
    ).all()
    if len(rows) > 2000:
        raise ValueError("STUDY_DECISION_HISTORY_BOUND_EXCEEDED")
    return rows


def register_study(db, account, request):
    now = utc_now()
    if request.strategy not in STRATEGY_SOURCES or request.strategy in {
        "external",
        "stop_loss",
        "take_profit",
        "emergency",
    }:
        raise ValueError("STUDY_STRATEGY_UNAVAILABLE")
    if (
        not now + timedelta(hours=1)
        <= request.enrollment_end
        < request.settlement_deadline
        <= now + timedelta(days=90)
    ):
        raise ValueError("STUDY_MUST_BE_REGISTERED_BEFORE_ITS_FUTURE_WINDOW")
    prior, unknown, mapped_trades, prior_ids = {}, [], set(), []
    for row in intents(db, account, now):
        value = json.loads(row.payload_json)
        if value.get("strategy") != request.strategy:
            continue
        prior_ids.append(row.id)
        if value.get("trade_id") is not None:
            mapped_trades.add(value["trade_id"])
        try:
            event, capture = event_identity(db, value["condition_id"], aware(row.occurred_at))
            prior[event] = capture
        except (EvidenceUnavailable, KeyError):
            unknown.append(row.id)
    scope = UserTrade.account_id == account.id
    if account.kind == "personal":
        scope = or_(scope, UserTrade.account_id.is_(None))
    trades = db.scalars(
        select(UserTrade)
        .where(
            UserTrade.user_id == account.owner_user_id,
            scope,
            UserTrade.status != "simulated",
            UserTrade.strategy_source == request.strategy,
            UserTrade.created_at <= now,
        )
        .limit(2001)
    ).all()
    if len(trades) > 2000:
        raise ValueError("STUDY_DECISION_HISTORY_BOUND_EXCEEDED")
    unknown_trades = []
    for trade in trades:
        if trade.id in mapped_trades:
            continue
        try:
            event, capture = event_identity(db, trade.market_id, now)
            prior[event] = capture
        except EvidenceUnavailable:
            unknown_trades.append(trade.id)
    code_fingerprint = implementation_fingerprint()
    config_snapshot = configuration_snapshot(
        db, account.owner_user_id, account.id, request.strategy
    )
    config_fingerprint = configuration_hash(config_snapshot)
    if code_fingerprint is None or config_fingerprint is None:
        raise ValueError("STUDY_STRATEGY_CONTEXT_UNAVAILABLE")
    now = utc_now()
    if (
        not now + timedelta(hours=1)
        <= request.enrollment_end
        < request.settlement_deadline
        <= now + timedelta(days=90)
    ):
        raise ValueError("STUDY_MUST_BE_REGISTERED_BEFORE_ITS_FUTURE_WINDOW")
    policy = {
        **request.model_dump(mode="json"),
        "registered_at": now.isoformat(),
        "user_id": account.owner_user_id,
        "account_id": account.id,
        "version": "prospective-paired-event-study-v2",
        "baseline_version": "paired-equal-capital-yes-control-v1",
        "implementation_fingerprint": code_fingerprint,
        "configuration_fingerprint": config_fingerprint,
        "configuration_snapshot": config_snapshot,
        "implementation_scope": "backend_app_source; external_models_services_unattested",
        "development_events": prior,
        "development_decision_ids": prior_ids,
        "unmapped_development_decision_ids": unknown,
        "unmapped_development_trade_ids": unknown_trades,
        "sampling": "all_long_entry_intents_for_frozen_strategy; no-fill decisions retained",
        "zero_fill_control_budget": "signed_principal; actual strategy retains cash",
        "minimum_executed_event_clusters": request.minimum_events,
        "cluster_method": "native-event equal-weight bootstrap95-v1",
        "release_rule": "min_events; lower95>0 and actual_net>0; otherwise_reject",
        "expense_scope": "native_collateral_trading; model_costs_and_gas_unverified",
    }
    identity = digest(policy)
    db.add(
        AttributionStudy(
            id=identity,
            user_id=account.owner_user_id,
            account_id=account.id,
            registered_at=now,
            policy_json=canonical(policy),
        )
    )
    db.commit()
    return {"study_id": identity, **policy}


def read_study(db, account, identity):
    row = db.scalar(
        select(AttributionStudy).where(
            AttributionStudy.id == identity,
            AttributionStudy.user_id == account.owner_user_id,
            AttributionStudy.account_id == account.id,
        )
    )
    if row is None:
        raise LookupError("Attribution study unavailable")
    value = json.loads(row.policy_json)
    if digest(value) != row.id:
        raise ValueError("STUDY_POLICY_INTEGRITY_MISMATCH")
    return {"study_id": row.id, **value}


@accounting
def cluster_statistics(clusters, study_id, minimum):
    values = [
        Decimal(row["actual_net_pnl"]) / Decimal(row["capital"])
        - Decimal(row["control_net_pnl"]) / Decimal(row["capital"])
        for _, row in sorted(clusters.items())
        if Decimal(row["capital"]) > 0
    ]
    result = {
        "event_count": len(values),
        "paired_event_mean_return_difference": None,
        "confidence_interval": None,
        "method": "equal-event-seeded-bootstrap95-v1",
        "assumption": "between-event independence assumed; control modeled",
    }
    if len(values) < minimum:
        return result
    rng = random.Random(int(study_id[:16], 16))  # noqa: S311 - reproducible statistical resampling
    means = sorted(
        sum(rng.choices(values, k=len(values)), Decimal(0)) / len(values) for _ in range(2000)
    )
    result.update(
        paired_event_mean_return_difference=sum(values, Decimal(0)) / len(values),
        confidence_interval=[means[49], means[1949]],
    )
    return result


def study_observations(db, account, report, start):
    from app.services.analytics.attribution_archive import read_report

    reports, seen = [report], set()
    while datetime.fromisoformat(reports[0]["opening_observed_at"]) > start:
        child = reports[0]
        parent_id = child.get("opening_state_report_id")
        if parent_id is None:
            break
        if parent_id in seen or len(reports) >= 100:
            raise ValueError("STUDY_REPORT_CHAIN_BOUND_EXCEEDED")
        seen.add(parent_id)
        parent = read_report(db, account, parent_id)
        if (
            parent["closing_snapshot_id"] != child["opening_snapshot_id"]
            or parent["closing_observed_at"] != child["opening_observed_at"]
        ):
            raise ValueError("STUDY_OBSERVATION_CHAIN_GAP")
        reports.insert(0, parent)
    merged = {
        **report,
        "opening_observed_at": reports[0]["opening_observed_at"],
        "history_evidence": reports[0]["history_evidence"],
        "fill_evidence": [fill for item in reports for fill in item["fill_evidence"]],
        "realized_allocations": [row for item in reports for row in item["realized_allocations"]],
        "input_hash": digest([item["input_hash"] for item in reports]),
        "status": "reconciled"
        if all(item["status"] == "reconciled" for item in reports)
        else "abstain",
        "chain_reconciliation": {
            "scope_reconciled": all(
                (item.get("chain_reconciliation") or {}).get("scope_reconciled") for item in reports
            )
        },
    }
    return merged, reports


@accounting
def scoped_nav_drawdown(reports, max_gap_hours):
    first = reports[0]
    opening_nav = first["opening_mark"]["observed_nav"]
    result = {
        "method": "external-flow-adjusted-scoped-NAV-observed-points-v1",
        "maximum_drawdown": None,
        "points": [],
        "reason": None,
        "full_wallet_nav": False,
    }
    if opening_nav is None:
        result["reason"] = "SCOPED_NAV_MARKS_UNAVAILABLE"
        return result
    peak = Decimal(opening_nav)
    cumulative_flow, maximum = Decimal(0), Decimal(0)
    previous = datetime.fromisoformat(first["opening_observed_at"])
    if peak <= 0:
        result["reason"] = "SCOPED_NAV_START_NOT_POSITIVE"
        return result
    result["points"].append({"at": previous.isoformat(), "adjusted_nav": str(peak)})
    for item in reports:
        current = datetime.fromisoformat(item["closing_observed_at"])
        nav = item["closing_mark"]["observed_nav"]
        if (
            current <= previous
            or current - previous > timedelta(hours=max_gap_hours)
            or nav is None
        ):
            result["reason"] = "SCOPED_NAV_OBSERVATION_GAP_OR_MISSING_MARK"
            return result
        cumulative_flow += sum(
            (
                Decimal(flow["cashflow"])
                for flow in item["chain_reconciliation"]["external_cashflows"]
            ),
            Decimal(0),
        )
        adjusted = Decimal(nav) - cumulative_flow
        peak = max(peak, adjusted)
        maximum = max(maximum, (peak - adjusted) / peak)
        result["points"].append(
            {
                "at": current.isoformat(),
                "adjusted_nav": str(adjusted),
                "unattributed_external_cashflow_removed": str(cumulative_flow),
            }
        )
        previous = current
    result["maximum_drawdown"] = str(maximum)
    result["interpretation"] = (
        "observed-point lower bound; intraperiod peaks and off-scope assets unknown"
    )
    return result


@accounting
def evaluate_study(db, account, study_id, report):
    policy = read_study(db, account, study_id)
    start, end, deadline = (
        datetime.fromisoformat(policy[key])
        for key in ("registered_at", "enrollment_end", "settlement_deadline")
    )
    report, observations = study_observations(db, account, report, start)
    result = {
        "study_id": study_id,
        "policy": policy,
        "status": "incomplete",
        "reasons": [],
        "release_decision": "ABSTAIN_PENDING_FORWARD_EVIDENCE",
        "profitability_claim": False,
        "pairs": [],
        "zero_fill_decisions": [],
        "clusters": {},
        "confidence_interval": None,
        "actual_account_nav_drawdown": None,
        "nav_drawdown_reason": "CONSISTENT_CHAIN_NAV_SERIES_REQUIRED",
    }
    if (
        utc_now() < deadline
        or datetime.fromisoformat(report["opening_observed_at"]) > start
        or datetime.fromisoformat(report["closing_observed_at"]) < deadline
    ):
        result["reasons"].append("FORWARD_WINDOW_NOT_FULLY_OBSERVED")
    if (
        not (report.get("chain_reconciliation") or {}).get("scope_reconciled")
        or report["status"] != "reconciled"
    ):
        result["reasons"].append("FORWARD_FINANCIAL_EVIDENCE_INCOMPLETE")
    if policy["unmapped_development_decision_ids"] or policy.get("unmapped_development_trade_ids"):
        result["reasons"].append("DEVELOPMENT_EVENT_DISJOINTNESS_UNVERIFIED")
    if implementation_fingerprint() != policy["implementation_fingerprint"]:
        result["reasons"].append("EVALUATOR_IMPLEMENTATION_CHANGED")
    if not policy["implementation_fingerprint"] or not policy.get("configuration_fingerprint"):
        result["reasons"].append("STRATEGY_IMPLEMENTATION_UNVERIFIED")
    allocations = {}
    for row in report["realized_allocations"]:
        allocations.setdefault(row["entry_id"], []).append(row)
    fills = report["history_evidence"] + report["fill_evidence"]
    consumed, selected_ids, selected_events = {}, [], set()
    decisions = []
    development_events = set(policy["development_events"])
    for row in intents(db, account, end):
        value = json.loads(row.payload_json)
        if (
            aware(row.occurred_at) < start
            and value.get("strategy") == policy["strategy"]
            and row.id not in policy.get("development_decision_ids", [])
        ):
            try:
                event, _ = event_identity(db, value["condition_id"], start)
                development_events.add(event)
            except (EvidenceUnavailable, KeyError):
                result["reasons"].append("LATE_DEVELOPMENT_HISTORY_UNVERIFIED")
        if (
            aware(row.occurred_at) < start
            or value.get("strategy") != policy["strategy"]
            or value.get("side") != "BUY"
        ):
            continue
        decisions.append((row, value))
    if not decisions:
        result["reasons"].append("NO_PROSPECTIVE_ENTRY_DECISIONS")
    for row, intent in decisions:
        selected_ids.append(row.id)
        if intent.get("implementation_fingerprint") != policy[
            "implementation_fingerprint"
        ] or intent.get("configuration_fingerprint") != policy.get("configuration_fingerprint"):
            result["reasons"].append("STRATEGY_IMPLEMENTATION_CHANGED_OR_UNRECORDED")
        try:
            event, event_capture = event_identity(
                db, intent["condition_id"], aware(row.occurred_at)
            )
            if event in development_events:
                raise EvidenceUnavailable("DEVELOPMENT_HOLDOUT_EVENT_OVERLAP")
            selected_events.add(event)
            entries = [
                fill
                for fill in fills
                if fill.get("intent_source_id") == row.id and fill["side"] == "BUY"
            ]
            if not entries:
                transitions = db.scalars(
                    select(AppEvent)
                    .where(
                        AppEvent.user_id == account.owner_user_id,
                        account_events(account),
                        AppEvent.source == "live_gateway",
                        AppEvent.aggregate_id == row.aggregate_id,
                        AppEvent.event_type == "order_state_changed",
                        AppEvent.occurred_at <= deadline,
                    )
                    .limit(100)
                ).all()
                terminal = [json.loads(item.payload_json).get("after", {}) for item in transitions]
                if not any(
                    item.get("state") in {"REJECTED", "CANCELLED"}
                    and Decimal(str(item.get("size_matched", 0))) == 0
                    for item in terminal
                ):
                    raise EvidenceUnavailable("NO_FILL_DECISION_TERMINAL_EVIDENCE_MISSING")
                budget = Decimal(str(intent.get("notional")))
                if not budget.is_finite() or budget <= 0:
                    raise EvidenceUnavailable("NO_FILL_PRINCIPAL_UNVERIFIED")
                control = paired_control(
                    db,
                    {
                        "token_id": intent["token_id"],
                        "condition_id": intent["condition_id"],
                        "decision_at": aware(row.occurred_at).isoformat(),
                    },
                    deadline,
                    budget,
                    consumed,
                )
                cluster = result["clusters"].setdefault(
                    event,
                    {
                        "capital": Decimal(0),
                        "actual_net_pnl": Decimal(0),
                        "control_net_pnl": Decimal(0),
                        "executed_entries": 0,
                    },
                )
                cluster["capital"] += budget
                cluster["control_net_pnl"] += Decimal(control["net_pnl"])
                if cluster["capital"] > Decimal(policy["max_capital_per_event"]):
                    raise EvidenceUnavailable("PREREGISTERED_EVENT_CAPITAL_BOUND_EXCEEDED")
                result["zero_fill_decisions"].append(
                    {
                        "intent_source_id": row.id,
                        "event_id": event,
                        "actual_net_pnl": "0",
                        "capital_deployed": "0",
                        "unspent_capital": str(budget),
                        "control": control,
                    }
                )
            for fill in sorted(entries, key=lambda item: (item["matched_at"], item["fill_id"])):
                closed = allocations.get(fill["source_id"], [])
                if sum((Decimal(item["shares"]) for item in closed), Decimal(0)) != Decimal(
                    fill["shares"]
                ):
                    raise EvidenceUnavailable("PROSPECTIVE_POSITION_NOT_FULLY_CLOSED")
                if fill["fee_usdc"] is None or any(
                    item["entry_fee"] is None or item["exit_fee"] is None for item in closed
                ):
                    raise EvidenceUnavailable("PROSPECTIVE_COSTS_UNVERIFIED")
                capital = Decimal(fill["notional"]) + Decimal(fill["fee_usdc"])
                actual = sum(
                    (
                        Decimal(item["gross_pnl"])
                        - Decimal(item["entry_fee"])
                        - Decimal(item["exit_fee"])
                        for item in closed
                    ),
                    Decimal(0),
                )
                control = paired_control(db, fill, deadline, capital, consumed)
                cluster = result["clusters"].setdefault(
                    event,
                    {
                        "capital": Decimal(0),
                        "actual_net_pnl": Decimal(0),
                        "control_net_pnl": Decimal(0),
                        "executed_entries": 0,
                    },
                )
                cluster["executed_entries"] += 1
                cluster["capital"] += capital
                cluster["actual_net_pnl"] += actual
                cluster["control_net_pnl"] += Decimal(control["net_pnl"])
                if cluster["capital"] > Decimal(policy["max_capital_per_event"]):
                    raise EvidenceUnavailable("PREREGISTERED_EVENT_CAPITAL_BOUND_EXCEEDED")
                result["pairs"].append(
                    {
                        "intent_source_id": row.id,
                        "fill_source_id": fill["source_id"],
                        "event_id": event,
                        "event_capture_id": event_capture,
                        "actual_net_pnl": actual,
                        "control": control,
                    }
                )
        except (EvidenceUnavailable, ValueError, ArithmeticError) as exc:
            result["reasons"].append(str(exc))
    nav = scoped_nav_drawdown(observations, policy["max_observation_gap_hours"])
    result["scoped_nav_drawdown"] = nav
    if nav["reason"]:
        result["reasons"].append(nav["reason"])
    stats = cluster_statistics(result["clusters"], study_id, policy["minimum_events"])
    result.update(stats)
    result["eligible_entry_decision_count"] = len(decisions)
    result["observed_event_count"] = len(selected_events)
    result["executed_event_count"] = sum(
        row.get("executed_entries", 0) > 0 for row in result["clusters"].values()
    )
    if result["executed_event_count"] < policy["minimum_executed_event_clusters"]:
        result["reasons"].append("INSUFFICIENT_EXECUTED_EVENT_CLUSTERS")
    if stats["confidence_interval"] is None:
        result["reasons"].append("INSUFFICIENT_DISJOINT_EVENT_CLUSTERS")
    if not result["reasons"]:
        actual_total = sum(
            (Decimal(row["actual_net_pnl"]) for row in result["clusters"].values()), Decimal(0)
        )
        result.update(
            status="complete",
            release_decision="SHADOW_EVIDENCE_SUPPORTS_SCOPED_REVIEW"
            if stats["confidence_interval"][0] > 0 and actual_total > 0
            else "REJECT_SCOPED_NET_PERFORMANCE",
        )
    if result["status"] == "complete" and Decimal(nav["maximum_drawdown"]) > Decimal(
        policy["max_scoped_drawdown"]
    ):
        result["release_decision"] = "REJECT_PREREGISTERED_SCOPED_DRAWDOWN_LIMIT"
    result["reasons"] = sorted(set(result["reasons"]))
    result["input_hash"] = digest(
        {
            "policy": policy,
            "report": report["input_hash"],
            "decision_ids": selected_ids,
            "pairs": result["pairs"],
            "clusters": result["clusters"],
            "scoped_nav_drawdown": nav,
            "reasons": result["reasons"],
        }
    )
    return json.loads(canonical(result))
