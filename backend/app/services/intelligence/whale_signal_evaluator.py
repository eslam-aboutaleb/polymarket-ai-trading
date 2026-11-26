"""Causal, time-aware evaluation of whale-flow and inverse signals (issue #11).

Research only. Nothing here places an order or suggests a trade without evidence.

Causality rules:

* A whale event is available only from ``observed_at`` (receipt time), never from
  the on-chain ``event_time``. A late backfill therefore enters late.
* A follower enters at the first quote at or after ``observed_at + delay`` (within
  ``max_wait``). No price before that moment is ever read.
* Wallets in one co-occurrence cluster and repeated events on one (market, side)
  count once, at the earliest observation.
* In-sample and out-of-sample sets are disjoint by market: a market belongs to the
  period of its first observed signal. Conclusions use the out-of-sample set only.
* Outcomes use the settlement payout when supplied, otherwise a markout at the
  horizon from a later quote (labelled), never a post-hoc rank.
* A trade suggestion needs enough distinct markets, a 95% interval above zero after
  costs, and a margin over a time-shifted placebo. Anything else abstains.
"""

from __future__ import annotations

import random
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

MODEL_VERSION = "whale-signal-eval-v1"
DELAYS_SECONDS = (60, 300, 1800)
MIN_DISTINCT_MARKETS = 30
Prob = Annotated[Decimal, Field(ge=0, le=1, max_digits=9, decimal_places=8)]


class WhaleSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    event_id: str = Field(min_length=1, max_length=120)
    wallet: str = Field(min_length=1, max_length=80)
    market_id: str = Field(min_length=1, max_length=200)
    token_id: str = Field(min_length=1, max_length=200)
    complement_token_id: str | None = Field(default=None, max_length=200)
    side: Literal["BUY", "SELL"]
    event_time: AwareDatetime | None = None
    observed_at: AwareDatetime


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    token_id: str
    observed_at: AwareDatetime
    bid: Prob | None = None
    ask: Prob | None = None


class Settlement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    token_id: str
    observed_at: AwareDatetime
    payout: Prob


def dedupe_signals(
    signals: list[WhaleSignal], clusters: list[Mapping[str, Any]]
) -> tuple[list[WhaleSignal], dict[str, int]]:
    """One signal per (cluster or wallet, market, side), earliest observation wins."""
    owner = {w.lower(): c["cluster_id"] for c in clusters for w in c["wallets"]}
    sides: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for s in signals:
        sides[
            (owner.get(s.wallet.lower(), s.wallet.lower()), s.market_id, s.observed_at.isoformat())
        ].add(s.side)
    kept: dict[tuple[str, str, str], WhaleSignal] = {}
    dropped = {"duplicate": 0, "self_trade": 0, "sell_not_followable": 0}
    for s in sorted(signals, key=lambda x: (x.observed_at, x.event_id)):
        unit = owner.get(s.wallet.lower(), s.wallet.lower())
        if len(sides[(unit, s.market_id, s.observed_at.isoformat())]) > 1:
            dropped["self_trade"] += 1
            continue
        if s.side != "BUY":
            dropped["sell_not_followable"] += 1
            continue
        key = (unit, s.market_id, s.side)
        if key in kept:
            dropped["duplicate"] += 1
            continue
        kept[key] = s
    return list(kept.values()), dropped


class QuoteIndex:
    """Quotes per token, sorted by receipt time, for bisect lookups."""

    def __init__(self, quotes: list[Quote]):
        grouped: dict[str, list[Quote]] = defaultdict(list)
        for q in sorted(quotes, key=lambda q: q.observed_at):
            grouped[q.token_id].append(q)
        self.by_token = grouped
        self.times = {t: [q.observed_at for q in rows] for t, rows in grouped.items()}

    def first(self, token: str, start: datetime, end: datetime, field: str) -> Quote | None:
        rows = self.by_token.get(token, [])
        at = bisect_left(self.times.get(token, []), start)
        while at < len(rows) and rows[at].observed_at <= end:
            if getattr(rows[at], field) is not None:
                return rows[at]
            at += 1
        return None


def signal_outcome(
    signal: WhaleSignal,
    quotes: list[Quote] | QuoteIndex,
    settlements: list[Settlement],
    *,
    delay_seconds: int,
    inverse: bool = False,
    fee_bps: Decimal = Decimal(0),
    max_wait_seconds: int = 120,
    markout_seconds: int = 3600,
    shift: timedelta = timedelta(0),
) -> dict[str, Any]:
    """Net return per dollar for one delayed (or inverse) follow. Unknown stays unknown."""
    index = quotes if isinstance(quotes, QuoteIndex) else QuoteIndex(quotes)
    token = signal.complement_token_id if inverse else signal.token_id
    if not token:
        return {"status": "missing_complement_token"}
    start = signal.observed_at + shift + timedelta(seconds=delay_seconds)
    entry = index.first(token, start, start + timedelta(seconds=max_wait_seconds), "ask")
    if entry is None:
        return {"status": "no_entry_quote"}
    settle = next(
        (s for s in settlements if s.token_id == token and s.observed_at >= entry.observed_at), None
    )
    cost = entry.ask * (1 + fee_bps / 10000)
    if settle is not None:
        value, basis = settle.payout, "settled"
    else:
        exit_quote = index.first(
            token,
            entry.observed_at + timedelta(seconds=markout_seconds),
            entry.observed_at + timedelta(seconds=markout_seconds + max_wait_seconds),
            "bid",
        )
        if exit_quote is None:
            return {"status": "no_exit_evidence"}
        value, basis = exit_quote.bid, "markout"
    return {"status": "ok", "basis": basis, "cost": cost, "net_return": (value - cost) / cost}


def _mean(values: list[Decimal]) -> float | None:
    return float(sum(values, Decimal(0)) / len(values)) if values else None


def _ci(by_market: dict[str, list[Decimal]], seed: int, resamples: int) -> list[float] | None:
    markets = sorted(by_market)
    if len(markets) < 2:
        return None
    rng = random.Random(seed)  # noqa: S311 - deterministic statistical resampling
    means = []
    for _ in range(resamples):
        picked = [v for m in (rng.choice(markets) for _ in markets) for v in by_market[m]]
        means.append(float(sum(picked, Decimal(0)) / len(picked)))
    means.sort()
    return [round(means[int(0.025 * len(means))], 6), round(means[int(0.975 * len(means)) - 1], 6)]


def _summarize(rows: list[tuple[WhaleSignal, dict]], seed: int, resamples: int) -> dict[str, Any]:
    ok = [(s, o) for s, o in rows if o["status"] == "ok"]
    by_market: dict[str, list[Decimal]] = defaultdict(list)
    for s, o in ok:
        by_market[s.market_id].append(o["net_return"])
    reasons: dict[str, int] = defaultdict(int)
    for _, o in rows:
        if o["status"] != "ok":
            reasons[o["status"]] += 1
    return {
        "signals": len(rows),
        "evaluated": len(ok),
        "distinct_markets": len(by_market),
        "missing": dict(sorted(reasons.items())),
        "bases": sorted({o["basis"] for _, o in ok}),
        "mean_net_return": _mean([o["net_return"] for _, o in ok]),
        "ci95": _ci(by_market, seed, resamples),
    }


def evaluate(
    signals: list[WhaleSignal],
    quotes: list[Quote],
    settlements: list[Settlement],
    clusters: list[Mapping[str, Any]],
    *,
    split_at: datetime,
    inverse: bool = False,
    fee_bps: Decimal = Decimal(0),
    seed: int = 5,
    resamples: int = 500,
    placebo_runs: int = 20,
    min_markets: int = MIN_DISTINCT_MARKETS,
) -> dict[str, Any]:
    usable = [s for s in signals if s.event_time is None or s.event_time <= s.observed_at]
    index = QuoteIndex(quotes)
    unique, dropped = dedupe_signals(usable, clusters)
    first_seen: dict[str, datetime] = {}
    for s in unique:
        first_seen[s.market_id] = min(first_seen.get(s.market_id, s.observed_at), s.observed_at)
    periods = {"in_sample": [], "out_of_sample": []}
    for s in unique:
        key = "in_sample" if first_seen[s.market_id] < split_at else "out_of_sample"
        periods[key].append(s)
    report: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "inverse": inverse,
        "split_at": split_at.isoformat(),
        "excluded": {**dropped, "event_time_after_receipt": len(signals) - len(usable)},
        "delays": [],
        "trade_suggestion": None,
        "informational_only": True,
    }
    rng = random.Random(seed)  # noqa: S311 - deterministic placebo shifts
    for delay in DELAYS_SECONDS:
        entry: dict[str, Any] = {"delay_seconds": delay}
        for name, items in periods.items():
            rows = [
                (
                    s,
                    signal_outcome(
                        s,
                        index,
                        settlements,
                        delay_seconds=delay,
                        inverse=inverse,
                        fee_bps=fee_bps,
                    ),
                )
                for s in items
            ]
            entry[name] = _summarize(rows, seed, resamples)
        placebo = []
        for _ in range(placebo_runs):
            rows = [
                (
                    s,
                    signal_outcome(
                        s,
                        index,
                        settlements,
                        delay_seconds=delay,
                        inverse=inverse,
                        fee_bps=fee_bps,
                        shift=timedelta(minutes=rng.choice([-360, -240, -120, 120, 240, 360])),
                    ),
                )
                for s in periods["out_of_sample"]
            ]
            mean = _mean([o["net_return"] for _, o in rows if o["status"] == "ok"])
            if mean is not None:
                placebo.append(mean)
        entry["placebo_mean_net_return"] = (
            round(sum(placebo) / len(placebo), 6) if placebo else None
        )
        report["delays"].append(entry)
    best = max(
        (d for d in report["delays"] if d["out_of_sample"]["ci95"]),
        key=lambda d: d["out_of_sample"]["ci95"][0],
        default=None,
    )
    reasons = []
    if best is None:
        reasons.append("missing_evidence")
    else:
        oos = best["out_of_sample"]
        if oos["distinct_markets"] < min_markets:
            reasons.append("too_few_distinct_markets")
        if oos["ci95"][0] <= 0:
            reasons.append("interval_includes_zero_after_costs")
        placebo_mean = best["placebo_mean_net_return"]
        if placebo_mean is not None and oos["mean_net_return"] <= placebo_mean:
            reasons.append("not_better_than_placebo")
        if oos["bases"] != ["settled"]:
            reasons.append("not_settlement_confirmed")
    report["abstain_reasons"] = reasons
    if best is not None and not reasons:
        report["trade_suggestion"] = {
            "delay_seconds": best["delay_seconds"],
            "note": "validated out of sample; still shadow/paper only",
        }
        report["informational_only"] = False
    return report
