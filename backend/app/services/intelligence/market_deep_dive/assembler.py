"""Single coordinator over the existing agent, forecast store and book model.

``run_structured_analysis`` yields typed SSE frames and finishes with the
validated result. It reuses the user-selected provider client handed in by the
route (no second orchestrator) and closes it, and any in-flight capture task, if
the consumer cancels.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.services.intelligence.market_deep_dive import analysis_runs
from app.services.intelligence.market_deep_dive.claim_validation import (
    parse_draft,
    validate_evidence_links,
)
from app.services.intelligence.market_deep_dive.context_builder import (
    CanonicalMarket,
    canonical_market_context,
)
from app.services.intelligence.market_deep_dive.decision_policy import enforce_decision_policy
from app.services.intelligence.market_deep_dive.evidence_adapter import (
    collect_evidence,
    drop_future_evidence,
)
from app.services.intelligence.market_deep_dive.forecast_adapter import build_forecast_display
from app.services.intelligence.market_deep_dive.prompt import deep_dive_prompt
from app.services.intelligence.market_deep_dive.quote_cost import (
    apply_net_ev,
    build_quote_and_cost,
    capture_book_values,
)
from app.services.intelligence.market_deep_dive.schemas import (
    SCHEMA_VERSION,
    Completeness,
    DeepDiveRequest,
    ForecastDisplay,
    MarketDeepDiveResult,
    QuoteAndCost,
)
from app.services.intelligence.market_research import _search_excerpts
from app.services.intelligence.prompt_version import current_prompt_version
from app.services.intelligence.resolution_rules import record_rule_snapshot
from app.services.intelligence.social_evidence.evidence import posts_to_evidence
from app.services.intelligence.social_evidence.providers import parse_status_url
from app.services.intelligence.social_evidence.quota import SocialQuota
from app.services.intelligence.social_evidence.service import collect_social
from app.services.market_data.replay_capture import PublicVenueCapture

logger = logging.getLogger(__name__)

POLICY_VERSION = "deep-dive-policy-v1"
RESEARCH_CONTRACT = "market-research-v1"

ClientFactory = Callable[[], Awaitable[tuple[Any, dict[str, Any] | None]]]


def _frame(kind: str, analysis_id: str, **data: Any) -> dict[str, Any]:
    return {"type": kind, "analysis_id": analysis_id, "schema_version": SCHEMA_VERSION, **data}


def _server_invalidations(market: CanonicalMarket) -> list[str]:
    return [
        "The resolution text, cutoff or YES/NO token mapping changes "
        f"(rules hash {market.identity.rules_hash[:12]}).",
        "The quote becomes stale, or displayed ask depth no longer covers the requested size.",
    ]


@dataclass
class SocialConfig:
    mode: str
    quota: SocialQuota
    bearer_token: str | None = None
    client: httpx.AsyncClient | None = None


def _indexed_x_items(research_context: str) -> list[dict]:
    items = []
    for url, excerpts in _search_excerpts(research_context or "").items():
        if parse_status_url(url) and excerpts:
            items.append({"url": url, "snippet": excerpts[0]})
    return items


async def run_structured_analysis(
    request: DeepDiveRequest,
    *,
    venue: PublicVenueCapture,
    client_factory: ClientFactory,
    db: Session | None,
    user_id: int | None,
    research_timeout: float,
    retention_days: int = 365,
    social: SocialConfig | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AsyncIterator[dict[str, Any]]:
    started = time.monotonic()
    analysis_id = str(uuid4())
    market = await canonical_market_context(venue, request.condition_id, request.selected_token_id)
    first_fact_ms = int((time.monotonic() - started) * 1000)
    yield _frame(
        "context_ready",
        analysis_id,
        identity=market.identity.model_dump(mode="json"),
        resolution=market.resolution.model_dump(mode="json"),
        indicative_prices={
            o.outcome: None if o.indicative_price is None else str(o.indicative_price)
            for o in market.context.outcomes
        },
        warnings=market.context.warnings,
    )

    failures: list[str] = []
    client = None
    provider = model = None
    research_text, research_context = "", ""
    quote_task = asyncio.create_task(capture_book_values(venue, market))
    try:
        run_research = (
            request.include_research
            and bool(market.context.rules.text.strip())
            and market.identity.market_status == "active"
        )
        if run_research:
            yield _frame("research_started", analysis_id, transport="unary")
            try:
                client, llm_config = await client_factory()
                async with asyncio.timeout(research_timeout):
                    response = await client.analyze_market(
                        market_title=market.context.question,
                        market_description=market.context.rules.text,
                        research_instructions=deep_dive_prompt(market.context),
                        # Indicative prices only feed the provider's text prompt; the
                        # result never treats them as executable quotes.
                        yes_price=next(
                            (
                                o.indicative_price
                                for o in market.context.outcomes
                                if o.outcome == "YES" and o.indicative_price is not None
                            ),
                            0.0,
                        ),
                        no_price=next(
                            (
                                o.indicative_price
                                for o in market.context.outcomes
                                if o.outcome == "NO" and o.indicative_price is not None
                            ),
                            0.0,
                        ),
                        volume_24h=0,
                        end_date=market.context.rules.market_end_date or "",
                        include_research=True,
                        llm_config=llm_config,
                    )
                metadata = response.get("metadata") or {}
                if metadata.get("research_contract") != RESEARCH_CONTRACT:
                    failures.append("PROVIDER_CONTRACT_UNSUPPORTED")
                else:
                    research_text = response.get("analysis", "")
                    research_context = response.get("research_context", "")
                    provider, model = metadata.get("provider"), metadata.get("model")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Structured research provider call failed")
                failures.append("RESEARCH_UNAVAILABLE")
        elif request.include_research and market.identity.market_status != "active":
            failures.append("RESEARCH_SKIPPED_MARKET_NOT_ACTIVE")

        try:
            quote_values, capture_failures = await quote_task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Quote capture failed")
            quote_values, capture_failures = [], [o.token_id for o in market.context.outcomes]
            failures.append("QUOTE_CAPTURE_FAILED")

        as_of = clock()  # after capture, so every receipt is visible at the cutoff
        evidence = collect_evidence(market.context, research_context, as_of)
        social_coverage = None
        if social is not None and social.mode != "off" and db is not None:
            try:
                social_posts, social_coverage = await collect_social(
                    db,
                    mode=social.mode,
                    query=market.context.question,
                    as_of=as_of,
                    indexed_items=_indexed_x_items(research_context),
                    user_id=user_id,
                    bearer_token=social.bearer_token,
                    quota=social.quota,
                    client=social.client,
                )
                evidence.extend(posts_to_evidence(social_posts, as_of))
            except Exception:
                logger.exception("Social evidence failed")
                failures.append("SOCIAL_EVIDENCE_FAILED")
        evidence, rejected_future = drop_future_evidence(evidence, as_of)
        yield _frame("evidence_collected", analysis_id, evidence_count=len(evidence))

        draft = parse_draft(research_text) if research_text else None
        claims, flags = ([], [])
        if draft is not None:
            claims, flags = validate_evidence_links(draft, evidence)
        yield _frame(
            "claims_checked",
            analysis_id,
            claims=len(claims),
            attributed=sum(c.attribution == "attributed" for c in claims),
        )

        yes_price = next(
            (o.indicative_price for o in market.context.outcomes if o.outcome == "YES"), None
        )
        forecast: ForecastDisplay | None = None
        if db is not None:
            try:
                # Public rule version, recorded idempotently so later forecasts can bind to it.
                record_rule_snapshot(db, market.spec)
                db.commit()
                forecast = build_forecast_display(
                    db,
                    market.spec,
                    as_of=as_of,
                    market_benchmark_p=None if yes_price is None else Decimal(str(yes_price)),
                    market_benchmark_observed_at=market.context.observed_at,
                )
            except SQLAlchemyError:
                db.rollback()
                failures.append("FORECAST_STORE_UNAVAILABLE")
        cost: QuoteAndCost | None
        if quote_values:
            cost = build_quote_and_cost(
                market, quote_values, capture_failures, request.requested_shares, as_of=as_of
            )
            cost = apply_net_ev(
                cost,
                market,
                forecast,
                request.requested_shares,
                request.requested_shares,
                as_of=as_of,
            )
        else:
            cost = QuoteAndCost(
                token_id=market.identity.selected_token_id,
                indicative_prices={
                    o.outcome: None
                    if o.indicative_price is None
                    else Decimal(str(o.indicative_price))
                    for o in market.context.outcomes
                },
                requested_size=request.requested_shares,
                validity_status="unavailable",
                validity_reasons=["BOOK_CAPTURE_UNAVAILABLE"],
            )

        extra = list(flags)
        if rejected_future:
            extra.append("FUTURE_EVIDENCE_REJECTED")
        policy = enforce_decision_policy(
            identity=market.identity,
            resolution=market.resolution,
            claims=claims,
            evidence=evidence,
            forecast=forecast,
            cost=cost,
            upstream_failures=failures,
            requested_shares_given=request.requested_shares is not None,
            draft_valid=draft is not None,
            extra_codes=extra,
        )
        search_evidence = [e for e in evidence if e.source_family == "search_excerpt"]
        sections: dict[str, Any] = {
            "market": "complete",
            "resolution": "complete" if market.resolution.verification == "verified" else "partial",
            "evidence": "failed"
            if "RESEARCH_UNAVAILABLE" in failures or "PROVIDER_CONTRACT_UNSUPPORTED" in failures
            else ("complete" if search_evidence else "missing"),
            "claims": "complete"
            if draft is not None and claims
            else ("missing" if draft is None else "partial"),
            "forecast": "missing"
            if forecast is None or forecast.validity_status == "unavailable"
            else ("complete" if forecast.validity_status == "validated" else "partial"),
            **(
                {
                    "social": "complete"
                    if social_coverage and social_coverage.posts_used
                    else "missing"
                }
                if social_coverage
                else {}
            ),
            "price_and_cost": "complete"
            if cost.validity_status == "executable_modeled"
            else ("failed" if "QUOTE_CAPTURE_FAILED" in failures else "partial"),
        }
        missing = [
            "Settlement rule interpretation, cutoff and oracle require independent review."
            if market.resolution.verification != "verified"
            else "Rule verification covers parsed fields only; read the original text.",
            "Search excerpts establish attribution only; full source truth is not verified.",
        ]
        if forecast is None or forecast.validity_status != "validated":
            missing.append("No validated calibrated probability exists for this market.")
        if cost.validity_status != "executable_modeled":
            missing.append("Executable cost is not established; prices shown are indicative.")
        if draft:
            missing.extend(item[:1000] for item in draft.missing_information)
        now = clock()
        key_insights = (
            [i[:300] for i in draft.key_insights[:5]]
            if draft and draft.key_insights
            else [c.statement[:300] for c in claims if c.attribution == "attributed"][:3]
        )
        result = MarketDeepDiveResult(
            analysis_id=analysis_id,
            identity=market.identity,
            resolution=market.resolution,
            summary=draft.summary if draft else "",
            key_insights=key_insights,
            evidence=evidence,
            claims=claims,
            forecast=forecast,
            price_and_cost=cost,
            social=social_coverage,
            risks=draft.risks if draft else [],
            invalidation_conditions=[
                *(draft.invalidation_conditions if draft else []),
                *_server_invalidations(market),
            ],
            missing_information=list(dict.fromkeys(missing)),
            decision=policy.decision,
            reason_codes=policy.reason_codes,
            completeness=Completeness(
                sections=sections, upstream_failures=failures, persisted=False
            ),
            versions={
                "schema_version": SCHEMA_VERSION,
                "policy_version": POLICY_VERSION,
                "prompt_version": current_prompt_version(),
                "rule_skill_version": market.spec.skill_version,
                "provider": provider,
                "model": model,
            },
            created_at=now,
        )
        if db is not None and user_id is not None:
            try:
                persisted = result.model_copy(
                    update={
                        "completeness": result.completeness.model_copy(update={"persisted": True})
                    }
                )
                analysis_runs.persist_run(
                    db,
                    persisted,
                    user_id,
                    provider=provider,
                    model=model,
                    first_fact_ms=first_fact_ms,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    retention_days=retention_days,
                )
                db.commit()
                result = persisted
            except SQLAlchemyError:
                db.rollback()
                logger.exception("Analysis run could not be persisted")
        yield _frame("analysis_completed", analysis_id, result=result.model_dump(mode="json"))
    finally:
        if not quote_task.done():
            quote_task.cancel()
        if client is not None:
            await client.close()


async def build_structured_market_analysis(
    request: DeepDiveRequest, **kwargs: Any
) -> MarketDeepDiveResult:
    """Non-streaming convenience wrapper over the same pipeline."""
    result = None
    async for event in run_structured_analysis(request, **kwargs):
        if event["type"] == "analysis_completed":
            result = MarketDeepDiveResult.model_validate(event["result"])
    if result is None:
        raise RuntimeError("Analysis ended without a result")
    return result
