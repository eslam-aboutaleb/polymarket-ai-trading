"""Trader skill, independent consensus and out-of-sample challenger (issue #6).

Research and shadow only: nothing here places or signs an order.

* ``trader_profile`` shrinks a category's observed net return toward a baseline,
  so a tiny successful sample stays near the baseline and is ``watch_only``.
* ``independent_votes`` counts one vote per wallet cluster, so correlated wallets
  are not independent confirmations.
* ``challenger_report`` compares a selective policy against copy-all on trades
  after a split time, using only information from before it.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

MODEL_VERSION = "copy-intelligence-v1"
MIN_MARKETS = 20
PRIOR_MARKETS = 30
MIN_TEST_TRADES = 30
MIN_TEST_WALLETS = 3


def trader_profile(
    wallet: str,
    category: str,
    positions: Iterable[Mapping[str, Any]],
    *,
    baseline_return: float = 0.0,
    prior_markets: int = PRIOR_MARKETS,
    min_markets: int = MIN_MARKETS,
) -> dict[str, Any]:
    """Category-specific profile from resolved positions (``market_id``, ``cost``, ``pnl``)."""
    rows = list(positions)
    by_market: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for row in rows:
        by_market[str(row["market_id"])][0] += float(row["cost"])
        by_market[str(row["market_id"])][1] += float(row["pnl"])
    n = len(by_market)
    cost = sum(c for c, _ in by_market.values())
    pnl = sum(p for _, p in by_market.values())
    raw = pnl / cost if cost > 0 else None
    weight = n / (n + prior_markets) if n else 0.0
    skill = baseline_return if raw is None else baseline_return + (raw - baseline_return) * weight
    returns = [p / c for c, p in by_market.values() if c > 0]
    mean = sum(returns) / len(returns) if returns else 0.0
    variance = (
        sum((r - mean) ** 2 for r in returns) / (len(returns) - 1) if len(returns) > 1 else 1.0
    )
    positive = sorted((p for _, p in by_market.values() if p > 0), reverse=True)
    status = "scored" if n >= min_markets and cost > 0 else "watch_only"
    return {
        "model_version": MODEL_VERSION,
        "wallet": wallet.lower(),
        "category": category,
        "observed_trades": len(rows),
        "independent_market_count": n,
        "realized_pnl": round(pnl, 8),
        "profit_concentration": round(positive[0] / sum(positive), 4) if positive else None,
        "raw_net_return": None if raw is None else round(raw, 6),
        "baseline_return": baseline_return,
        "skill": round(skill, 6),
        "shrinkage_weight": round(weight, 4),
        "uncertainty": round(math.sqrt(variance / n) * weight, 6) if n else None,
        "status": status,
    }


def independent_votes(
    signals: Iterable[Mapping[str, Any]], clusters: Iterable[Mapping[str, Any]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """Votes per (market, side). Wallets in one cluster count once.

    ``clusters`` is the output of ``wallet_clustering.clusters_from_events``.
    """
    owner = {}
    for cluster in clusters:
        for wallet in cluster["wallets"]:
            owner[wallet.lower()] = cluster["cluster_id"]
    seen: dict[tuple[str, str], dict[str, set]] = defaultdict(
        lambda: {"wallets": set(), "units": set()}
    )
    for signal in signals:
        wallet = str(signal["wallet"]).lower()
        key = (str(signal["market_id"]), str(signal["side"]))
        seen[key]["wallets"].add(wallet)
        seen[key]["units"].add(owner.get(wallet, "wallet:" + wallet))
    return {
        key: {"raw_votes": len(v["wallets"]), "independent_votes": len(v["units"])}
        for key, v in sorted(seen.items())
    }


def _pooled(rows: list[Mapping[str, Any]]) -> float | None:
    cost = sum((Decimal(r["cost"]) for r in rows), Decimal(0))
    if cost <= 0:
        return None
    return float(sum((Decimal(r["pnl"]) for r in rows), Decimal(0)) / cost)


def _bootstrap(
    selected: dict[str, list], rest: dict[str, list], seed: int, resamples: int
) -> list[float] | None:
    rng = random.Random(seed)  # noqa: S311 - deterministic statistical resampling
    a_keys, b_keys = sorted(selected), sorted(rest)
    diffs = []
    for _ in range(resamples):
        a = _pooled([r for k in (rng.choice(a_keys) for _ in a_keys) for r in selected[k]])
        b = _pooled([r for k in (rng.choice(b_keys) for _ in b_keys) for r in rest[k]])
        if a is not None and b is not None:
            diffs.append(a - b)
    if len(diffs) < resamples // 2:
        return None
    diffs.sort()
    return [round(diffs[int(0.025 * len(diffs))], 6), round(diffs[int(0.975 * len(diffs)) - 1], 6)]


def challenger_report(
    rows: list[Mapping[str, Any]],
    *,
    split_at: datetime,
    min_votes: int = 1,
    prior_markets: int = PRIOR_MARKETS,
    min_markets: int = MIN_MARKETS,
    seed: int = 11,
    resamples: int = 1000,
) -> dict[str, Any]:
    """Selective policy vs copy-all, judged only on trades first seen at or after ``split_at``.

    Each row: ``wallet``, ``market_id``, ``first_seen_at``, ``cost``, ``pnl`` (the follower's
    own settled PnL), ``independent_votes``. The policy copies a trade when its wallet's
    shrunk net return on training rows (strictly before ``split_at``) is positive,
    the wallet has at least ``min_markets`` training markets, and votes reach ``min_votes``.
    """
    train: dict[str, list] = defaultdict(list)
    test = []
    for row in rows:
        (train[str(row["wallet"]).lower()] if row["first_seen_at"] < split_at else test).append(row)
    approved = {}
    for wallet, items in train.items():
        profile = trader_profile(
            wallet, "all", items, prior_markets=prior_markets, min_markets=min_markets
        )
        approved[wallet] = profile["status"] == "scored" and profile["skill"] > 0
    selected: dict[str, list] = defaultdict(list)
    rest: dict[str, list] = defaultdict(list)
    chosen = []
    for row in test:
        wallet = str(row["wallet"]).lower()
        if approved.get(wallet) and row.get("independent_votes", 1) >= min_votes:
            selected[wallet].append(row)
            chosen.append(row)
        else:
            rest[wallet].append(row)
    report: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "split_at": split_at.isoformat(),
        "test_trades": len(test),
        "copy_all": {"trades": len(test), "pooled_net_return": _pooled(test)},
        "selective": {
            "trades": len(chosen),
            "wallets": len(selected),
            "pooled_net_return": _pooled(chosen),
        },
        "not_selected": {
            "trades": len(test) - len(chosen),
            "pooled_net_return": _pooled([r for items in rest.values() for r in items]),
        },
        "difference_ci95": None,
    }
    enough = (
        len(chosen) >= MIN_TEST_TRADES
        and len(test) - len(chosen) >= MIN_TEST_TRADES
        and len(selected) >= MIN_TEST_WALLETS
        and len(rest) >= MIN_TEST_WALLETS
    )
    if not enough:
        report["verdict"] = "insufficient_evidence"
        report["statement"] = (
            "Too few out-of-sample trades or wallets to say whether selective copying beats "
            "copy-all. No improvement is claimed."
        )
        return report
    interval = _bootstrap(selected, rest, seed, resamples)
    report["difference_ci95"] = interval
    if interval is None or interval[0] <= 0 <= interval[1]:
        report["verdict"] = "no_improvement_detected"
        report["statement"] = (
            "Selective copying did NOT beat the unselected trades beyond noise after costs."
        )
    elif interval[0] > 0:
        report["verdict"] = "selective_beats_rest"
        report["statement"] = (
            "Out of sample, selected trades beat unselected trades after costs; "
            "95% interval above zero."
        )
    else:
        report["verdict"] = "selective_worse"
        report["statement"] = "Out of sample, selected trades did worse than unselected ones."
    return report
