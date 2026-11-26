"""Social providers: an authorized X API reader and the indexed-snippet fallback.

Only the provider boundary (HTTP) is replaceable in tests. Metadata is copied only
when the provider returned it: no inferred authenticity, engagement or timestamps.
"""

import hashlib
import re
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from app.services.intelligence.social_evidence.schemas import Engagement, SocialPost

X_API_BASE = "https://api.x.com/2"
_STATUS = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/([0-9]{1,20})/?$")
_INJECTION = re.compile(
    r"(ignore (all |any )?(previous|prior|above)|system prompt|you are now|disregard|"
    r"reveal .*(key|secret|prompt)|execute .*(trade|order)|place an? order)",
    re.I,
)
MAX_TEXT = 1000


class ProviderUnavailable(Exception):
    """Permission, outage or rate limit at the provider. Carries a stable reason code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def text_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).lower().encode()).hexdigest()


def clean_text(raw: str) -> str:
    return "".join(ch for ch in raw if ch == "\n" or ch.isprintable())[:MAX_TEXT]


def parse_status_url(url: str) -> tuple[str, str] | None:
    """(handle, post_id) only for https status links on real X hosts."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in {
        "x.com",
        "www.x.com",
        "twitter.com",
        "mobile.twitter.com",
    }:
        return None
    match = _STATUS.match(parts.path)
    return (match.group(1), match.group(2)) if match else None


def post_url(post_id: str) -> str:
    return f"https://x.com/i/status/{post_id}"


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _count(metrics: dict, key: str) -> int | None:
    value = metrics.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class XApiProvider:
    """Authorized server-side X API v2 reader (bearer token from server settings only)."""

    name = "x_api_v2"

    def __init__(self, client: httpx.AsyncClient, bearer_token: str):
        self._client, self._token = client, bearer_token

    async def _get(self, path: str, params: dict[str, str]) -> dict:
        try:
            response = await self._client.get(
                f"{X_API_BASE}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=10,
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable("SOCIAL_PROVIDER_UNREACHABLE") from exc
        if response.status_code in (401, 403):
            raise ProviderUnavailable("SOCIAL_PROVIDER_PERMISSION_DENIED")
        if response.status_code == 429:
            raise ProviderUnavailable("SOCIAL_PROVIDER_RATE_LIMITED")
        if response.status_code >= 400:
            raise ProviderUnavailable("SOCIAL_PROVIDER_ERROR")
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderUnavailable("SOCIAL_PROVIDER_ERROR") from exc
        return body if isinstance(body, dict) else {}

    def _post(self, tweet: dict, users: dict[str, dict], now: datetime) -> SocialPost | None:
        post_id = tweet.get("id")
        text = tweet.get("text")
        if not isinstance(post_id, str) or not post_id.isdigit() or not isinstance(text, str):
            return None
        author = users.get(str(tweet.get("author_id")), {})
        metrics = tweet.get("public_metrics")
        engagement = None
        if isinstance(metrics, dict):
            engagement = Engagement(
                likes=_count(metrics, "like_count"),
                reposts=_count(metrics, "retweet_count"),
                replies=_count(metrics, "reply_count"),
                quotes=_count(metrics, "quote_count"),
            )
        cleaned = clean_text(text)
        verified = author.get("verified")
        return SocialPost(
            provider=self.name,
            post_id=post_id,
            canonical_url=post_url(post_id),
            author_id=str(tweet["author_id"]) if tweet.get("author_id") else None,
            author_handle=author.get("username"),
            provider_verified=verified if isinstance(verified, bool) else None,
            text=cleaned,
            text_hash=text_hash(cleaned),
            posted_at=_parse_time(tweet.get("created_at")),
            first_seen_at=now,
            observed_at=now,
            engagement=engagement,
            engagement_as_of=now if engagement else None,
            provenance="api_authorized",
            injection_flagged=bool(_INJECTION.search(text)),
        )

    async def search(self, query: str, now: datetime) -> list[SocialPost]:
        body = await self._get(
            "/tweets/search/recent",
            {
                "query": f"{query[:300]} -is:retweet",
                "max_results": "20",
                "tweet.fields": "created_at,public_metrics,author_id",
                "expansions": "author_id",
                "user.fields": "username,verified",
            },
        )
        users = {
            str(u["id"]): u
            for u in (body.get("includes") or {}).get("users", [])
            if isinstance(u, dict) and "id" in u
        }
        posts = [self._post(t, users, now) for t in body.get("data", []) if isinstance(t, dict)]
        return [p for p in posts if p is not None]

    async def refresh(self, post_id: str, now: datetime) -> SocialPost | None:
        """Re-read one post. ``None`` content means deleted/unavailable per the provider."""
        body = await self._get(
            f"/tweets/{post_id}",
            {
                "tweet.fields": "created_at,public_metrics,author_id",
                "expansions": "author_id",
                "user.fields": "username,verified",
            },
        )
        if "data" in body:
            users = {
                str(u["id"]): u
                for u in (body.get("includes") or {}).get("users", [])
                if isinstance(u, dict) and "id" in u
            }
            return self._post(body["data"], users, now)
        return None


def indexed_posts(items: list[dict], now: datetime) -> list[SocialPost]:
    """Fallback: X status links from a search index. No author id, time or engagement exists."""
    posts: dict[str, SocialPost] = {}
    for item in items:
        parsed = parse_status_url(str(item.get("url", "")))
        if not parsed:
            continue
        handle, post_id = parsed
        text = clean_text(str(item.get("snippet") or item.get("title") or ""))
        if post_id in posts or not text:
            continue
        posts[post_id] = SocialPost(
            provider="indexed_search",
            post_id=post_id,
            canonical_url=f"https://x.com/{handle.lower()}/status/{post_id}",
            author_handle=handle,
            text=text,
            text_hash=text_hash(text),
            first_seen_at=now,
            observed_at=now,
            url_aliases=[],
            provenance="indexed_snippet",
            injection_flagged=bool(_INJECTION.search(text)),
        )
    return list(posts.values())
