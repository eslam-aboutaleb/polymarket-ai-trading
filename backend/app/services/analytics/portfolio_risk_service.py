"""
Portfolio risk report — deterministic metrics plus an AI narrative.

Every number in the report is computed here, in code, so the UI can never
present a figure the wallet data does not support:

* **portfolio value** = ``usdc_balance + Σ (size * curPrice)`` — the same
  definition Kelly uses for bankroll (``kelly_service.compute_bankroll``);
* **loss bound** = Σ (size * avgPrice) — the cost basis, i.e. the most a
  binary position can lose. It is a *bound*, not a VaR;
* **concentration** = per-position market value / portfolio value, flagged
  above 20%;
* **clusters** — a deterministic first pass grouping by ``condition_id``
  (YES/NO legs of one market) and by shared significant title tokens;
* **catalysts** — market end dates fetched from the Gamma markets feed (the
  ``markets`` table's ``end_date`` column is never populated), 30-day window;
* **risk limits** — the live ``UserSettings`` caps, echoed into the payload so
  the LLM narrates the user's actual limits.

The LLM only narrates. When it is unavailable the route still returns the
metrics with ``narrative: null`` — a degraded report beats a 500.
"""

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import get_settings
from app.services.market_data.polymarket_service import get_polymarket_service
from app.utils.cache import get_cache
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

#: Gamma API root — the markets feed is the only source of live end dates.
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"

#: Per-position weight above this share of portfolio value is flagged.
CONCENTRATION_THRESHOLD = 0.20

#: Catalysts further out than this are dropped from the report.
CATALYST_WINDOW_DAYS = 30

#: Positions sharing at least this many significant title tokens cluster.
TITLE_TOKEN_OVERLAP = 2

#: Narrative cache TTL — the LLM call is the expensive half of the report.
NARRATIVE_CACHE_TTL_SECONDS = 900

#: Cap on the list fields of the narrative, so one chatty model cannot blow
#: up the response payload.
NARRATIVE_LIST_LIMIT = 8

#: Coarse time bucket for the narrative cache key (seconds). Keying on the
#: payload alone meant a single price tick changed the hash and forced a new
#: LLM call on every page load; bucketing the clock to this width keeps the
#: cache useful while still letting a stale narrative expire.
NARRATIVE_CACHE_BUCKET_SECONDS = 3600

#: Narrative cache, keyed by user + hour + a hash of the positions payload.
#: The payload embeds live prices, so a payload-only key expired on every
#: tick and the "cached" narrative was really an LLM call per page load; the
#: hour bucket is what makes the entry survive a refresh. The user id is part
#: of the key so one user's narrative is never served to another.
narrative_cache = get_cache("risk_report")

# Titles share boilerplate ("will", "the", "before the end of"); those tokens
# carry no clustering signal, so they are dropped before the overlap test.
_TITLE_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "from",
        "will",
        "would",
        "shall",
        "into",
        "over",
        "under",
        "than",
        "then",
        "when",
        "what",
        "who",
        "whom",
        "whose",
        "how",
        "why",
        "are",
        "was",
        "were",
        "been",
        "being",
        "have",
        "has",
        "had",
        "does",
        "did",
        "doing",
        "can",
        "could",
        "may",
        "might",
        "must",
        "not",
        "yes",
        "any",
        "all",
        "its",
        "his",
        "her",
        "their",
        "our",
        "your",
        "him",
        "she",
        "them",
        "they",
        "you",
        "win",
        "wins",
        "winner",
        "winners",
        "lose",
        "loss",
        "market",
        "markets",
        "polymarket",
        "price",
        "prices",
        "before",
        "after",
        "end",
        "endOf",
        "by",
        "on",
        "in",
        "at",
        "to",
        "of",
        "a",
        "an",
        "be",
        "is",
        "it",
        "as",
        "or",
        "if",
        "so",
        "no",
        "up",
        "out",
        "per",
        "vs",
    }
)

# Prompt sections (risk_management.portfolio_risk_report) → narrative field.
# The prompt's "Event calendar risks" section is deliberately not surfaced as a
# narrative field: the deterministic ``metrics.catalysts`` table is the honest
# source for upcoming resolution dates.
_SECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("var_estimate", re.compile(r"\bvar\b|value[- ]at[- ]risk|worst\s*5%", re.IGNORECASE)),
    ("concentration_flags", re.compile(r"concentration", re.IGNORECASE)),
    ("correlated_clusters", re.compile(r"correlat", re.IGNORECASE)),
    ("catalysts", re.compile(r"catalyst|event calendar|calendar risk", re.IGNORECASE)),
    ("tail_scenarios", re.compile(r"tail|black[- ]?swan|scenario", re.IGNORECASE)),
    ("hedges", re.compile(r"hedg|position adjustment", re.IGNORECASE)),
)

# A line is a section header when it is markdown/numbered, or when it is short
# and carries a section keyword — bullets inside a section must not match.
_HEADER_MARKER = re.compile(r"^(?:#{1,6}\s*|\*\*|__|\d+[\.\)]\s*)")


# --------------------------------------------------------------------------
# scalar helpers
# --------------------------------------------------------------------------


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> float | None:
    """Coerce to a float, or ``None`` when the setting is unset."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, assuming UTC when naive."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _position_title(position: dict) -> str:
    for key in ("title", "market", "question"):
        value = position.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(position.get("condition_id") or position.get("asset_id") or "Unknown market")


def _position_condition_id(position: dict) -> str:
    """Condition ids arrive as ``condition_id`` (CLOB path) or ``conditionId``."""
    for key in ("condition_id", "conditionId"):
        value = position.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _member_label(rows: list[dict], index: int) -> str:
    """Human label for a cluster member — YES/NO legs share a market title."""
    row = rows[index]
    title = row["market_title"]
    outcome = row.get("outcome") or ""
    return f"{title} ({outcome})" if outcome else title


# --------------------------------------------------------------------------
# positions
# --------------------------------------------------------------------------


def prepare_positions(
    raw_positions: list | None,
    max_positions: int = 25,
) -> list[dict]:
    """Normalise, rank and cap the raw Polymarket position rows.

    Rows arrive in both the authenticated (``avgPrice``/``curPrice``) and the
    Data-API (``avg_price``/``cur_price``/``price``) shapes, so both spellings
    are accepted. Positions are ranked by market value so the cap keeps the
    largest exposures, and dust positions are dropped.
    """
    rows: list[dict] = []
    for raw in raw_positions or []:
        if not isinstance(raw, dict):
            continue
        size = _to_float(raw.get("size"))
        if size <= 0:
            continue
        avg_price = _to_float(raw.get("avgPrice", raw.get("avg_price")))
        cur_price = _to_float(raw.get("curPrice", raw.get("cur_price", raw.get("price"))))
        if cur_price <= 0:
            # A missing live price must not invent a gain or a loss: fall back
            # to the entry price so the position carries its cost basis only.
            cur_price = avg_price
        rows.append(
            {
                "market_title": _position_title(raw),
                "condition_id": _position_condition_id(raw),
                "outcome": str(raw.get("outcome") or "").strip(),
                "size": round(size, 6),
                "avg_price": round(avg_price, 6),
                "current_price": round(cur_price, 6),
                "value": round(size * cur_price, 4),
                "cost_basis": round(size * avg_price, 4),
                "loss_bound": round(size * avg_price, 4),
                "weight": 0.0,
                "concentration_flag": False,
            }
        )

    rows.sort(key=lambda row: row["value"], reverse=True)
    if max_positions and max_positions > 0:
        rows = rows[:max_positions]
    return rows


def _title_tokens(title: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", title.lower())
    return {token for token in tokens if len(token) > 2 and token not in _TITLE_STOPWORDS}


def cluster_positions(rows: list[dict]) -> list[dict]:
    """Group positions that move together.

    Two deterministic passes: exact ``condition_id`` (a market's YES and NO
    legs are the same underlying exposure) and shared significant title
    tokens. Positions carry no category/event field, so this is a heuristic —
    the LLM does the semantic clustering over the already-grouped input.
    """
    count = len(rows)
    parent = list(range(count))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    by_condition: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        condition_id = row["condition_id"]
        if condition_id:
            by_condition.setdefault(condition_id, []).append(index)
    for indexes in by_condition.values():
        for index in indexes[1:]:
            union(indexes[0], index)

    tokens = [_title_tokens(row["market_title"]) for row in rows]
    for left in range(count):
        for right in range(left + 1, count):
            if find(left) == find(right):
                continue
            if len(tokens[left] & tokens[right]) >= TITLE_TOKEN_OVERLAP:
                union(left, right)

    groups: dict[int, list[int]] = {}
    for index in range(count):
        groups.setdefault(find(index), []).append(index)

    clusters: list[dict] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda index: rows[index]["value"], reverse=True)
        clusters.append(
            {
                "label": rows[members[0]]["market_title"],
                "members": [_member_label(rows, index) for index in members],
                "weight": round(sum(rows[index]["weight"] for index in members), 6),
            }
        )
    clusters.sort(key=lambda cluster: cluster["weight"], reverse=True)
    return clusters


def build_catalysts(
    rows: list[dict],
    end_dates: dict[str, str] | None,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Upcoming resolution dates inside the catalyst window, soonest first."""
    moment = now or utc_now()
    seen: set[str] = set()
    catalysts: list[dict] = []
    for row in rows:
        condition_id = row["condition_id"]
        if not condition_id or condition_id in seen:
            continue
        end_date = _parse_datetime((end_dates or {}).get(condition_id))
        if end_date is None:
            continue
        days = (end_date - moment).days
        if days < 0 or days > CATALYST_WINDOW_DAYS:
            continue
        seen.add(condition_id)
        catalysts.append(
            {
                "market_title": row["market_title"],
                "end_date": end_date.date().isoformat(),
                "days_to_expiry": days,
            }
        )
    catalysts.sort(key=lambda item: (item["days_to_expiry"], item["market_title"]))
    return catalysts


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------


def compute_metrics(
    rows: list[dict],
    usdc_balance: float,
    *,
    end_dates: dict[str, str] | None = None,
    risk_limits: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Deterministic portfolio risk metrics for the prepared position rows."""
    positions_value = sum(row["value"] for row in rows)
    unrealized_pnl = sum(row["size"] * (row["current_price"] - row["avg_price"]) for row in rows)
    portfolio_value = _to_float(usdc_balance) + positions_value
    loss_bound = sum(row["cost_basis"] for row in rows)

    for row in rows:
        weight = row["value"] / portfolio_value if portfolio_value > 0 else 0.0
        row["weight"] = round(weight, 6)
        row["concentration_flag"] = weight > CONCENTRATION_THRESHOLD

    return {
        "portfolio_value": round(portfolio_value, 4),
        "usdc_balance": round(_to_float(usdc_balance), 4),
        "unrealized_pnl": round(unrealized_pnl, 4),
        "positions_count": len(rows),
        "loss_bound": round(loss_bound, 4),
        "max_position_weight": round(max((row["weight"] for row in rows), default=0.0), 6),
        "concentration_flags": sum(1 for row in rows if row["concentration_flag"]),
        "positions": rows,
        "clusters": cluster_positions(rows),
        "catalysts": build_catalysts(rows, end_dates, now=now),
        "risk_limits": risk_limits or {},
    }


def risk_limits_from_settings(user_settings: Any) -> dict[str, Any]:
    """Echo the live UserSettings risk caps, so the LLM narrates real limits.

    ``None`` values are dropped: the risk_limits map is typed as
    ``number | string | boolean`` on the wire.
    """
    if user_settings is None:
        return {}

    limits: dict[str, Any] = {
        "max_drawdown_pct": _optional_float(getattr(user_settings, "max_drawdown_pct", None)),
        "total_loss_halt_pct": _optional_float(getattr(user_settings, "total_loss_halt_pct", None)),
        "monthly_loss_limit": _optional_float(getattr(user_settings, "monthly_loss_limit", None)),
        "max_position_size": _optional_float(getattr(user_settings, "max_position_size", None)),
        "daily_loss_limit": _optional_float(getattr(user_settings, "daily_loss_limit", None)),
        "peak_capital": _optional_float(getattr(user_settings, "peak_capital", None)),
        "initial_capital": _optional_float(getattr(user_settings, "initial_capital", None)),
        "trading_halted": bool(getattr(user_settings, "trading_halted", False)),
        "halt_reason": str(getattr(user_settings, "halt_reason", "") or ""),
        "concentration_threshold_pct": int(CONCENTRATION_THRESHOLD * 100),
        "paper_balance": _optional_float(getattr(user_settings, "paper_balance", None)),
    }

    simulation_mode = bool(getattr(user_settings, "simulation_mode", False))
    limits["simulation_mode"] = simulation_mode
    limits["mode"] = "paper" if simulation_mode else "live"

    cooldown_until = getattr(user_settings, "cooldown_until", None)
    if cooldown_until is not None:
        limits["cooldown_until"] = (
            cooldown_until.isoformat()
            if hasattr(cooldown_until, "isoformat")
            else str(cooldown_until)
        )

    return {key: value for key, value in limits.items() if value is not None and value != ""}


# --------------------------------------------------------------------------
# narrative
# --------------------------------------------------------------------------


def build_positions_payload(metrics: dict[str, Any]) -> str:
    """Serialise the deterministic report as the prompt's ``positions_json``.

    The template takes a single placeholder, so the metrics, clusters,
    catalysts and live risk limits travel with the positions — the LLM can
    reference the real numbers instead of inventing its own.
    """
    payload = {
        "portfolio_value": metrics.get("portfolio_value"),
        "usdc_balance": metrics.get("usdc_balance"),
        "unrealized_pnl": metrics.get("unrealized_pnl"),
        "positions_count": metrics.get("positions_count"),
        "loss_bound": metrics.get("loss_bound"),
        "max_position_weight": metrics.get("max_position_weight"),
        "concentration_flags": metrics.get("concentration_flags"),
        "positions": metrics.get("positions", []),
        "clusters": metrics.get("clusters", []),
        "catalysts": metrics.get("catalysts", []),
        "risk_limits": metrics.get("risk_limits", {}),
    }
    return json.dumps(payload, default=str)


def _strip_header_marker(line: str) -> str:
    return re.sub(r"^(?:#{1,6}\s*|\d+[\.\)]\s*|\*\*|__)", "", line).strip().strip("*_").strip()


def _looks_like_header(line: str) -> bool:
    if _HEADER_MARKER.match(line):
        return True
    if len(line) > 80 or line.rstrip().endswith((".", ";", ",")):
        return False
    return len(line.split()) <= 12


def _section_for_line(line: str) -> str | None:
    """First section whose keyword this header line names."""
    for field, pattern in _SECTION_PATTERNS:
        if pattern.search(_strip_header_marker(line)):
            return field
    return None


def _bullet_items(lines: list[str]) -> list[str]:
    items: list[str] = []
    for line in lines:
        cleaned = re.sub(r"^(?:[-*•]\s+|\d+[\.\)]\s+|#{1,6}\s*|\*\*|__)", "", line)
        cleaned = cleaned.strip().strip("*_").strip()
        if cleaned:
            items.append(cleaned)
    return items


def _prose(lines: list[str]) -> str:
    return " ".join(_bullet_items(lines)).strip()


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        parts = re.split(r"\n+|;\s*", value)
        return [part.strip(" -*•") for part in parts if part.strip(" -*•")]
    return []


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "; ".join(_as_list(value))
    return ""


def _first_key(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, "", [], {}):
            return payload[key]
    return None


def _parse_narrative_json(text: str) -> dict[str, Any] | None:
    """Read a structured JSON answer, or ``None`` when it is prose."""
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if _first_key(parsed, "overall_risk_rating", "risk_rating", "var_estimate") is None:
        return None
    return {
        "overall_risk_rating": _as_text(
            _first_key(parsed, "overall_risk_rating", "overall_rating", "risk_rating", "rating")
        ),
        "var_estimate": _as_text(
            _first_key(parsed, "var_estimate", "var", "value_at_risk", "portfolio_var")
        ),
        "concentration_flags": _as_list(
            _first_key(parsed, "concentration_flags", "concentration_risks", "concentration")
        ),
        "correlated_clusters": _as_list(
            _first_key(
                parsed, "correlated_clusters", "correlated_positions", "correlation", "clusters"
            )
        ),
        "tail_scenarios": _as_list(
            _first_key(parsed, "tail_scenarios", "tail_risks", "black_swan_scenarios")
        ),
        "hedges": _as_list(
            _first_key(parsed, "hedges", "recommended_hedges", "recommendations", "adjustments")
        ),
    }


def parse_narrative(report_text: str | None) -> dict[str, Any] | None:
    """Split the LLM report into the narrative fields, or ``None`` if empty.

    The prompt asks for six numbered sections in prose; a model that answers
    with JSON instead is handled by ``_parse_narrative_json``.
    """
    text = (report_text or "").strip()
    if not text:
        return None

    structured = _parse_narrative_json(text)
    if structured is not None:
        return structured

    sections: dict[str, list[str]] = {}
    rating_lines: list[str] = []
    current: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        field = _section_for_line(line) if _looks_like_header(line) else None
        if field and field not in sections:
            sections[field] = []
            current = field
            remainder = _strip_header_marker(line)
            if len(remainder.split()) > 6:
                sections[field].append(remainder)
            continue
        if current is None:
            rating_lines.append(line)
        else:
            sections[current].append(line)

    narrative = {
        "overall_risk_rating": _prose(rating_lines)[:400],
        "var_estimate": _prose(sections.get("var_estimate", []))[:400],
        "concentration_flags": _bullet_items(sections.get("concentration_flags", [])),
        "correlated_clusters": _bullet_items(sections.get("correlated_clusters", [])),
        "tail_scenarios": _bullet_items(sections.get("tail_scenarios", [])),
        "hedges": _bullet_items(sections.get("hedges", [])),
    }
    for key in ("concentration_flags", "correlated_clusters", "tail_scenarios", "hedges"):
        narrative[key] = narrative[key][:NARRATIVE_LIST_LIMIT]
    return narrative


def _payload_fingerprint(positions_payload: str) -> str:
    """Hash of the payload's *composition*, with every live price dropped.

    The payload carries ``current_price``, ``value``, ``weight``,
    ``unrealized_pnl`` and ``portfolio_value`` — all tick-derived — so
    hashing it verbatim invalidated the narrative cache on every price
    move. What the narrative actually describes is the book's shape: which
    markets, how big, how clustered, what resolves soon. This projection
    keeps that and discards the prices, so a re-priced but unchanged book
    reuses one narrative.
    """
    try:
        payload = json.loads(positions_payload)
    except (TypeError, ValueError):
        # Not the JSON this function builds (a caller passing raw text): fall
        # back to the whole payload rather than guessing at its shape.
        return hashlib.sha256(positions_payload.encode()).hexdigest()

    composition = {
        "positions_count": payload.get("positions_count"),
        "positions": [
            {
                "market_title": row.get("market_title"),
                "condition_id": row.get("condition_id"),
                "outcome": row.get("outcome"),
                "size": row.get("size"),
            }
            for row in payload.get("positions", [])
            if isinstance(row, dict)
        ],
        "clusters": payload.get("clusters"),
        "catalysts": payload.get("catalysts"),
        "risk_limits": payload.get("risk_limits"),
    }
    return hashlib.sha256(json.dumps(composition, sort_keys=True, default=str).encode()).hexdigest()


def _narrative_cache_key(
    positions_payload: str,
    user_id: int | None,
    *,
    now: datetime | None = None,
) -> str:
    """Cache key for one user's narrative at the current hour.

    Three parts, each load-bearing:

    * ``user_id`` — the narrative describes that user's wallet, so two users
      must never share an entry (and one user's narrative must never be
      served to another);
    * an hour bucket — bounds how stale a narrative can be. Re-opening the
      page inside the hour hits this entry instead of the model, which is
      what stops the "one LLM call per page load" behaviour;
    * the composition fingerprint — a materially different book still gets
      its own narrative inside the same hour, while a mere price tick does
      not.
    """
    moment = now or utc_now()
    bucket = int(moment.timestamp()) // NARRATIVE_CACHE_BUCKET_SECONDS
    fingerprint = _payload_fingerprint(positions_payload)
    return f"{user_id if user_id is not None else 'anon'}:{bucket}:{fingerprint}"


async def generate_narrative(
    positions_payload: str,
    user_id: int | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Narrate the deterministic payload; ``None`` when the LLM is unusable.

    Failures are swallowed on purpose: the deterministic metrics are the
    report, the narrative is a bonus. Successful answers are cached per user
    + hour + payload hash, so re-opening the page does not re-bill the model
    (the payload embeds live prices, so hashing it alone would invalidate the
    cache on every tick).
    """
    cache_key = "narrative:" + _narrative_cache_key(positions_payload, user_id, now=now)
    cached = narrative_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        from app.grpc_clients.analysis_client import get_analysis_client

        client = get_analysis_client()
        result = await client.generate_risk_report(positions_json=positions_payload)
    except Exception as exc:
        logger.warning("Risk report narrative generation failed: %s", exc)
        return None

    if not isinstance(result, dict) or not result.get("success"):
        return None

    narrative = parse_narrative(result.get("report"))
    if narrative is None:
        return None

    narrative_cache.set(cache_key, narrative, ttl_seconds=NARRATIVE_CACHE_TTL_SECONDS)
    return narrative


# --------------------------------------------------------------------------
# data fetching
# --------------------------------------------------------------------------


async def _fetch_usdc_balance(
    service: Any,
    wallet_address: str,
    private_key: str | None,
    clob_creds: dict | None,
) -> float:
    try:
        balance = await service.get_wallet_balance(
            wallet_address,
            private_key=private_key,
            clob_creds=clob_creds,
        )
    except Exception as exc:
        logger.warning("Risk report balance fetch failed for %s: %s", wallet_address, exc)
        return 0.0
    return _to_float((balance or {}).get("usdc_balance"))


async def _fetch_positions(
    service: Any,
    wallet_address: str,
    private_key: str | None,
    clob_creds: dict | None,
) -> list:
    try:
        positions = await service.get_positions(
            wallet_address,
            private_key=private_key,
            clob_creds=clob_creds,
        )
    except Exception as exc:
        logger.warning("Risk report positions fetch failed for %s: %s", wallet_address, exc)
        return []
    return positions if isinstance(positions, list) else []


async def fetch_market_end_dates(condition_ids: list[str]) -> dict[str, str]:
    """Resolve ``condition_id → endDate`` for the held markets.

    Uses the Gamma markets feed: the ``markets`` table's ``end_date`` column is
    never populated, and the summary endpoint truncates positions to 20.
    Best-effort — an unreachable feed yields no catalysts, never an error.
    """
    unique_ids = [cid for cid in dict.fromkeys(condition_ids) if cid]
    if not unique_ids:
        return {}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{POLYMARKET_GAMMA_API}/markets",
                params={"condition_ids": unique_ids, "limit": len(unique_ids)},
            )
        if response.status_code != 200:
            logger.warning("Gamma /markets returned %s", response.status_code)
            return {}
        payload = response.json()
    except Exception as exc:
        logger.warning("Gamma market end-date lookup failed: %s", exc)
        return {}

    markets = payload if isinstance(payload, list) else (payload or {}).get("markets", [])
    end_dates: dict[str, str] = {}
    for market in markets if isinstance(markets, list) else []:
        if not isinstance(market, dict):
            continue
        condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
        end_date = market.get("endDate") or market.get("end_date_iso") or market.get("endDateIso")
        if condition_id and end_date:
            end_dates[condition_id] = str(end_date)
    return end_dates


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------


async def build_risk_report(
    service: Any = None,
    wallet_address: str = "",
    *,
    private_key: str | None = None,
    clob_creds: dict | None = None,
    user_settings: Any = None,
    include_narrative: bool = True,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Build the full risk report for a wallet.

    Deterministic metrics are always returned; the narrative is ``None`` when
    the LLM is unavailable. Wallet fetches degrade to zero rather than failing
    the request.
    """
    service = service or get_polymarket_service()
    max_positions = get_settings().risk_report_max_positions

    usdc_balance = await _fetch_usdc_balance(service, wallet_address, private_key, clob_creds)
    positions = await _fetch_positions(service, wallet_address, private_key, clob_creds)
    rows = prepare_positions(positions, max_positions)

    end_dates = await fetch_market_end_dates(
        [row["condition_id"] for row in rows if row["condition_id"]]
    )

    metrics = compute_metrics(
        rows,
        usdc_balance,
        end_dates=end_dates,
        risk_limits=risk_limits_from_settings(user_settings),
    )

    narrative: dict[str, Any] | None = None
    if include_narrative and metrics["positions_count"] > 0:
        # An empty portfolio has nothing to narrate — and an LLM asked to
        # describe risk with no data invents it.
        narrative = await generate_narrative(build_positions_payload(metrics), user_id)

    return {
        "metrics": metrics,
        "narrative": narrative,
        "generated_at": utc_now().isoformat(),
    }
