"""Research credibility scoring for the opportunity scan (plan 06).

``credibility_score`` used to be whatever the model felt like writing:
the ``market_scoring`` / ``event_scoring`` prompts ask for "how
credible/researchable is this market", the models answer with a default
50, and nothing grounded the number. The field was also rendered nowhere.

This module replaces it with something a reader can audit, in three
layers:

1. a **deterministic baseline** built only from evidence the backend can
   actually see during a scan — the live order-book whale snapshot,
   aggregate 24h volume/liquidity, and 24h price momentum — scored on
   **freshness**, **source diversity** and **corroboration**;
2. **one** ``research.synthesize_research`` call per scan batch (not per
   market — a scan covers up to 80 markets) that may move a group's
   baseline and supplies the ``search_summary`` prose;
3. **per-source** ``research.source_evaluation`` calls, budget-capped at
   ``MAX_SOURCE_EVALUATION_GROUPS`` groups / ``max_source_evaluations``
   calls per batch.

Every LLM layer is optional. A failed or unparseable answer leaves the
layer above it untouched and ends at the deterministic baseline, because
a scan that cannot reach the model still has to rank markets.

Tiering note: these calls are smuggled through the ``AnalyzeMarket``
RPC's ``market_description`` (the idiom every structured-verdict path in
``analysis_client`` uses), so they run under ``market_assessment``'s
STRONG tier until a real RPC exists; ``TASK_TIERS`` already reserves the
CHEAP tier for ``source_evaluation`` / ``synthesize_research`` /
``event_batch_scoring`` for that day.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# ``analysis_client`` owns the one prompt-store loader (with the same
# fallback behaviour when the store is unreadable), so rendering the
# ``source_evaluation`` template here goes through it instead of a second
# copy of the loader.
from app.grpc_clients.analysis_client import _load_prompts

logger = logging.getLogger(__name__)

# ── deterministic scoring constants ────────────────────────────

#: A source at or below this age is fully fresh; between here and
#: ``FRESHNESS_STALE_HOURS`` the freshness score decays linearly and is
#: zero beyond it. ``None`` age scores a neutral 0.5 instead of guessing.
FRESHNESS_FULL_HOURS = 6.0
FRESHNESS_STALE_HOURS = 72.0

#: Freshness / diversity / corroboration weights. They sum to 1.0.
CREDIBILITY_WEIGHTS: dict[str, float] = {
    "freshness": 0.35,
    "diversity": 0.30,
    "corroboration": 0.35,
}

#: Credibility for a group with no evidence at all, matching the scan's
#: own AI-failure fallback so both read the same way.
NO_EVIDENCE_CREDIBILITY = 30

#: Number of independent source families that counts as fully diverse.
DIVERSITY_FULL_DOMAINS = 4

# ── LLM blend weights ──────────────────────────────────────────

#: How far per-source ratings may pull a baseline.
SOURCE_EVALUATION_WEIGHT = 0.4

#: How far the batch synthesis may pull the (possibly source-adjusted)
#: credibility. Small on purpose: the synthesis reads the same evidence
#: digest the baseline was computed from.
SYNTHESIS_WEIGHT = 0.25

# ── batch budget constants ─────────────────────────────────────

#: Sources scored per group (a 20-option event would otherwise flood the
#: diversity signal with near-identical rows).
MAX_SOURCES_PER_GROUP = 8

#: Groups that receive per-source evaluation, and the hard cap on
#: ``source_evaluation`` calls per batch (the two together bound the
#: latency and cost of the credibility pass).
MAX_SOURCE_EVALUATION_GROUPS = 5
MAX_SOURCE_EVALUATIONS = 5

#: Groups represented in the synthesis digest.
MAX_DIGEST_GROUPS = 20

#: Length cap for every prose field handed to a prompt or stored as
#: ``search_summary``.
MAX_SUMMARY_CHARS = 400

#: Stances a source can take on "does YES resolve true?".
_STANCES = ("YES", "NO")

#: A 24h move must reach this relative size to count as a stance.
MOMENTUM_STANCE_THRESHOLD = 0.02

#: Field names the evidence builders look for on a raw market dict.
_VOLUME_KEYS = ("volume24hr", "volume_24hr", "_event_volume_24hr", "volumeNum")
_LIQUIDITY_KEYS = ("liquidityNum", "liquidity", "_event_liquidity")
_CHANGE_KEYS = ("oneDayPriceChange", "one_day_price_change", "price_change_24h", "change_24h")

#: What the synthesis call is asked about: it is one batch-wide call, so
#: the question is about the batch, not a single market.
SYNTHESIS_QUESTION = (
    "How credible is the evidence behind this batch of Polymarket easy-trade candidates?"
)

#: Digest instruction asking for one parseable line per group. The model
#: is free to ignore it; ``parse_synthesis_report`` falls back to a
#: single batch-level number and finally to the baseline.
_DIGEST_OUTPUT_CONTRACT = (
    "After your six sections, emit one line per group in the form: "
    "CREDIBILITY <key> <0-100 integer>"
)

# Regexes for parsing free-text LLM answers.
_PER_GROUP_RE = re.compile(
    r"(?im)^[\s\-*>#]*CREDIBILITY\s*[:\-]?\s*([A-Za-z0-9_.:\-]+)\s+(\d{1,3})\b"
)
_BATCH_RE = re.compile(r"(?i)credibility[^0-9\n]{0,24}(\d{1,3})")
_OVERALL_RE = re.compile(r"(?i)overall[^0-9]{0,24}(\d+(?:\.\d+)?)")
_RATING_RE = re.compile(r"(?<![\d.])(10|[1-9])(?![\d.])")
_CONSENSUS_RE = re.compile(
    r"(?is)key\s+consensus\s*:?\s*(.*?)(?:\n\s*(?:\d[.)]|disagreements|base\s+rate|bayesian|"
    r"calibrated|information\s+gaps)|$)"
)

# Sentinel key for a batch-level credibility value (no group owns it).
BATCH_KEY = "*"


@dataclass(frozen=True)
class EvidenceSource:
    """One independent evidence stream the scan can see for a group.

    ``domain`` is the independence unit for the diversity and
    corroboration signals: two sources that share a domain (e.g. the
    order book and the CLOB state) are one stream, not two.
    """

    name: str
    domain: str
    summary: str = ""
    age_hours: float | None = None
    stance: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable view used by the synthesis prompt."""
        return {
            "name": self.name,
            "domain": self.domain,
            "age_hours": self.age_hours,
            "stance": self.stance,
            "summary": self.summary[:MAX_SUMMARY_CHARS],
        }


@dataclass(frozen=True)
class GroupEvidence:
    """One event group's evidence bundle for the batch credibility pass."""

    key: str
    question: str
    markets: Sequence[Mapping[str, Any]] = ()
    smart_money: Mapping[str, Any] | None = None
    priority: float = 0.0


@dataclass(frozen=True)
class BatchCredibility:
    """Per-group outcome of the credibility pass."""

    credibility_score: int
    search_summary: str = ""


# ── deterministic scoring ──────────────────────────────────────


def _clamp(value: float) -> int:
    """Clamp a score into the 0-100 integer range the schema carries."""
    return int(max(0, min(100, round(value))))


def _to_float(value: Any) -> float | None:
    """Coerce a Gamma/Polymarket value to a finite float, else ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _first_number(market: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    """First parseable value among ``keys`` on a raw market dict."""
    for key in keys:
        if key not in market:
            continue
        number = _to_float(market.get(key))
        if number is not None:
            return number
    return None


def source_freshness(age_hours: float | None) -> float:
    """Freshness of one source in ``[0, 1]`` from its age in hours."""
    if age_hours is None:
        return 0.5
    if age_hours <= FRESHNESS_FULL_HOURS:
        return 1.0
    if age_hours >= FRESHNESS_STALE_HOURS:
        return 0.0
    span = FRESHNESS_STALE_HOURS - FRESHNESS_FULL_HOURS
    return 1.0 - (age_hours - FRESHNESS_FULL_HOURS) / span


def freshness_score(sources: Sequence[EvidenceSource]) -> float:
    """Mean freshness; unknown ages count as neutral rather than absent."""
    if not sources:
        return 0.5
    return sum(source_freshness(s.age_hours) for s in sources) / len(sources)


def diversity_score(sources: Sequence[EvidenceSource]) -> float:
    """Distinct source families, saturating at ``DIVERSITY_FULL_DOMAINS``."""
    domains = {s.domain for s in sources if s.domain}
    return min(len(domains), DIVERSITY_FULL_DOMAINS) / DIVERSITY_FULL_DOMAINS


def corroboration_score(sources: Sequence[EvidenceSource]) -> float:
    """How much the independent streams agree about the YES stance.

    A single stream (or several streams sharing one domain) cannot
    corroborate anything, so it scores a neutral 0.5 instead of the
    inflated 1.0 a naive share-of-stances would give.
    """
    stanced = [s for s in sources if s.stance in _STANCES]
    if not stanced:
        return 0.5
    yes = sum(1 for s in stanced if s.stance == "YES")
    no = len(stanced) - yes
    dominant = "YES" if yes > no else "NO"
    agreeing = yes if dominant == "YES" else no
    support = {s.domain for s in stanced if s.stance == dominant}
    if len(support) < 2:
        return 0.5
    return agreeing / len(stanced)


def credibility_from_sources(sources: Sequence[EvidenceSource]) -> int:
    """Deterministic 0-100 credibility baseline for one group."""
    if not sources:
        return NO_EVIDENCE_CREDIBILITY
    score = (
        CREDIBILITY_WEIGHTS["freshness"] * freshness_score(sources)
        + CREDIBILITY_WEIGHTS["diversity"] * diversity_score(sources)
        + CREDIBILITY_WEIGHTS["corroboration"] * corroboration_score(sources)
    )
    return _clamp(score * 100)


# ── evidence builders (backend data only) ──────────────────────


def _smart_money_source(smart_money: Mapping[str, Any] | None) -> EvidenceSource | None:
    """Live order-book whale positioning, the freshest stream available."""
    if not isinstance(smart_money, Mapping) or not smart_money:
        return None
    bias = str(smart_money.get("whale_bias") or "").strip().upper()
    context = str(smart_money.get("context_text") or "").strip()
    whale_count = _to_float(smart_money.get("whale_count"))
    if not bias and not context and whale_count is None:
        return None
    if not context:
        context = f"Whale orders detected: {whale_count}"
    return EvidenceSource(
        name="Order-book whale positioning",
        domain="polymarket.clob",
        summary=context,
        # Fetched from the live CLOB book during this scan.
        age_hours=0.0,
        stance=bias if bias in _STANCES else None,
    )


def _liquidity_source(markets: Sequence[Mapping[str, Any]]) -> EvidenceSource | None:
    """Aggregate 24h volume / liquidity across the group's options."""
    if not markets:
        return None
    volume = sum(_first_number(m, _VOLUME_KEYS) or 0.0 for m in markets)
    liquidity = sum(_first_number(m, _LIQUIDITY_KEYS) or 0.0 for m in markets)
    traded = volume > 0 or liquidity > 0
    return EvidenceSource(
        name="Market liquidity & 24h volume",
        domain="polymarket.market-data",
        summary=(
            f"24h volume ${volume:,.0f}; liquidity ${liquidity:,.0f}"
            if traded
            else "No 24h volume or liquidity reported"
        ),
        # Traded inside the last 24h window; when nothing traded the
        # age is unknown rather than assumed stale.
        age_hours=24.0 if traded else None,
    )


def _momentum_source(markets: Sequence[Mapping[str, Any]]) -> EvidenceSource | None:
    """24h price momentum, the one stream that can disagree with whales."""
    largest: float | None = None
    for market in markets:
        change = _first_number(market, _CHANGE_KEYS)
        if change is None:
            continue
        if largest is None or abs(change) > abs(largest):
            largest = change
    if largest is None:
        return None
    if largest > MOMENTUM_STANCE_THRESHOLD:
        stance: str | None = "YES"
    elif largest < -MOMENTUM_STANCE_THRESHOLD:
        stance = "NO"
    else:
        stance = None
    return EvidenceSource(
        name="24h price momentum",
        domain="polymarket.price-action",
        summary=f"largest 24h move {largest:+.2%}",
        # Midpoint of the 24h window the reading comes from.
        age_hours=12.0,
        stance=stance,
    )


def build_scan_sources(
    markets: Sequence[Mapping[str, Any]],
    smart_money: Mapping[str, Any] | None = None,
) -> list[EvidenceSource]:
    """Every evidence stream the backend can honestly produce for a group.

    Deliberately limited to backend data: the scan path runs the easy
    -trade batch calls with ``include_research=False``, so there is no
    web-research corpus to score here. Freshness therefore measures data
    recency (live order book vs a 24h window), and corroboration measures
    agreement between independent streams (whale positioning vs price
    momentum) rather than between news outlets.
    """
    sources: list[EvidenceSource] = []
    whales = _smart_money_source(smart_money)
    if whales is not None:
        sources.append(whales)
    markets = list(markets or [])
    if not markets:
        return sources
    liquidity = _liquidity_source(markets)
    if liquidity is not None:
        sources.append(liquidity)
    momentum = _momentum_source(markets)
    if momentum is not None:
        sources.append(momentum)
    return sources


# ── free-text parsing ──────────────────────────────────────────


def parse_source_rating(text: str | None) -> float | None:
    """Parse a 1-10 source rating out of a free-text evaluation."""
    if not text:
        return None
    match = _OVERALL_RE.search(text) or _RATING_RE.search(text)
    if match is None:
        return None
    rating = _to_float(match.group(1))
    if rating is None:
        return None
    return min(10.0, max(1.0, rating))


def parse_synthesis_report(
    report: str | None, keys: Sequence[str] = ()
) -> tuple[dict[str, int], str]:
    """Split a synthesis report into per-group credibility + a summary.

    Returns ``({key: credibility}, summary)``. When the model emitted no
    per-group lines, a single batch-level number is returned under
    :data:`BATCH_KEY`; when it emitted neither, the mapping is empty and
    the caller keeps its deterministic baseline.
    """
    text = (report or "").strip()
    if not text:
        return {}, ""
    summary = _extract_summary(text)
    per_group: dict[str, int] = {}
    for key, raw in _PER_GROUP_RE.findall(text):
        if keys and key not in keys:
            continue
        value = _to_float(raw)
        if value is None:
            continue
        per_group[key] = _clamp(value)
    if per_group:
        return per_group, summary
    batch = _BATCH_RE.search(text)
    if batch is not None:
        value = _to_float(batch.group(1))
        if value is not None:
            return {BATCH_KEY: _clamp(value)}, summary
    return {}, summary


def _extract_summary(text: str) -> str:
    """Prefer the synthesis report's KEY CONSENSUS section, else its head."""
    match = _CONSENSUS_RE.search(text)
    if match:
        section = match.group(1).strip()
        if section:
            return section[:MAX_SUMMARY_CHARS]
    return text[:MAX_SUMMARY_CHARS]


def _blend(current: int, observed: float, weight: float) -> int:
    """Pull a 0-100 score toward a 0-1 observed signal."""
    return _clamp(current * (1 - weight) + observed * 100 * weight)


# ── LLM layer ──────────────────────────────────────────────────


def _describe_age(age_hours: float | None) -> str:
    """Human-readable recency for the source-evaluation prompt."""
    if age_hours is None:
        return "publication time unknown"
    if age_hours < 1:
        return f"{age_hours * 60:.0f} minutes ago"
    if age_hours < 48:
        return f"{age_hours:.1f} hours ago"
    return f"{age_hours / 24:.1f} days ago"


def _render_source_prompt(source: EvidenceSource, market_question: str) -> str:
    """Render ``research.source_evaluation`` for one source.

    The kwargs must match the template's placeholders exactly — the
    prompt-contract test enforces that statically, and a drift would
    raise ``KeyError`` inside a live scan.
    """
    prompts = _load_prompts()
    template = prompts.get("research", {}).get("source_evaluation", "")
    if template:
        return template.format(
            market_question=market_question,
            source_name=source.name,
            content_summary=source.summary or source.name,
            pub_date=_describe_age(source.age_hours),
        )
    return (
        f"Rate the credibility (1-10) of this source for the market "
        f"'{market_question}': {source.name} — {source.summary} "
        f"({_describe_age(source.age_hours)})."
    )


async def evaluate_source(
    client: Any,
    source: EvidenceSource,
    market_question: str,
    llm_config: dict[str, Any] | None = None,
) -> float | None:
    """Ask the model to rate one evidence source 1-10.

    Returns the rating, or ``None`` when the call fails or the answer
    carries no parseable number: the caller keeps the deterministic
    score for that group.
    """
    description = _render_source_prompt(source, market_question)
    try:
        result = await client.analyze_market(
            market_title=f"Source evaluation — {source.name}"[:120],
            market_description=description,
            yes_price=0.5,
            no_price=0.5,
            volume_24h=0,
            end_date="",
            include_research=False,
            llm_config=llm_config,
        )
    except Exception as exc:
        logger.debug("Source evaluation unavailable for %s: %s", source.name, exc)
        return None
    if not isinstance(result, dict):
        return None
    return parse_source_rating(result.get("analysis"))


def _build_digest(
    bundles: Sequence[tuple[GroupEvidence, list[EvidenceSource]]],
    baselines: Mapping[str, int],
) -> str:
    """Serialise the batch's evidence for the single synthesis call."""
    payload = {
        "scan": "opportunity_scan",
        "groups": [
            {
                "key": group.key,
                "question": group.question[:200],
                "credibility_baseline": baselines[group.key],
                "sources": [s.as_dict() for s in sources[:MAX_SOURCES_PER_GROUP]],
            }
            for group, sources in bundles[:MAX_DIGEST_GROUPS]
        ],
        "output_contract": _DIGEST_OUTPUT_CONTRACT,
    }
    return json.dumps(payload, default=str)


async def _synthesize(client: Any, digest: str, llm_config: dict[str, Any] | None) -> str | None:
    """One ``research.synthesize_research`` call for the whole batch."""
    try:
        result = await client.synthesize_research(
            market_question=SYNTHESIS_QUESTION,
            findings_json=digest,
            llm_config=llm_config,
        )
    except Exception as exc:
        logger.info("Research synthesis unavailable (%s); using deterministic baseline", exc)
        return None
    if not isinstance(result, dict) or not result.get("success"):
        return None
    report = str(result.get("report") or "").strip()
    return report or None


async def _evaluate_top_sources(
    client: Any,
    bundles: Sequence[tuple[GroupEvidence, list[EvidenceSource]]],
    baselines: Mapping[str, int],
    llm_config: dict[str, Any] | None,
    max_source_evaluations: int,
) -> dict[str, float]:
    """Rate sources for the highest-priority groups within the budget."""
    sources_by_key = {group.key: sources for group, sources in bundles}
    ranked = sorted(
        (group for group, _ in bundles),
        key=lambda group: (group.priority, baselines.get(group.key, 0)),
        reverse=True,
    )[:MAX_SOURCE_EVALUATION_GROUPS]

    pairs: list[tuple[str, EvidenceSource, str]] = []
    for group in ranked:
        for source in sources_by_key.get(group.key, [])[:MAX_SOURCES_PER_GROUP]:
            if len(pairs) >= max_source_evaluations:
                break
            pairs.append((group.key, source, group.question))
        if len(pairs) >= max_source_evaluations:
            break

    if not pairs:
        return {}

    ratings = await asyncio.gather(
        *(evaluate_source(client, source, question, llm_config) for _, source, question in pairs),
        return_exceptions=True,
    )
    quality: dict[str, list[float]] = {}
    for (key, _source, _question), rating in zip(pairs, ratings, strict=True):
        if isinstance(rating, Exception) or rating is None:
            continue
        quality.setdefault(key, []).append((rating - 1) / 9)
    return {key: sum(values) / len(values) for key, values in quality.items()}


async def score_batch_credibility(
    client: Any,
    groups: Sequence[GroupEvidence],
    llm_config: dict[str, Any] | None = None,
    *,
    max_source_evaluations: int = MAX_SOURCE_EVALUATIONS,
) -> dict[str, BatchCredibility]:
    """Score every group's credibility for one scan batch.

    Computes the deterministic baseline, then optionally lifts it with
    per-source ratings and the batch synthesis. Every step degrades to
    the layer above it, so the returned map always carries a score for
    each input group.
    """
    if not groups or client is None:
        return {}

    bundles = [
        (
            group,
            build_scan_sources(group.markets, group.smart_money)[:MAX_SOURCES_PER_GROUP],
        )
        for group in groups
    ]
    baselines = {group.key: credibility_from_sources(sources) for group, sources in bundles}

    report = await _synthesize(client, _build_digest(bundles, baselines), llm_config)
    reported, summary = parse_synthesis_report(report, [group.key for group, _ in bundles])

    credibility = dict(baselines)
    keys = list(baselines)
    for key, quality in (
        await _evaluate_top_sources(client, bundles, baselines, llm_config, max_source_evaluations)
    ).items():
        credibility[key] = _blend(baselines[key], quality, SOURCE_EVALUATION_WEIGHT)

    if reported:
        # A per-group number applies to that group; a batch-level number
        # (BATCH_KEY) is the model's read on the whole digest.
        for key in keys:
            value = reported.get(key, reported.get(BATCH_KEY))
            if value is None:
                continue
            credibility[key] = _blend(credibility[key], value / 100, SYNTHESIS_WEIGHT)

    return {
        group.key: BatchCredibility(
            credibility_score=credibility[group.key],
            search_summary=summary,
        )
        for group, _ in bundles
    }
