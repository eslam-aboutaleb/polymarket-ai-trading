"""Portfolio risk report API route.

A dedicated router rather than a route inside ``portfolio.py``: the report is
a read-only aggregation over the wallet's positions plus the user's live risk
limits, and it must not grow the portfolio module that the Dashboard summary
depends on.

The deterministic half of the report always succeeds. The LLM narrative is
best-effort — when the analysis backend is down the response carries
``narrative: null`` with the metrics intact, so a risk page never 500s.
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user_from_token
from app.models.user_settings import UserSettings
from app.security.credential_store import CredentialStoreError, load_wallet_credentials
from app.services import portfolio_risk_service
from app.services.polymarket_service import get_polymarket_service
from app.utils.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


# --- Response Schemas ---


class RiskReportPosition(BaseModel):
    """One held position with its deterministic risk figures."""

    market_title: str
    condition_id: str = ""
    outcome: str = ""
    size: float
    avg_price: float
    current_price: float
    value: float
    weight: float
    cost_basis: float
    loss_bound: float
    concentration_flag: bool


class RiskCluster(BaseModel):
    """A deterministic group of positions that move together."""

    label: str
    members: list[str]
    weight: float


class RiskCatalyst(BaseModel):
    """An upcoming market resolution date."""

    market_title: str
    end_date: str
    days_to_expiry: int


class RiskReportMetrics(BaseModel):
    """Deterministic portfolio risk metrics — computed, never narrated."""

    portfolio_value: float
    usdc_balance: float
    unrealized_pnl: float
    positions_count: int
    loss_bound: float
    max_position_weight: float
    concentration_flags: int
    positions: list[RiskReportPosition] = []
    clusters: list[RiskCluster] = []
    catalysts: list[RiskCatalyst] = []
    risk_limits: dict[str, float | str | bool] = {}


class RiskNarrative(BaseModel):
    """The LLM's narrative half of the report (null when unavailable)."""

    overall_risk_rating: str = ""
    var_estimate: str = ""
    concentration_flags: list[str] = []
    correlated_clusters: list[str] = []
    tail_scenarios: list[str] = []
    hedges: list[str] = []


class RiskReportResponse(BaseModel):
    """Portfolio risk report: deterministic metrics + optional narrative."""

    metrics: RiskReportMetrics
    narrative: RiskNarrative | None = None
    generated_at: str


# --- Endpoints ---


@router.get("/risk-report", response_model=RiskReportResponse)
async def get_risk_report(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Get the authenticated user's portfolio risk report.

    Returns concentration weights, loss bound, correlated clusters, upcoming
    catalysts and the live risk limits, plus an AI narrative when the analysis
    backend is reachable.
    """
    wallet_address = current_user["wallet_address"]
    service = get_polymarket_service()

    # Stored private key / CLOB creds unlock the authenticated position path.
    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError:
        stored = None
    pk = stored["private_key"] if stored else None
    creds = stored.get("clob_creds") if stored else None

    user_settings = (
        db.query(UserSettings).filter(UserSettings.user_id == current_user["user_id"]).first()
    )

    return await portfolio_risk_service.build_risk_report(
        service,
        wallet_address,
        private_key=pk,
        clob_creds=creds,
        user_settings=user_settings,
        # The narrative cache is keyed per user: one wallet's narrative must
        # never be served to another.
        user_id=current_user["user_id"],
    )
