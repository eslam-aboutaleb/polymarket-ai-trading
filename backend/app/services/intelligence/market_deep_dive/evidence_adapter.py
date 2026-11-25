"""Turn captured research text into typed, hashed evidence items.

Only the existing bounded search-excerpt format is read; no model-provided URL
is ever fetched. Publication time is reported only when the source carried it,
otherwise it stays ``None`` and the limitation is stated.
"""

import hashlib
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.services.intelligence.market_context import MarketContext
from app.services.intelligence.market_deep_dive.schemas import EvidenceItem
from app.services.intelligence.market_research import _search_excerpts, safe_research_url

_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_src$)", re.I)
MAX_EVIDENCE_ITEMS = 40


def canonical_url(url: str) -> str:
    """Stable identity for a public HTTPS URL: no fragment, tracking params, www or default port."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING.match(k)
        )
    )
    path = parts.path.rstrip("/") or "/"
    port = f":{parts.port}" if parts.port not in (None, 80, 443) else ""
    return urlunsplit((parts.scheme.lower(), host + port, path, query, ""))


def publisher_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def content_hash(text: str) -> str:
    normalized = " ".join(text.split()).lower()
    return hashlib.sha256(normalized.encode()).hexdigest()


def evidence_id(canonical: str, digest: str) -> str:
    return "ev_" + hashlib.sha256(f"{canonical}|{digest}".encode()).hexdigest()[:16]


def collect_evidence(
    context: MarketContext, research_context: str, retrieved_at: datetime
) -> list[EvidenceItem]:
    """Venue rules plus bounded search excerpts, de-duplicated by content hash."""
    items: list[EvidenceItem] = []
    rules = context.rules.text
    if rules.strip():
        digest = content_hash(rules)
        items.append(
            EvidenceItem(
                evidence_id=evidence_id(canonical_url(context.source_url), digest),
                url=context.source_url,
                canonical_url=canonical_url(context.source_url),
                publisher=publisher_of(context.source_url),
                published_at=None,
                retrieved_at=context.observed_at,
                available_at=context.observed_at,
                source_family="venue_rules",
                content_hash=digest,
                excerpt=rules[:3000],
                verification="venue_text",
                limitations=["Venue text is shown as written; interpretation is separate."],
            )
        )
    seen = {item.content_hash for item in items}
    for url, excerpts in _search_excerpts(research_context or "").items():
        if not safe_research_url(url):
            continue
        for text in excerpts:
            digest = content_hash(text)
            if digest in seen:  # syndicated or repeated text is one piece of evidence
                continue
            seen.add(digest)
            canonical = canonical_url(url)
            items.append(
                EvidenceItem(
                    evidence_id=evidence_id(canonical, digest),
                    url=url,
                    canonical_url=canonical,
                    publisher=publisher_of(url),
                    published_at=None,
                    retrieved_at=retrieved_at,
                    available_at=None,
                    source_family="search_excerpt",
                    content_hash=digest,
                    excerpt=text[:3000],
                    verification="excerpt_matched",
                    limitations=[
                        "Search excerpt only; full page not verified.",
                        "Publication time was not supplied by the source.",
                    ],
                )
            )
            if len(items) >= MAX_EVIDENCE_ITEMS:
                return items
    return items


def drop_future_evidence(
    items: list[EvidenceItem], as_of: datetime
) -> tuple[list[EvidenceItem], list[str]]:
    """Point-in-time replay: evidence published or available after ``as_of`` is rejected."""
    kept, rejected = [], []
    for item in items:
        later = [t for t in (item.published_at, item.available_at) if t is not None and t > as_of]
        if later:
            rejected.append(item.evidence_id)
        else:
            kept.append(item)
    return kept, rejected
