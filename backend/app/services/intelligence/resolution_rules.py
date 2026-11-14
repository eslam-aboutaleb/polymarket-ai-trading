"""Deterministic, evidence-backed resolution rule analysis for binary markets.

Parses Polymarket CLOB/Gamma market metadata and resolution criteria into a
versioned ``ContractSpecification`` with exact source-span citations, UTC cutoff
normalization, oracle identification, ambiguity/mismatch detection, and a pure
``resolve_payoff`` evaluator. Unverified or ambiguous rules categorically block
payoff resolution and tradable-edge claims.

This module is strictly read-only/analytical: it holds no wallet keys and
cannot sign, submit, or cancel orders.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, time
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.analytics.assessment import Assessment
from app.models.analytics.resolution_rule_snapshot import ResolutionRuleSnapshot
from app.services.intelligence.resolution_payoff import resolve_payoff as resolve_payoff
from app.services.intelligence.resolution_rule_models import (
    SCHEMA_VERSION as SCHEMA_VERSION,
)
from app.services.intelligence.resolution_rule_models import (
    SKILL_VERSION as SKILL_VERSION,
)
from app.services.intelligence.resolution_rule_models import (
    Comparator as Comparator,
)
from app.services.intelligence.resolution_rule_models import (
    ContractSpecification as ContractSpecification,
)
from app.services.intelligence.resolution_rule_models import (
    EvidenceSpan as EvidenceSpan,
)
from app.services.intelligence.resolution_rule_models import (
    PayoffObservation as PayoffObservation,
)
from app.services.intelligence.resolution_rule_models import (
    PayoffResolution as PayoffResolution,
)
from app.services.intelligence.resolution_rule_models import (
    ResolutionRuleExplanation as ResolutionRuleExplanation,
)
from app.services.intelligence.resolution_rule_models import (
    ResolutionRuleVersion as ResolutionRuleVersion,
)
from app.services.intelligence.resolution_rule_models import (
    ReviewStatus as ReviewStatus,
)
from app.services.intelligence.resolution_rule_models import (
    RuleChangeReport as RuleChangeReport,
)
from app.services.market_data.binary_identity import canonical_binary_metadata
from app.services.market_data.provider_contract import OFFICIAL_CLOB_API as POLYMARKET_CLOB_API

if TYPE_CHECKING:
    from app.services.market_data.polymarket_service import PolymarketService
from app.utils.time import utc_now

# Explicit fixed-offset or IANA timezone mappings. "ET"/"PT"/"CT" without
# standard/daylight qualifier are treated as America/New_York etc. only when
# unambiguously resolvable, and flagged if DST-ambiguous.
_FIXED_TZ_OFFSETS: dict[str, str] = {
    "UTC": "UTC",
    "GMT": "UTC",
    "Z": "UTC",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "ET": "America/New_York",
    "PT": "America/Los_Angeles",
    "CT": "America/Chicago",
}

_EST_FIXED_ZONES = {"EST": -5, "EDT": -4, "PST": -8, "PDT": -7, "CST": -6, "CDT": -5}

_STRICT_GT_RE = re.compile(
    r"\b(?:strictly\s+(?:greater|more|higher)\s+than|greater\s+than(?!\s+or\s+equal)|"
    r"more\s+than|above(?!\s+or\s+equal)|exceeds?|over)\b|(?<![<>!])>(?!=)",
    re.IGNORECASE,
)
_GTE_RE = re.compile(
    r"\b(?:at\s+least|or\s+(?:more|above|higher|greater)|"
    r"greater\s+than\s+or\s+equal\s+to|no\s+less\s+than)\b|>=",
    re.IGNORECASE,
)
_STRICT_LT_RE = re.compile(
    r"\b(?:strictly\s+less\s+than|less\s+than(?!\s+or\s+equal)|"
    r"below(?!\s+or\s+equal)|under|fewer\s+than)\b|(?<![<>!])<(?!=)",
    re.IGNORECASE,
)
_LTE_RE = re.compile(
    r"\b(?:at\s+most|or\s+(?:less|below|lower|fewer)|"
    r"less\s+than\s+or\s+equal\s+to|no\s+more\s+than)\b|<=",
    re.IGNORECASE,
)
_EQ_RE = re.compile(r"\b(?:equal\s+to|exactly)\b|==", re.IGNORECASE)
_AMBIGUOUS_COMP_RE = re.compile(r"\b(?:reaches?|hits?|touches?)\b", re.IGNORECASE)

_THRESHOLD_RE = re.compile(
    r"(?<![\d.,])(?P<prefix>\$|USD\s*)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d.,])"
    r"\s*(?P<unit>%|percent|bps|USDC|USD|EUR|BTC|ETH|°?[CF])?",
    re.IGNORECASE,
)

_DATE_TIME_TZ_RE = re.compile(
    r"(?P<iso>\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?"
    r"\s*(?:Z|UTC|GMT|EST|EDT|PST|PDT|CST|CDT|ET|PT|CT)?)"
    r"|(?P<named>(?:January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+\d{1,2},?\s+\d{4}"
    r"(?:\s+(?:at\s+)?\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)?"
    r"\s*(?:UTC|GMT|EST|EDT|PST|PDT|CST|CDT|ET|PT|CT)?)",
    re.IGNORECASE,
)

_ORACLE_RE = re.compile(
    r"\b(?P<oracle>Chainlink|UMA(?:\s+Optimistic\s+Oracle)?|Pyth|Coinbase(?:\s+Pro)?|"
    r"Binance|Kraken|Bitstamp|Associated\s+Press|AP\s+News|Reuters|Bloomberg|"
    r"Federal\s+Reserve|Bureau\s+of\s+Labor\s+Statistics|BLS|NOAA|NASA|SEC|official\s+[A-Za-z0-9\s]+?)\b"
    r"(?:\s*\((?P<symbol>[A-Z0-9/_-]{2,24})\))?",
    re.IGNORECASE,
)

_VOID_DISPUTE_RE = re.compile(
    r"\b(?:void(?:ed)?|invalid(?:ated)?|cancel(?:led|ed)?|50\s*/\s*50|50-50|dispute[sd]?|challenged?)\b",
    re.IGNORECASE,
)
_ROUNDING_RE = re.compile(
    r"\b(?:round(?:ed|ing)?\s+(?:to|up|down|nearest)[^.;\n]{0,80}|truncat(?:ed|ion)[^.;\n]{0,60})\b",
    re.IGNORECASE,
)
_FALLBACK_RE = re.compile(
    r"\b(?:fallback|if\s+unavailable|secondary\s+source|otherwise\s+resolve)[^.;\n]{0,120}",
    re.IGNORECASE,
)
_SUBJECT_RE = re.compile(
    r"^\s*(?:Will|Is|Does|Can)\s+(?P<subject>[A-Za-z0-9][A-Za-z0-9\s./-]{1,60}?)"
    r"(?:\s+(?:be|reach|hit|exceed|close|trade|fall|rise|stay|remain)\b|\?)",
    re.IGNORECASE,
)
_METRIC_RE = re.compile(
    r"\b(?P<metric>price|closing\s+price|spot\s+price|rate|interest\s+rate|cpi|inflation|"
    r"temperature|volume|market\s+cap|gdp|unemployment|votes?|margin)\b",
    re.IGNORECASE,
)


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("Timezone-aware UTC datetime is required")
    return dt.astimezone(UTC)


def _extract_token_outcomes(raw: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Extract explicit binary YES/NO token mapping without positional inference."""
    reasons: list[str] = []
    tokens = raw.get("tokens")
    if not isinstance(tokens, list) or len(tokens) != 2:
        return {}, ["ABSTAIN_NON_BINARY_OR_MISSING_TOKENS"]

    # Support explicit paired Up/Down aliases when gamma outcomes are supplied.
    if "outcomes" in raw:
        try:
            normalized_clob, _ = canonical_binary_metadata(raw, raw)
            tokens = normalized_clob.get("tokens", tokens)
        except ValueError:
            pass

    mapping: dict[str, str] = {}
    for item in tokens:
        if not isinstance(item, dict):
            return {}, ["ABSTAIN_INVALID_TOKEN_ENTRY"]
        label = str(item.get("outcome", "")).strip().upper()
        token_id = item.get("token_id")
        if label not in ("YES", "NO") or not isinstance(token_id, str) or not token_id.isdecimal():
            return {}, ["ABSTAIN_AMBIGUOUS_TOKEN_MAPPING"]
        if label in mapping or token_id in mapping.values():
            return {}, ["ABSTAIN_DUPLICATE_TOKEN_OR_OUTCOME"]
        mapping[label] = token_id

    if set(mapping.keys()) != {"YES", "NO"}:
        reasons.append("ABSTAIN_INCOMPLETE_BINARY_MAPPING")
    return mapping, reasons


def _extract_comparator(
    text: str, field: str, source_url: str
) -> tuple[Comparator, list[EvidenceSpan], list[str]]:
    flags: list[str] = []
    spans: list[EvidenceSpan] = []

    # Check >= and <= before > and < so "greater than or equal to" is not
    # misclassified as strict ">".
    matches: list[tuple[Comparator, re.Match[str]]] = []
    for comp, pattern in (
        (">=", _GTE_RE),
        ("<=", _LTE_RE),
        (">", _STRICT_GT_RE),
        ("<", _STRICT_LT_RE),
        ("==", _EQ_RE),
    ):
        for m in pattern.finditer(text):
            if not any(m.start() < prior.end() and m.end() > prior.start() for _, prior in matches):
                matches.append((comp, m))

    if len(matches) > 1:
        flags.append(f"MULTIPLE_COMPARATORS_IN_{field.upper()}")
        return "unknown", spans, flags

    if len(matches) == 1:
        comp, m = matches[0]
        spans.append(
            EvidenceSpan(
                field=f"{field}.comparator",
                quote=m.group(0),
                start=m.start(),
                end=m.end(),
                source_url=source_url,
            )
        )
        return comp, spans, flags

    ambig = _AMBIGUOUS_COMP_RE.search(text)
    if ambig:
        flags.append("AMBIGUOUS_THRESHOLD_COMPARATOR")
        spans.append(
            EvidenceSpan(
                field=f"{field}.comparator_ambiguous",
                quote=ambig.group(0),
                start=ambig.start(),
                end=ambig.end(),
                source_url=source_url,
            )
        )
    return "unknown", spans, flags


def _extract_threshold(
    text: str, field: str, source_url: str
) -> tuple[Decimal | None, int | None, str | None, list[EvidenceSpan]]:
    for m in _THRESHOLD_RE.finditer(text):
        prefix = (m.group("prefix") or "").strip()
        unit = (m.group("unit") or "").strip()
        raw_num = m.group("num").replace(",", "")
        # Skip bare 4-digit years (e.g. 2026) when no currency/unit is attached.
        if not prefix and not unit and len(raw_num) == 4 and raw_num.startswith(("19", "20")):
            continue
        try:
            val = Decimal(raw_num)
        except InvalidOperation:
            continue
        precision = abs(val.as_tuple().exponent) if isinstance(val.as_tuple().exponent, int) else 0
        resolved_unit = (
            unit.upper() if unit else ("USD" if "$" in prefix or "USD" in prefix.upper() else None)
        )
        span = EvidenceSpan(
            field=f"{field}.threshold",
            quote=m.group(0),
            start=m.start(),
            end=m.end(),
            source_url=source_url,
        )
        return val, precision, resolved_unit, [span]
    return None, None, None, []


def _parse_cutoff(
    rules_text: str, end_date_iso: str | None, source_url: str
) -> tuple[datetime | None, str | None, bool | None, list[EvidenceSpan], list[str]]:
    flags: list[str] = []
    spans: list[EvidenceSpan] = []

    m = _DATE_TIME_TZ_RE.search(rules_text)
    if m:
        snippet = m.group(0).strip()
        spans.append(
            EvidenceSpan(
                field="rules.cutoff",
                quote=m.group(0),
                start=m.start(),
                end=m.end(),
                source_url=source_url,
            )
        )
        tz_token = None
        for tz in ("UTC", "GMT", "EST", "EDT", "PST", "PDT", "CST", "CDT", "ET", "PT", "CT", "Z"):
            if re.search(rf"(?:\b|(?<=\d)){tz}\b", snippet, re.IGNORECASE):
                tz_token = tz.upper()
                break

        if tz_token is None:
            flags.append("MISSING_TIMEZONE_IN_CUTOFF")
            return None, None, None, spans, flags

        cleaned = re.sub(rf"(?:\b|(?<=\d)){tz_token}\b", "", snippet, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"\s+at\s+", " ", cleaned, flags=re.IGNORECASE).replace(",", "")

        parsed_naive: datetime | None = None
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%B %d %Y %I:%M %p",
            "%B %d %Y %H:%M",
            "%B %d %Y",
            "%b %d %Y %I:%M %p",
            "%b %d %Y %H:%M",
            "%b %d %Y",
        ):
            try:
                parsed_naive = datetime.strptime(cleaned, fmt)
                break
            except ValueError:
                continue

        if parsed_naive is None:
            flags.append("UNPARSEABLE_CUTOFF_TIMESTAMP")
            return None, tz_token, None, spans, flags

        # Check if date-only without explicit time of day.
        if parsed_naive.time() == time(0, 0) and not re.search(r"\d{1,2}:\d{2}", cleaned):
            flags.append("DATE_ONLY_CUTOFF_MISSING_TIME")

        if tz_token in ("ET", "PT", "CT"):
            flags.append("AMBIGUOUS_DAYLIGHT_SAVING_TIMEZONE")
            iana = _FIXED_TZ_OFFSETS[tz_token]
            cutoff_utc = parsed_naive.replace(tzinfo=ZoneInfo(iana)).astimezone(UTC)
            return cutoff_utc, iana, True, spans, flags

        if tz_token in _EST_FIXED_ZONES:
            iana_equiv = _FIXED_TZ_OFFSETS[tz_token]
            zone_dt = parsed_naive.replace(tzinfo=ZoneInfo(iana_equiv))
            is_dst = bool(zone_dt.dst())
            if tz_token.endswith("ST") and is_dst:
                flags.append("DST_STANDARD_TIME_MISMATCH")
            if tz_token.endswith("DT") and not is_dst:
                flags.append("DST_DAYLIGHT_TIME_MISMATCH")
            from datetime import timedelta, timezone

            fixed_tz = timezone(timedelta(hours=_EST_FIXED_ZONES[tz_token]))
            cutoff_utc = parsed_naive.replace(tzinfo=fixed_tz).astimezone(UTC)
            return cutoff_utc, tz_token, True, spans, flags

        cutoff_utc = parsed_naive.replace(tzinfo=UTC)
        if end_date_iso:
            try:
                meta_dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
                if meta_dt.tzinfo is None:
                    meta_dt = meta_dt.replace(tzinfo=UTC)
                if abs((meta_dt.astimezone(UTC) - cutoff_utc).total_seconds()) > 86400:
                    flags.append("METADATA_END_DATE_CUTOFF_MISMATCH")
            except ValueError:
                pass
        return cutoff_utc, "UTC", True, spans, flags

    flags.append("MISSING_CUTOFF_IN_RULES")
    return None, None, None, spans, flags


def _extract_oracle(
    rules_text: str, raw: dict[str, Any], source_url: str
) -> tuple[str | None, str | None, list[EvidenceSpan], list[str]]:
    flags: list[str] = []
    spans: list[EvidenceSpan] = []

    meta_source = raw.get("resolutionSource") or raw.get("resolution_source")
    meta_source = (
        meta_source.strip() if isinstance(meta_source, str) and meta_source.strip() else None
    )

    m = _ORACLE_RE.search(rules_text)
    if m:
        oracle_name = m.group("oracle").strip()
        symbol = m.group("symbol").strip() if m.group("symbol") else None
        spans.append(
            EvidenceSpan(
                field="rules.oracle",
                quote=m.group(0),
                start=m.start(),
                end=m.end(),
                source_url=source_url,
            )
        )
        if (
            meta_source
            and oracle_name.lower() not in meta_source.lower()
            and meta_source.lower() not in oracle_name.lower()
        ):
            flags.append("ORACLE_METADATA_RULES_MISMATCH")
        return oracle_name, symbol, spans, flags

    if meta_source:
        flags.append("ORACLE_ONLY_IN_METADATA_NOT_RULES")
        return meta_source, None, spans, flags

    flags.append("MISSING_ORACLE_SOURCE")
    return None, None, spans, flags


def parse_resolution_rule(
    condition_id: str,
    raw: dict[str, Any],
    observed_at: datetime,
    *,
    clarifications: list[str] | None = None,
) -> ContractSpecification:
    """Deterministically parse market metadata + rules into a ContractSpecification."""
    obs_utc = _ensure_utc(observed_at)
    norm_cid = condition_id.strip().lower()
    source_url = f"{POLYMARKET_CLOB_API}/markets/{norm_cid}"

    question = raw.get("question") if isinstance(raw.get("question"), str) else ""
    description = raw.get("description") if isinstance(raw.get("description"), str) else ""
    clarification_list = [
        c.strip() for c in (clarifications or []) if isinstance(c, str) and c.strip()
    ]
    full_rules = description
    if clarification_list:
        full_rules = f"{description}\n\nClarifications:\n" + "\n".join(clarification_list)

    end_date_iso = raw.get("end_date_iso") if isinstance(raw.get("end_date_iso"), str) else None
    meta_oracle = (
        raw.get("resolutionSource") if isinstance(raw.get("resolutionSource"), str) else None
    )

    token_outcomes, token_abstain = _extract_token_outcomes(raw)
    ambiguity_flags: list[str] = []
    abstain_reasons: list[str] = list(token_abstain)
    evidence_spans: list[EvidenceSpan] = []

    raw_cid = raw.get("condition_id")
    if not isinstance(raw_cid, str) or raw_cid.strip().lower() != norm_cid:
        abstain_reasons.append("ABSTAIN_CONDITION_ID_MISMATCH")

    if not question.strip():
        abstain_reasons.append("ABSTAIN_MISSING_QUESTION")
    if raw.get("version") not in (None, "v1"):
        abstain_reasons.append("ABSTAIN_UNSUPPORTED_PROTOCOL_VERSION")
    if raw.get("neg_risk") is True:
        abstain_reasons.append("ABSTAIN_UNSUPPORTED_MARKET_FORM")

    if not full_rules.strip():
        abstain_reasons.append("ABSTAIN_MISSING_RESOLUTION_RULES")

    if clarification_list:
        ambiguity_flags.append("LATE_CLARIFICATION_PRESENT")

    uma_status = str(raw.get("umaResolutionStatus") or "").strip().lower()
    if uma_status in ("disputed", "challenged") or raw.get("disputed") is True:
        ambiguity_flags.append("ACTIVE_DISPUTE")
        abstain_reasons.append("ABSTAIN_DISPUTED_MARKET")
    if uma_status in ("void", "voided") or raw.get("voided") is True:
        ambiguity_flags.append("VOID_MARKET")
        abstain_reasons.append("ABSTAIN_VOID_MARKET")

    if re.search(
        r"\b(?:TWAP|time.weighted|median|average|at any time|during the)\b", full_rules, re.I
    ):
        abstain_reasons.append("ABSTAIN_UNSUPPORTED_AGGREGATION_OR_WINDOW")
    if re.search(r"(?:\$|USD\s*)\d[\d,.]*(?:[eE][+-]?\d+|[kKmMbB]\b|\s+\d{3}\b)", full_rules):
        abstain_reasons.append("ABSTAIN_UNSUPPORTED_NUMERIC_FORMAT")

    # Extract comparator & threshold from rules, and cross-check against question.
    rule_comp, rule_comp_spans, rule_comp_flags = _extract_comparator(
        full_rules, "rules", source_url
    )
    q_comp, _, _ = _extract_comparator(question, "question", source_url)
    evidence_spans.extend(rule_comp_spans)
    ambiguity_flags.extend(rule_comp_flags)
    if q_comp != "unknown" and rule_comp != "unknown" and q_comp != rule_comp:
        ambiguity_flags.append("TITLE_RULES_COMPARATOR_MISMATCH")

    rule_thresh, precision, units, thresh_spans = _extract_threshold(
        full_rules, "rules", source_url
    )
    q_thresh, _, _, _ = _extract_threshold(question, "question", source_url)
    evidence_spans.extend(thresh_spans)
    if q_thresh is not None and rule_thresh is not None and q_thresh != rule_thresh:
        ambiguity_flags.append("TITLE_RULES_THRESHOLD_MISMATCH")

    cutoff_utc, local_tz, cutoff_inclusive, cutoff_spans, cutoff_flags = _parse_cutoff(
        full_rules, end_date_iso, source_url
    )
    evidence_spans.extend(cutoff_spans)
    ambiguity_flags.extend(cutoff_flags)
    if cutoff_utc is not None and end_date_iso:
        try:
            metadata_end = _ensure_utc(datetime.fromisoformat(end_date_iso.replace("Z", "+00:00")))
            if metadata_end != cutoff_utc:
                ambiguity_flags.append("METADATA_END_DATE_CUTOFF_MISMATCH")
        except ValueError:
            ambiguity_flags.append("METADATA_END_DATE_CUTOFF_MISMATCH")

    oracle, oracle_symbol, oracle_spans, oracle_flags = _extract_oracle(full_rules, raw, source_url)
    evidence_spans.extend(oracle_spans)
    ambiguity_flags.extend(oracle_flags)

    rounding_m = _ROUNDING_RE.search(full_rules)
    rounding_policy = rounding_m.group(0).strip() if rounding_m else None
    if rounding_m:
        ambiguity_flags.append("UNSUPPORTED_ROUNDING_POLICY")
        evidence_spans.append(
            EvidenceSpan(
                field="rules.rounding_policy",
                quote=rounding_m.group(0),
                start=rounding_m.start(),
                end=rounding_m.end(),
                source_url=source_url,
            )
        )

    void_m = _VOID_DISPUTE_RE.search(full_rules)
    invalid_void_policy = void_m.group(0).strip() if void_m else None
    fallback_hierarchy = [m.group(0).strip() for m in _FALLBACK_RE.finditer(full_rules)]
    if fallback_hierarchy:
        ambiguity_flags.append("UNVERIFIED_FALLBACK_POLICY")

    subj_m = _SUBJECT_RE.search(question)
    subject = subj_m.group("subject").strip() if subj_m else None
    if subj_m:
        raw_subject = subj_m.group("subject")
        evidence_spans.append(
            EvidenceSpan(
                field="question.subject",
                quote=subj_m.group("subject").strip(),
                start=subj_m.start("subject") + len(raw_subject) - len(raw_subject.lstrip()),
                end=subj_m.end("subject") - len(raw_subject) + len(raw_subject.rstrip()),
                source_url=source_url,
            )
        )

    metric_m = _METRIC_RE.search(full_rules) or _METRIC_RE.search(question)
    metric = (
        metric_m.group("metric").strip().lower()
        if metric_m
        else (f"oracle_series:{oracle_symbol}" if oracle_symbol else None)
    )
    if metric_m:
        field_prefix = "rules" if _METRIC_RE.search(full_rules) else "question"
        evidence_spans.append(
            EvidenceSpan(
                field=f"{field_prefix}.metric",
                quote=metric_m.group(0),
                start=metric_m.start(),
                end=metric_m.end(),
                source_url=source_url,
            )
        )

    if rule_comp == "unknown" and rule_thresh is not None:
        ambiguity_flags.append("THRESHOLD_WITHOUT_COMPARATOR")
    if rule_thresh is None or rule_comp == "unknown":
        abstain_reasons.append("ABSTAIN_UNSUPPORTED_QUANTITATIVE_RULE")
    if "AMBIGUOUS_THRESHOLD_COMPARATOR" in ambiguity_flags:
        abstain_reasons.append("ABSTAIN_AMBIGUOUS_COMPARATOR")
    if "MISSING_ORACLE_SOURCE" in ambiguity_flags:
        abstain_reasons.append("ABSTAIN_MISSING_ORACLE")
    if any(
        f in ambiguity_flags
        for f in (
            "AMBIGUOUS_DAYLIGHT_SAVING_TIMEZONE",
            "DST_STANDARD_TIME_MISMATCH",
            "DST_DAYLIGHT_TIME_MISMATCH",
            "MISSING_TIMEZONE_IN_CUTOFF",
            "MISSING_CUTOFF_IN_RULES",
            "DATE_ONLY_CUTOFF_MISSING_TIME",
            "METADATA_END_DATE_CUTOFF_MISMATCH",
        )
    ):
        abstain_reasons.append("ABSTAIN_AMBIGUOUS_CUTOFF")
    if any(
        f in ambiguity_flags
        for f in (
            "TITLE_RULES_COMPARATOR_MISMATCH",
            "TITLE_RULES_THRESHOLD_MISMATCH",
            "ORACLE_METADATA_RULES_MISMATCH",
        )
    ):
        abstain_reasons.append("ABSTAIN_METADATA_RULES_MISMATCH")

    # Deduplicate preserving order.
    ambiguity_flags = list(dict.fromkeys(ambiguity_flags))
    abstain_reasons = list(dict.fromkeys(abstain_reasons))

    if any(
        r in abstain_reasons
        for r in (
            "ABSTAIN_VOID_MARKET",
            "ABSTAIN_CONDITION_ID_MISMATCH",
            "ABSTAIN_NON_BINARY_OR_MISSING_TOKENS",
        )
    ):
        review_status: ReviewStatus = "invalid"
    elif abstain_reasons or ambiguity_flags:
        review_status = "ambiguous"
    elif oracle and cutoff_utc is not None and token_outcomes:
        review_status = "verified"
    else:
        review_status = "unverified"
        abstain_reasons.append("ABSTAIN_UNVERIFIED_SPECIFICATION")

    source_hash = _digest(
        {
            "condition_id": norm_cid,
            "question": question,
            "rules": full_rules,
            "end_date": end_date_iso,
            "oracle": meta_oracle,
            "tokens": sorted(token_outcomes.items()),
            "settlement_status": uma_status
            if uma_status in {"disputed", "challenged", "void", "voided"}
            else None,
            "disputed": raw.get("disputed") is True,
            "voided": raw.get("voided") is True,
        }
    )

    return ContractSpecification(
        condition_id=norm_cid,
        question=question,
        token_outcomes=token_outcomes,
        subject=subject,
        metric=metric,
        comparator=rule_comp,
        threshold=rule_thresh,
        threshold_precision=precision,
        units=units,
        cutoff_utc=cutoff_utc,
        local_timezone=local_tz,
        cutoff_inclusive=cutoff_inclusive,
        oracle=oracle,
        oracle_symbol=oracle_symbol,
        fallback_hierarchy=fallback_hierarchy,
        rounding_policy=rounding_policy,
        invalid_void_policy=invalid_void_policy,
        dispute_policy="UMA" if uma_status else None,
        source_uri=source_url,
        source_hash=source_hash,
        raw_rules=full_rules,
        clarifications=clarification_list,
        raw_market=raw,
        observed_at=obs_utc,
        valid_from=obs_utc,
        review_status=review_status,
        ambiguity_flags=ambiguity_flags,
        abstain_reasons=abstain_reasons,
        evidence_spans=evidence_spans,
    )


def detect_rule_change(
    previous: ContractSpecification, current: ContractSpecification
) -> RuleChangeReport:
    """Detect material rule, token-mapping, cutoff, or oracle changes."""
    reasons: list[str] = []
    if previous.source_hash == current.source_hash:
        return RuleChangeReport(
            condition_id=current.condition_id,
            previous_hash=previous.source_hash,
            current_hash=current.source_hash,
            changed=False,
            invalidates_forecasts=False,
        )

    if previous.token_outcomes != current.token_outcomes:
        reasons.append("TOKEN_MAPPING_FLIPPED_OR_CHANGED")
    if previous.comparator != current.comparator or previous.threshold != current.threshold:
        reasons.append("COMPARATOR_OR_THRESHOLD_CHANGED")
    if previous.cutoff_utc != current.cutoff_utc:
        reasons.append("CUTOFF_CHANGED")
    if previous.oracle != current.oracle or previous.oracle_symbol != current.oracle_symbol:
        reasons.append("ORACLE_CHANGED")
    if "LATE_CLARIFICATION_PRESENT" in current.ambiguity_flags:
        reasons.append("CLARIFICATION_ADDED")
    if not reasons:
        reasons.append("RULE_TEXT_CHANGED")

    return RuleChangeReport(
        condition_id=current.condition_id,
        previous_hash=previous.source_hash,
        current_hash=current.source_hash,
        changed=True,
        invalidates_forecasts=True,
        reasons=reasons,
    )


def record_rule_snapshot(db: Session, spec: ContractSpecification) -> ResolutionRuleSnapshot:
    """Persist an immutable rule snapshot idempotently by (condition_id, source_hash)."""
    existing = db.scalar(
        select(ResolutionRuleSnapshot).where(
            ResolutionRuleSnapshot.condition_id == spec.condition_id,
            ResolutionRuleSnapshot.source_hash == spec.source_hash,
            ResolutionRuleSnapshot.observed_at == spec.observed_at,
        )
    )
    if existing is not None:
        return existing

    row = ResolutionRuleSnapshot(
        condition_id=spec.condition_id,
        source_hash=spec.source_hash,
        observed_at=spec.observed_at,
        source_uri=spec.source_uri,
        review_status=spec.review_status,
        token_outcomes_json=json.dumps(spec.token_outcomes, sort_keys=True),
        spec_json=spec.model_dump_json(),
        ambiguity_flags_json=json.dumps(spec.ambiguity_flags),
        abstain_reasons_json=json.dumps(spec.abstain_reasons),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        return db.scalar(
            select(ResolutionRuleSnapshot).where(
                ResolutionRuleSnapshot.condition_id == spec.condition_id,
                ResolutionRuleSnapshot.source_hash == spec.source_hash,
                ResolutionRuleSnapshot.observed_at == spec.observed_at,
            )
        )


def get_rule_snapshot_as_of(
    db: Session, condition_id: str, as_of: datetime | None = None
) -> ContractSpecification | None:
    """Point-in-time query: only returns snapshots with observed_at <= as_of."""
    stmt = select(ResolutionRuleSnapshot).where(
        ResolutionRuleSnapshot.condition_id == condition_id.strip().lower()
    )
    if as_of is not None:
        stmt = stmt.where(ResolutionRuleSnapshot.observed_at <= _ensure_utc(as_of))
    stmt = stmt.order_by(
        ResolutionRuleSnapshot.observed_at.desc(),
        ResolutionRuleSnapshot.id.desc(),
    ).limit(1)
    row = db.scalar(stmt)
    if row is None:
        return None
    spec = ContractSpecification.model_validate_json(row.spec_json)
    received = row.observed_at
    if received.tzinfo is None:  # SQLite stores the application's UTC datetime without tzinfo.
        received = received.replace(tzinfo=UTC)
    derived = parse_resolution_rule(
        spec.condition_id, spec.raw_market, spec.observed_at, clarifications=spec.clarifications
    )
    if (
        spec != derived
        or spec.source_hash != row.source_hash
        or spec.condition_id != row.condition_id
        or spec.observed_at != received
        or spec.source_uri != row.source_uri
        or spec.review_status != row.review_status
    ):
        raise ValueError("RULE_SNAPSHOT_INTEGRITY_FAILURE")
    return spec


def invalidate_dependent_forecasts(db: Session, condition_id: str, current_source_hash: str) -> int:
    """Quarantine unresolved assessments whose market rules changed."""
    from app.models.trading.trade_history import TradeHistory

    trade_ids = select(TradeHistory.id).where(
        TradeHistory.market_id == condition_id.strip().lower()
    )
    result = db.execute(
        update(Assessment)
        .where(
            or_(
                Assessment.condition_id == condition_id.strip().lower(),
                Assessment.trade_history_id.in_(trade_ids),
            ),
            Assessment.resolved_at.is_(None),
            Assessment.exclusion_reason.is_(None),
        )
        .values(exclusion_reason=f"rule_changed:{current_source_hash[:12]}")
    )
    return int(result.rowcount or 0)


async def explain_resolution_rule(
    condition_id: str,
    *,
    as_of: datetime | None = None,
    db: Session | None = None,
    raw_market: dict[str, Any] | None = None,
    service: PolymarketService | None = None,
    clarifications: list[str] | None = None,
) -> ResolutionRuleExplanation:
    """Read-only rule explanation with citations, verification status, and abstain codes."""
    norm_cid = condition_id.strip().lower()
    prior_spec = get_rule_snapshot_as_of(db, norm_cid, as_of) if db is not None else None

    if as_of is not None:
        _ensure_utc(as_of)
        if raw_market is not None or prior_spec is None:
            raise ValueError(
                "HISTORICAL_RULE_SNAPSHOT_UNAVAILABLE: "
                "historical queries require a recorded snapshot"
            )
        spec = prior_spec
    elif raw_market is not None:
        spec = parse_resolution_rule(
            norm_cid,
            raw_market,
            utc_now(),
            clarifications=clarifications,
        )
    elif service is not None:
        fetched = await service.get_market_outcome_tokens(norm_cid)
        spec = parse_resolution_rule(
            norm_cid,
            fetched,
            utc_now(),
            clarifications=clarifications,
        )
    elif prior_spec is not None:
        spec = prior_spec
    else:
        raise ValueError("No market payload, snapshot, or PolymarketService supplied")

    change_report = (
        detect_rule_change(prior_spec, spec)
        if prior_spec is not None and spec.source_hash != prior_spec.source_hash
        else None
    )

    return ResolutionRuleExplanation(
        specification=spec,
        tradable_edge_allowed=(spec.review_status == "verified" and raw_market is None),
        change_report=change_report,
    )
