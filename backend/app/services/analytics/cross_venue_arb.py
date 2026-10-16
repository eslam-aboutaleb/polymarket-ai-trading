"""Cross-venue mismatch detection (plan 05 Phase B).

**Signal-only.** This module compares the yes price of the same question
on two venues and reports the gap. It never places, cancels or sizes an
order, never bridges two venues, and never assumes a trade is available:
a mismatch is a question for a human ("is this the same market, and can
I actually act on it?"), not an instruction.

The comparison is a **pure function over DB rows**: ``mismatches_from_
rows`` takes the already-serialized ``venue_markets`` rows and returns
plain dicts, so the math is unit-testable without a database, a network
call or a clock. ``find_mismatches`` only adds the query.

Three properties make the output safe to act on, and they are the whole
design:

* **Both questions travel verbatim.** A wrong match is the main source
  of a fake edge (plan 05 Risks), so every mismatch carries each side's
  question as the venue wrote it, plus how the two were matched (exact
  normalized key, or fuzzy token overlap) and the overlap score.
* **Money-ness is respected.** A Manifold (play-money) price is not the
  executable leg of a signal, so a real-money/play-money pair is never
  returned by default. ``include_play_money=True`` returns those pairs
  with ``executable_signal=False`` and the offending venues named, so
  the sentiment reading is still visible without being actionable.
* **The gap is a price gap, not a profit.** ``min_edge`` is compared
  against ``max(yes) − min(yes)`` in the same currency. No fee, spread
  or fill model is applied, so the reported edge is an upper bound.

The module also gives the **intra-Polymarket** detector a consumer:
``get_intra_polymarket_signals`` reads ``arbitrage_service``'s recent
complement/spread spots instead of re-deriving them, so both kinds of
signal reach a reviewer through one read-only surface. That service is
untouched by this plan — it stays the owner of its own scan.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from sqlalchemy.orm import Session

from app.repositories.market_data.venues import VenueMarketRepository
from app.services.market_data.venue_market_service import normalize_question, serialize_venue_market

logger = logging.getLogger(__name__)

# Default gap (in price units) below which a pair is not worth a human's
# attention. Two venues quoting the same question rarely agree to the cent.
DEFAULT_MIN_EDGE = 0.03
DEFAULT_MIN_LIQUIDITY = 0.0
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

# Cost guard (master 5.8): the mismatch scan is O(rows + candidates), so
# both the row read and the per-row comparison count are capped before
# any pairing happens.
MAX_SCAN_ROWS = 2000
MAX_CANDIDATES_PER_ROW = 200
# A token carried by this many rows is a series label ("maximum",
# "temperature"), not question detail: at that frequency it is not
# evidence of a match and counting it would flood the candidate set on
# a table dominated by one series. The rule only bites on tables far
# larger than a three-adapter refresh (≤200 rows per venue), where the
# exact-key path and the numeric veto already cover the real cases.
MAX_TOKEN_POSTINGS = 500
QUESTION_CACHE_SIZE = 4096

# Fuzzy-match thresholds. Both sides must be content-rich enough that
# shared boilerplate ("will the … be") cannot manufacture a match, and
# the overlap must be high enough that one differing clause is fatal.
MIN_SHARED_TOKENS = 4
MIN_OVERLAP_SCORE = 0.75

# Two legs of the "same" question that close more than this far apart
# are flagged, not filtered: it usually means the questions differ by a
# horizon ("in December" vs "by June") and the gap is not one trade.
HORIZON_TOLERANCE_DAYS = 7

# Words that carry no venue-specific meaning. Removing them keeps a
# question's *content* tokens from being diluted by English scaffolding,
# which is what makes the overlap score meaningful.
STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "be",
        "been",
        "being",
        "is",
        "are",
        "was",
        "were",
        "will",
        "would",
        "can",
        "could",
        "shall",
        "should",
        "do",
        "does",
        "did",
        "if",
        "then",
        "than",
        "that",
        "this",
        "there",
        "it",
        "its",
        "as",
        "with",
        "from",
        "before",
        "after",
        "any",
        "no",
        "not",
    }
)

MATCH_EXACT = "exact"
MATCH_FUZZY = "fuzzy"

PLAY_MONEY_REASON = (
    "play-money price: a Manifold quote is sentiment, not cash, so it can never be "
    "the executable leg of a cross-venue signal"
)


def _tokens(question: str) -> set[str]:
    """Content tokens: lowercase words minus English scaffolding."""
    words = {word for word in _split(question) if word not in STOPWORDS}
    return {word for word in words if len(word) > 1}


def _split(question: str) -> list[str]:
    """Lowercase alphanumeric tokens, punctuation and symbols dropped."""
    text = (question or "").lower()
    current: list[str] = []
    tokens: list[str] = []
    for char in text:
        if char.isalnum():
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _numbers(question: str) -> set[str]:
    """Numeric tokens (dates, thresholds, years, scores) as strings."""
    return {word for word in _split(question) if word.isdigit()}


@dataclass(frozen=True)
class _Question:
    """One question's match facts, computed once and reused.

    Matching is the hot path — a full cache table is thousands of
    candidate pairs — so the normalized key, the content tokens and the
    numeric tokens are memoized per distinct question text instead of
    being re-derived for every comparison.
    """

    text: str
    key: str
    tokens: frozenset[str]
    numbers: frozenset[str]

    def shared_with(self, other: _Question) -> frozenset[str]:
        """Content tokens the two questions have in common."""
        return self.tokens & other.tokens


@lru_cache(maxsize=QUESTION_CACHE_SIZE)
def _question(text: str) -> _Question:
    return _Question(
        text=text,
        key=normalize_question(text),
        tokens=frozenset(_tokens(text)),
        numbers=frozenset(_numbers(text)),
    )


def _row_question(row: Mapping[str, Any]) -> _Question:
    return _question(str(row.get("question") or ""))


def token_overlap_score(left: str, right: str) -> float:
    """Overlap coefficient of two questions' content tokens, in [0, 1]."""
    left_question = _question(left)
    right_question = _question(right)
    if not left_question.tokens or not right_question.tokens:
        return 0.0
    shared = len(left_question.shared_with(right_question))
    return shared / min(len(left_question.tokens), len(right_question.tokens))


def _match(
    left: _Question,
    right: _Question,
    *,
    shared: int | None = None,
) -> dict[str, Any] | None:
    """Match decision for two question facts (``None`` when they differ).

    Exact matching is the phase-A normalized key. Fuzzy matching is a
    content-token overlap coefficient with two guards that keep same
    *series* markets apart: at least ``MIN_SHARED_TOKENS`` shared tokens,
    and no conflicting numbers. "…be **OVER 44.5** points" and
    "…be **OVER 43.5** points" share almost every token but are
    different markets, so a numeric disagreement vetoes the match.

    Both guards fail **closed**: they cost a missed match ("$100,000"
    vs "100000") rather than a wrong one. A missed match is a review
    nobody sees; a wrong match is a trade made on a market that does not
    exist.

    ``shared`` lets the caller pass the overlap it already computed from
    the inverted index; when it is omitted the intersection is computed
    here, which is the path ``match_questions`` takes.
    """
    if left.key and left.key == right.key:
        return {
            "match_type": MATCH_EXACT,
            "match_score": 1.0,
            "shared_tokens": sorted(left.shared_with(right)),
        }
    overlap = len(left.shared_with(right)) if shared is None else shared
    if overlap < MIN_SHARED_TOKENS:
        return None
    score = overlap / min(len(left.tokens), len(right.tokens))
    if score < MIN_OVERLAP_SCORE:
        return None
    if left.numbers and right.numbers and left.numbers != right.numbers:
        return None
    return {
        "match_type": MATCH_FUZZY,
        "match_score": round(score, 4),
        "shared_tokens": sorted(left.shared_with(right)),
    }


def match_questions(left: str, right: str) -> dict[str, Any] | None:
    """Whether two question texts describe the same market, and how well."""
    return _match(_question(left or ""), _question(right or ""))


def _price(row: Mapping[str, Any], key: str) -> float | None:
    value = row.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _float(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _datetime(value: Any) -> datetime | None:
    """A row's end date as aware UTC (ISO strings or datetimes)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _venue(row: Mapping[str, Any]) -> str:
    return str(row.get("venue") or "")


def _row_shape(row: Mapping[str, Any]) -> dict[str, Any]:
    """The per-venue half of a mismatch (venue, question, price, liquidity)."""
    return {
        "venue": _venue(row),
        "market_id": str(row.get("market_id") or ""),
        "question": str(row.get("question") or ""),
        "yes_price": _price(row, "yes_price"),
        "no_price": _price(row, "no_price"),
        "volume_24h": _float(row.get("volume_24h")),
        "liquidity": _float(row.get("liquidity")),
        "url": str(row.get("url") or ""),
        "is_real_money": bool(row.get("is_real_money", True)),
        "fetched_at": row.get("fetched_at"),
    }


def _horizon_days(low: Mapping[str, Any], high: Mapping[str, Any]) -> float | None:
    """Days between the two legs' close dates, None when either is missing."""
    left = _datetime(low.get("end_date"))
    right = _datetime(high.get("end_date"))
    if left is None or right is None:
        return None
    return round(abs((left - right).total_seconds()) / 86400.0, 2)


def _mismatch(
    low: Mapping[str, Any],
    high: Mapping[str, Any],
    match: Mapping[str, Any],
) -> dict[str, Any]:
    """One pair of venues quoting the same question at different prices."""
    low_price = _price(low, "yes_price")
    high_price = _price(high, "yes_price")
    edge = (high_price or 0.0) - (low_price or 0.0)
    real_money = bool(low.get("is_real_money", True)) and bool(high.get("is_real_money", True))
    play_money_venues = sorted(
        venue
        for venue, row in ((_venue(low), low), (_venue(high), high))
        if not bool(row.get("is_real_money", True))
    )
    stamps = [row.get("fetched_at") for row in (low, high) if row.get("fetched_at")]
    questions = {
        _venue(low): str(low.get("question") or ""),
        _venue(high): str(high.get("question") or ""),
    }
    horizon = _horizon_days(low, high)
    return {
        "venues": sorted({_venue(low), _venue(high)}),
        "match_type": match["match_type"],
        "match_score": match["match_score"],
        "shared_tokens": list(match["shared_tokens"]),
        # Both questions verbatim, cheapest side first — a reviewer must
        # be able to confirm the match without leaving the row.
        "cheap": _row_shape(low),
        "rich": _row_shape(high),
        "questions": questions,
        "edge": round(edge, 4),
        "edge_pct": round(edge * 100.0, 2),
        "low_price": low_price,
        "high_price": high_price,
        "min_liquidity": round(min(_float(low.get("liquidity")), _float(high.get("liquidity"))), 4),
        # A horizon gap is the cheapest way to catch a wrong match, so
        # it travels with the pair rather than being filtered away.
        "end_date_delta_days": horizon,
        "horizon_mismatch": horizon is not None and horizon > HORIZON_TOLERANCE_DAYS,
        # A play-money side can never be the executable leg.
        "executable_signal": bool(real_money and low_price is not None and high_price is not None),
        "play_money_venues": play_money_venues,
        "play_money_only": len(play_money_venues) == 2,
        "play_money_reason": PLAY_MONEY_REASON if play_money_venues else None,
        "newest_fetched_at": max(stamps) if stamps else None,
    }


def _passes_thresholds(
    mismatch: Mapping[str, Any],
    min_edge: float,
    min_liquidity: float,
    include_play_money: bool,
) -> bool:
    if not mismatch["executable_signal"] and not include_play_money:
        return False
    if mismatch["edge"] < min_edge:
        return False
    return mismatch["min_liquidity"] >= min_liquidity


def _price_sort_key(row: Mapping[str, Any]) -> tuple:
    """Order rows by yes price, then liquidity, so the cheap side comes first."""
    price = _price(row, "yes_price")
    return (price is None, price if price is not None else 0.0, -_float(row.get("liquidity")))


def _candidate_counts(
    position: int,
    *,
    questions: Sequence[_Question],
    keys: Mapping[str, list[int]],
    tokens: Mapping[str, list[int]],
) -> dict[int, int]:
    """Other positions that can match this one, with their shared-token count.

    The token index bounds the fuzzy scan to rows that actually share
    content, so a candidate set stays small on a full table instead of
    degrading into an all-pairs comparison on every request.
    """
    question = questions[position]
    counts: dict[int, int] = defaultdict(int)
    if len(question.tokens) >= MIN_SHARED_TOKENS:
        for token in question.tokens:
            postings = tokens.get(token, ())
            if len(postings) > MAX_TOKEN_POSTINGS:
                continue
            for other in postings:
                if other != position:
                    counts[other] += 1
    # An exact-key match shares every token by definition, so its count
    # is authoritative and overwrites whatever the token loop counted.
    for other in keys.get(question.key, ()):
        if other != position:
            counts[other] = len(question.tokens)
    counts.pop(position, None)
    return counts


def _candidate_pairs(
    rows: Sequence[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any], dict[str, Any]]]:
    """Every cross-venue row pair whose questions match, low side first.

    Same-venue pairs are never compared: two rows from one venue are
    that venue's own book, not a cross-venue signal. Each unordered pair
    is produced once (``other_position > position``), and the cheaper
    side is emitted first so the row a reviewer reads left-to-right goes
    from the low price to the high one.

    The inverted token index makes this O(rows + candidates) rather than
    O(rows²): a row only ever compares against rows that share at least
    ``MIN_SHARED_TOKENS`` of its content words (or its exact key), and a
    per-row cap bounds the pathological case where one venue lists the
    same question in thousands of markets — those pairs are redundant,
    so dropping the overflow costs no distinct information.
    """
    questions = [_row_question(row) for row in rows]
    keys: dict[str, list[int]] = defaultdict(list)
    tokens: dict[str, list[int]] = defaultdict(list)
    for position, question in enumerate(questions):
        if question.key:
            keys[question.key].append(position)
        for token in question.tokens:
            tokens[token].append(position)

    prices = [_price(row, "yes_price") for row in rows]
    venues = [_venue(row) for row in rows]
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any], dict[str, Any]]] = []
    for position, row in enumerate(rows):
        if prices[position] is None:
            continue
        counts = _candidate_counts(position, questions=questions, keys=keys, tokens=tokens)
        ordered = sorted(
            (other for other in counts if other > position),
            # Same-venue rows are dropped outright, so if the cap ever
            # bites, the surviving slots are the ones that can match.
            key=lambda other: (venues[other] == venues[position], -counts[other], other),
        )
        for other_position in ordered[:MAX_CANDIDATES_PER_ROW]:
            other = rows[other_position]
            if venues[other_position] == venues[position] or prices[other_position] is None:
                continue
            match = _match(
                questions[position],
                questions[other_position],
                shared=counts[other_position],
            )
            if match is None:
                continue
            low, high = sorted((row, other), key=_price_sort_key)
            pairs.append((low, high, match))
    return pairs


def mismatches_from_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    min_edge: float = DEFAULT_MIN_EDGE,
    min_liquidity: float = DEFAULT_MIN_LIQUIDITY,
    include_play_money: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Pure mismatch detection over serialized ``venue_markets`` rows.

    ``min_edge`` is a price gap in the same currency and ``min_liquidity``
    is applied to the *weaker* side, because a gap only means something
    when both legs can absorb a trade. Results are ordered by edge, then
    by the weaker side's liquidity, so the most interesting pair is first
    and the order is stable across calls.
    """
    materialized = [row for row in rows if isinstance(row, Mapping)]
    clamped = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    candidates = [
        _mismatch(low, high, match) for low, high, match in _candidate_pairs(materialized)
    ]
    mismatches = [
        mismatch
        for mismatch in candidates
        if _passes_thresholds(mismatch, min_edge, min_liquidity, include_play_money)
    ]
    mismatches.sort(
        key=lambda mismatch: (
            -mismatch["edge"],
            -mismatch["min_liquidity"],
            mismatch["venues"],
        )
    )
    return mismatches[:clamped]


def find_mismatches(
    db: Session,
    *,
    min_edge: float = DEFAULT_MIN_EDGE,
    min_liquidity: float = DEFAULT_MIN_LIQUIDITY,
    include_play_money: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Mismatches across the cached rows of every registered venue.

    Reads at most ``MAX_SCAN_ROWS`` rows, most liquid first: the cache
    is a snapshot, not a warehouse, and an unbounded scan would put an
    unbounded cost on every request. The whole scan is O(rows +
    candidates) — three adapters at the refresh limit (≈600 rows) run
    in ~10 ms, and the pathological ceiling (2000 rows, all sharing
    tokens) stays under a second thanks to the posting and candidate
    caps in ``_candidate_pairs``.
    """
    rows = VenueMarketRepository(db).list_for_comparison(MAX_SCAN_ROWS)
    serialized = [serialize_venue_market(row) for row in rows]
    return mismatches_from_rows(
        serialized,
        min_edge=min_edge,
        min_liquidity=min_liquidity,
        include_play_money=include_play_money,
        limit=limit,
    )


def get_intra_polymarket_signals(limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """Recent intra-Polymarket complement/spread spots, read-only.

    ``arbitrage_service.get_recent_opportunities`` has no consuming
    route, so those detections are log-only dead surface. Reading them
    here — rather than reimplementing the complement or spread check —
    puts both arbitrage kinds on one surface without touching that
    service's scan. The list is in-memory, so this is free and never
    fails: a service that has not run yet simply yields ``[]``.
    """
    try:
        from app.services.analytics.arbitrage_service import get_recent_opportunities

        opportunities = get_recent_opportunities()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Intra-Polymarket signals unavailable: %s", exc)
        return []
    if not isinstance(opportunities, list):
        return []
    clamped = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    return [opportunity for opportunity in opportunities[:clamped] if isinstance(opportunity, dict)]
