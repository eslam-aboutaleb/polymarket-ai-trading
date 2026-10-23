"""Read-only, source-attributed market context; no inferred settlement semantics.

A CLOB response identifies the tokens and supplies the original rules. Its
retrieval time does not establish quote freshness, nor does its end date prove
the settlement cutoff. Keep those distinctions in the public contract.
"""

import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.market_data.polymarket_service import POLYMARKET_CLOB_API, PolymarketService
from app.utils.time import utc_now


class OutcomeContext(BaseModel):
    token_id: str
    outcome: Literal["YES", "NO"]
    indicative_price: float | None = None


class RuleEvidence(BaseModel):
    text: str
    source_url: str
    content_hash: str
    verification: Literal["source_text_only"] = "source_text_only"
    market_end_date: str | None = None
    settlement_cutoff: None = None
    oracle: None = None


class MarketContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["market-context-v1"] = "market-context-v1"
    condition_id: str
    question: str
    snapshot_id: str
    observed_at: datetime
    source_url: str
    rules: RuleEvidence
    outcomes: list[OutcomeContext]
    closed: bool | None
    active: bool | None
    quote_quality: Literal["indicative", "unavailable"]
    quote_observed_at: None = None
    warnings: list[str] = Field(default_factory=list)


class MarketIdentityError(ValueError):
    """The source cannot prove this is the requested binary market."""


def _price(raw: Any) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    return float(value) if value.is_finite() and 0 <= value <= 1 else None


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def build_market_context(
    condition_id: str, raw: dict[str, Any], observed_at: datetime
) -> MarketContext:
    """Normalize only explicit source fields, rejecting ambiguous token mapping."""
    source_id = raw.get("condition_id") if isinstance(raw, dict) else None
    if not isinstance(source_id, str) or source_id.lower() != condition_id.lower():
        raise MarketIdentityError("Market source returned a different condition ID")
    question = raw.get("question")
    tokens = raw.get("tokens")
    if not isinstance(question, str) or not question.strip():
        raise MarketIdentityError("Market source has no question")
    if not isinstance(tokens, list) or len(tokens) != 2:
        raise MarketIdentityError("Only explicitly mapped binary YES/NO markets are supported")
    outcomes = []
    for token in tokens:
        if not isinstance(token, dict):
            raise MarketIdentityError("Invalid outcome token")
        label = str(token.get("outcome", "")).strip().upper()
        token_id = token.get("token_id")
        if label not in ("YES", "NO") or not isinstance(token_id, str) or not token_id.isdecimal():
            raise MarketIdentityError("Ambiguous outcome-token mapping")
        outcomes.append(
            OutcomeContext(
                token_id=token_id, outcome=label, indicative_price=_price(token.get("price"))
            )
        )
    if len({o.outcome for o in outcomes}) != 2 or len({o.token_id for o in outcomes}) != 2:
        raise MarketIdentityError("Duplicate outcomes or token IDs")
    outcomes.sort(key=lambda o: o.outcome, reverse=True)
    source_url = f"{POLYMARKET_CLOB_API}/markets/{condition_id}"
    description = raw.get("description")
    rules_text = description if isinstance(description, str) else ""
    end_date = raw.get("end_date_iso")
    end_date = end_date if isinstance(end_date, str) and end_date.strip() else None
    # The hash binds the rules to their token interpretation. A changed source
    # description or reversed token map invalidates the previous identity.
    rule_hash = _digest(
        {
            "text": rules_text,
            "end_date": end_date,
            "tokens": [(o.outcome, o.token_id) for o in outcomes],
        }
    )
    warnings = [
        "Original rules are shown without automatic verification of their interpretation.",
        "Market end date is metadata, not a verified settlement cutoff.",
        "Token prices are indicative; quote time, depth and executable cost are unavailable.",
    ]
    if not rules_text.strip():
        warnings.append("Resolution rules are missing. Do not infer them from the market title.")
    closed = raw.get("closed") if isinstance(raw.get("closed"), bool) else None
    active = raw.get("active") if isinstance(raw.get("active"), bool) else None
    if closed is not False or active is not True:
        warnings.append("Market is closed, inactive, or its trading state is unknown.")
    return MarketContext(
        condition_id=condition_id.lower(),
        question=question,
        snapshot_id=_digest({"source": raw, "observed_at": observed_at.isoformat()}),
        observed_at=observed_at,
        source_url=source_url,
        rules=RuleEvidence(
            text=rules_text, source_url=source_url, content_hash=rule_hash, market_end_date=end_date
        ),
        outcomes=outcomes,
        closed=closed,
        active=active,
        quote_quality="indicative"
        if all(o.indicative_price is not None for o in outcomes)
        else "unavailable",
        warnings=warnings,
    )


async def fetch_market_context(service: PolymarketService, condition_id: str) -> MarketContext:
    raw = await service.get_market_outcome_tokens(condition_id)
    return build_market_context(condition_id, raw, utc_now())
