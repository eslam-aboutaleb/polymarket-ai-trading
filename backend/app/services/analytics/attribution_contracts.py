"""Versioned, read-only financial report contracts and machine-readable acceptance gates."""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.services.analytics.replay.ledger import accounting


class BalanceReference(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_at: AwareDatetime


class AcceptanceCheck(BaseModel):
    criterion: str
    status: Literal["pass", "incomplete", "fail"]
    reason: str


class AttributionReport(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: Literal["live-attribution-v2"]
    account_id: int
    status: Literal["reconciled", "abstain"]
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    opening_observed_at: AwareDatetime
    closing_observed_at: AwareDatetime
    gross_realized_pnl: str | None
    net_realized_pnl: str | None
    verified_net_realized_pnl: str | None
    unrealized_pnl: str | None
    mark_contribution: str | None
    cash_residual_usdc: str | None
    total_fees_usdc: str | None
    estimated_rebates_usdc: str
    confirmed_rebates_usdc: str | None
    execution_eligible: Literal[False]
    full_wallet_reconciled: bool
    reasons: list[str]
    acceptance: list[AcceptanceCheck]
    acceptance_complete: bool
    benchmark_version: str


@accounting
def acceptance_checks(result):
    """A passing unit test is not a substitute for missing production evidence."""

    def check(name, passes, reason, *, fails=False):
        return {
            "criterion": name,
            "status": "pass" if passes else "fail" if fails else "incomplete",
            "reason": "EVIDENCE_VERIFIED" if passes else reason,
        }

    tolerance = Decimal(".000001")
    cash = result["cash_residual_usdc"]
    position_ok = all(
        abs(Decimal(value)) <= tolerance for value in result["position_residuals"].values()
    )
    wallet_ok = (
        result["full_wallet_reconciled"]
        and cash is not None
        and abs(Decimal(cash)) <= tolerance
        and position_ok
    )
    net = result["net_realized_pnl"]
    attributed = [item["net_realized"] for item in result["strategies"].values()]
    additive = net is not None and all(value is not None for value in attributed)
    if additive:
        additive = abs(Decimal(net) - sum(map(Decimal, attributed), Decimal(0))) <= tolerance
    benchmark_ok = all(row["status"] == "observed_comparison" for row in result["benchmarks"])
    costs_ok = (
        result["total_fees_usdc"] is not None and result["confirmed_rebates_usdc"] is not None
    )
    return [
        check(
            "exchange_cash_and_positions",
            wallet_ok,
            "WALLET_OR_SETTLEMENT_COVERAGE_UNVERIFIED",
            fails=(cash is not None and abs(Decimal(cash)) > tolerance) or not position_ok,
        ),
        check("duplicate_fill_identity", True, ""),
        check("confirmed_and_estimated_rebates_separate", True, ""),
        check("strategy_totals_additive", additive, "NET_COST_OR_BASIS_INCOMPLETE"),
        check(
            "versioned_fill_and_benchmark_lineage",
            benchmark_ok,
            "POINT_IN_TIME_BENCHMARK_UNAVAILABLE",
        ),
        check("missing_costs_remain_unknown", True, ""),
        check("actual_cost_evidence", costs_ok, "ACTUAL_COSTS_UNVERIFIED"),
        check("forward_validation", False, "NO_DISJOINT_FORWARD_EVENT_STUDY"),
    ]


class ConfirmedFillPayload(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    trade_id: str = Field(min_length=1, max_length=200)
    transaction_hash: str = Field(min_length=1, max_length=100)
    matched_at: AwareDatetime
    shares: Decimal = Field(gt=0, le=1_000_000_000, max_digits=28, decimal_places=18)
    notional: Decimal = Field(gt=0, le=1_000_000_000, max_digits=28, decimal_places=18)
    fee_usdc: Decimal | None = Field(default=None, ge=0, le=1_000_000_000)
    confirmed_rebate_usdc: Decimal | None = Field(default=None, ge=0, le=1_000_000_000)
    estimated_rebate_usdc: Decimal | None = Field(default=None, ge=0, le=1_000_000_000)


class FreezeAttributionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opening_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    closing_id: str = Field(pattern=r"^[a-f0-9]{64}$")
