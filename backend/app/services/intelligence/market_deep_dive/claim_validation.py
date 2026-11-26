"""Server-side claim grounding. The model proposes; this module decides attribution.

A claim is ``attributed`` only when its quote appears verbatim in captured
evidence for the cited URL. Attribution is not entailment and not truth.
"""

import hashlib
import json
import re
from typing import Literal

from pydantic import ValidationError

from app.services.intelligence.market_deep_dive.evidence_adapter import canonical_url
from app.services.intelligence.market_deep_dive.schemas import (
    DeepDiveDraft,
    EvidenceItem,
    ResearchClaim,
)

MIN_QUOTE_CHARS = 20
_INSTRUCTION_TEXT = re.compile(
    r"(ignore (all |any |previous |prior )?(instructions|constraints|rules)|"
    r"\b(sign|submit|place|execute)\b.{0,20}\b(trade|order|transaction)\b|"
    r"(system|developer) (prompt|message))",
    re.I,
)


def parse_draft(raw: object) -> DeepDiveDraft | None:
    """Accept one JSON document (optionally one fenced block), never prose with a guessed object."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    if not text or len(text) > 64_000:
        return None
    try:
        return DeepDiveDraft.model_validate_json(text)
    except (ValidationError, ValueError):
        return None


def _claim_id(statement: str, direction: str, index: int) -> str:
    return (
        "cl_" + hashlib.sha256(json.dumps([index, direction, statement]).encode()).hexdigest()[:12]
    )


def validate_evidence_links(
    draft: DeepDiveDraft, evidence: list[EvidenceItem]
) -> tuple[list[ResearchClaim], list[str]]:
    """Bind each claim to evidence IDs it can prove; flag instruction-like untrusted text."""
    by_url: dict[str, list[EvidenceItem]] = {}
    for item in evidence:
        by_url.setdefault(item.canonical_url, []).append(item)
    flags: list[str] = []
    claims: list[ResearchClaim] = []
    for index, draft_claim in enumerate(draft.claims):
        cited: list[str] = []
        try:
            wanted = canonical_url(draft_claim.source_url)
        except ValueError:
            wanted = ""
        if len(draft_claim.quote.strip()) >= MIN_QUOTE_CHARS:
            for item in by_url.get(wanted, []):
                if item.excerpt and draft_claim.quote in item.excerpt:
                    cited.append(item.evidence_id)
        if _INSTRUCTION_TEXT.search(draft_claim.statement) or _INSTRUCTION_TEXT.search(
            draft_claim.quote
        ):
            flags.append("UNTRUSTED_INSTRUCTION_TEXT_IGNORED")
        # Social posts are untrusted amplification: they never count as independent sources.
        sources = {
            item.publisher
            for item in evidence
            if item.evidence_id in cited and item.source_family != "social_post"
        }
        claims.append(
            ResearchClaim(
                claim_id=_claim_id(draft_claim.statement, draft_claim.direction, index),
                statement=draft_claim.statement,
                direction=draft_claim.direction,
                pivotal=draft_claim.pivotal,
                cited_evidence_ids=cited,
                independent_source_count=len(sources),
                attribution="attributed" if cited else "unsupported",
                uncertainty=draft_claim.uncertainty,
            )
        )
    attributed = [c for c in claims if c.attribution == "attributed"]
    for claim in attributed:
        opposite: Literal["supports", "contradicts"] | None = {
            "supports": "contradicts",
            "contradicts": "supports",
        }.get(claim.direction)
        if opposite:
            claim.conflicts = [c.claim_id for c in attributed if c.direction == opposite]
    return claims, list(dict.fromkeys(flags))


def independent_source_total(claims: list[ResearchClaim], evidence: list[EvidenceItem]) -> int:
    cited = {e for c in claims if c.attribution == "attributed" for e in c.cited_evidence_ids}
    return len(
        {
            item.publisher
            for item in evidence
            if item.evidence_id in cited and item.source_family != "social_post"
        }
    )
