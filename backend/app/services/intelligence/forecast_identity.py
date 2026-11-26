"""Immutable forecast identity and decision-time provenance (issue #60).

Pure helpers, no database access. A forecast run is identified by the
decision inputs at full precision, never by rounded values and never by the
calendar day, so a replayed SSE event collapses to one row while a genuinely
revised forecast (different probability, evidence, rule, model, prompt or
calibrator) stays its own sample.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: The only probability semantics a forecast row may carry. ``p_yes`` is the
#: probability that the YES outcome of the market resolves true. It is not the
#: probability that an order is profitable; that is a separate quantity.
P_YES = "p_yes"

#: A replayed verdict arrives inside this window of the original. Identical
#: full-precision inputs inside it collapse; the same inputs recorded after it
#: are a new decision.
REPLAY_WINDOW_SECONDS = 1200

#: A benchmark quote older than this at decision time is flagged stale.
MAX_FRESH_QUOTE_AGE_SECONDS = 600


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def parse_instant(value: Any) -> datetime | None:
    """Parse a unix timestamp or ISO-8601 string into an aware UTC datetime."""
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        if number > 1e11:  # milliseconds
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return ensure_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def _unit(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None


@dataclass(frozen=True)
class Benchmark:
    """The market-implied probability observed at (or before) the decision."""

    price: float | None
    bid: float | None
    ask: float | None
    observed_at: datetime | None
    source: str | None
    age_seconds: float | None
    #: ``None`` when the benchmark is usable; otherwise why it is not matched.
    rejected_reason: str | None = None

    @property
    def is_matched(self) -> bool:
        return self.price is not None and self.rejected_reason is None


def build_benchmark(
    *,
    price: Any,
    bid: Any,
    ask: Any,
    observed_at: Any,
    source: str | None,
    prediction_instant: datetime,
) -> Benchmark:
    """Validate a quote against the prediction instant.

    A quote observed *after* the prediction is evidence learned after the
    decision and is rejected, not silently accepted. A quote without an
    observation time cannot be shown to be contemporaneous, so it is kept as
    the caller's price but flagged unverified (no freshness is invented).
    """
    px = _unit(price)
    b, a = _unit(bid), _unit(ask)
    if b is not None and a is not None and b > a:
        return Benchmark(None, None, None, None, source, None, "crossed_quote")
    if px is None and b is not None and a is not None:
        px = (b + a) / 2.0
    seen = parse_instant(observed_at)
    if px is None:
        return Benchmark(None, b, a, seen, source, None, "no_price")
    if seen is None:
        return Benchmark(px, b, a, None, source, None, "observation_time_missing")
    age = (prediction_instant - seen).total_seconds()
    if age < 0:
        return Benchmark(None, None, None, seen, source, None, "quote_after_prediction")
    if age > MAX_FRESH_QUOTE_AGE_SECONDS:
        return Benchmark(px, b, a, seen, source, age, "quote_stale")
    return Benchmark(px, b, a, seen, source, age, None)


def _canonical(value: Any) -> Any:
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat()
    return value


def request_key(
    *,
    request_id: str | None,
    condition_id: str,
    token_id: str | None,
    p_interpretation: str,
    probability: float,
    market_price: float | None,
    model: str | None,
    task: str | None,
    prompt_version: str | None,
    rule_hash: str | None,
    evidence_snapshot_id: str | None,
    calibrator_version: str | None,
    prediction_instant: datetime | None,
    recorded_at: datetime,
    window_offset: int = 0,
) -> str:
    """Replay-safe identity (64 hex chars).

    * A caller idempotency key is the identity: it scopes the market so the same
      id on another market is a different request.
    * An explicit ``prediction_instant`` is part of the identity: the caller
      says when the decision was made, so the same decision replays exactly.
    * Otherwise the full-precision inputs are hashed together with the replay
      window index, so identical inputs repeated after the window are a new
      forecast.
    """
    payload: dict[str, Any] = {
        "c": condition_id.strip().lower(),
        "t": token_id,
        "i": p_interpretation,
    }
    if request_id:
        payload["id"] = request_id.strip()
    else:
        payload.update(
            {
                "p": _canonical(float(probability)),
                "px": None if market_price is None else _canonical(float(market_price)),
                "m": model,
                "k": task,
                "pv": prompt_version,
                "rh": rule_hash,
                "ev": evidence_snapshot_id,
                "cv": calibrator_version,
            }
        )
        if prediction_instant is not None:
            payload["at"] = _canonical(prediction_instant)
        else:
            window = int(ensure_utc(recorded_at).timestamp() // REPLAY_WINDOW_SECONDS)
            payload["w"] = window + window_offset
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def market_provenance(market: dict[str, Any] | None) -> dict[str, Any]:
    """Extract decision-time identity fields from a Gamma market dict.

    Only values the market object actually carries are returned; nothing is
    invented. The YES token is the one whose outcome label is "yes".
    """
    if not isinstance(market, dict):
        return {}
    out: dict[str, Any] = {}
    tokens = _json_list(market.get("clobTokenIds") or market.get("clob_token_ids"))
    outcomes = _json_list(market.get("outcomes"))
    if tokens and len(tokens) == len(outcomes):
        mapping = {str(t): str(o) for t, o in zip(tokens, outcomes, strict=True)}
        out["outcome_mapping"] = mapping
        for token, label in mapping.items():
            if label.strip().lower() == "yes":
                out["token_id"] = token
                break
    out["market_close_at"] = parse_instant(
        market.get("endDate") or market.get("end_date") or market.get("endDateIso")
    )
    events = market.get("events")
    group = None
    if isinstance(events, list) and events and isinstance(events[0], dict):
        group = events[0].get("id") or events[0].get("slug")
    group = group or market.get("eventId") or market.get("event_id") or market.get("eventSlug")
    if group not in (None, ""):
        out["event_group"] = str(group)
    out["benchmark_bid"] = market.get("bestBid")
    out["benchmark_ask"] = market.get("bestAsk")
    out["benchmark_observed_at"] = market.get("updatedAt") or market.get("updated_at")
    if out["benchmark_bid"] is not None or out["benchmark_observed_at"] is not None:
        out["benchmark_source"] = "gamma"
    return {k: v for k, v in out.items() if v not in (None, "")}


def _json_list(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []
