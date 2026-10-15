"""On-chain wallet co-occurrence clustering (plan 05 Phase B).

The whale monitor records one ``WhaleEvent`` per wallet-sized CTF
movement. A single event says one wallet did something; a *cluster* says
several wallets did the same thing in the same market on the same side,
which is the shape that separates a coordinated position from
coincidence and from one wallet's normal trading.

Clustering is a pure graph over events:

* wallets co-occur when they appear in the same **market and side**
  inside the window (``(market_id, side)`` is the co-occurrence key, so
  a buyer and a seller of the same market never cluster);
* ``min_co_occurrence`` is how many *distinct* markets (per side) two
  wallets must have traded together — one shared market is weak
  evidence, two is what separates a cluster from coincidence;
* connected components of the surviving edges are the clusters.

Everything here is deterministic: wallets are sorted inside a cluster,
clusters are sorted by size then by first wallet, and no event ordering,
timestamp conversion or database read happens inside the graph
functions. The DB-facing wrapper lives in ``ctf_events_service``
(``build_wallet_clusters`` / ``get_wallet_clusters``), so the graph stays
unit-testable on a synthetic event set with no fixtures at all.

Signal-only. A cluster is a label for a human to review — nothing here
follows, copies or blocks a wallet.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from app.utils.time import utc_now

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_HOURS = 24.0
DEFAULT_MIN_CO_OCCURRENCE = 2

# Cost guard (master 5.8): the graph is O(events) in memory and
# O(buckets²) in pairs, so the event read is capped by the caller.
MAX_EVENTS = 5000

_SEP = "\x1f"


def co_occurrence_key(event: Mapping[str, Any]) -> str:
    """Market + side key a wallet's event contributes to."""
    return f"{str(event.get('market_id') or '')}{_SEP}{str(event.get('side') or '')}"


def _wallet(event: Mapping[str, Any]) -> str:
    return str(event.get("wallet") or "").strip().lower()


def _notional(event: Mapping[str, Any]) -> float:
    value = event.get("notional")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _timestamp(event: Mapping[str, Any]) -> datetime | None:
    value = event.get("detected_at")
    return value if isinstance(value, datetime) else None


def build_co_occurrence_graph(
    events: Iterable[Mapping[str, Any]],
    *,
    min_co_occurrence: int = DEFAULT_MIN_CO_OCCURRENCE,
) -> dict[tuple[str, str], int]:
    """Wallet-pair edge weights from a co-occurrence graph.

    Returns ``{(wallet_a, wallet_b): shared_markets}`` for pairs that
    traded at least ``min_co_occurrence`` distinct markets on the same
    side, with the pair always ordered so the same two wallets always
    produce one key.
    """
    threshold = max(1, int(min_co_occurrence))
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for wallets in _grouped_buckets(events).values():
        ordered = sorted(wallets)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                counts[(left, right)] += 1
    return {pair: count for pair, count in counts.items() if count >= threshold}


def _connected_components(
    edges: Iterable[tuple[str, str]],
) -> list[set[str]]:
    """Union-find over the surviving edges: the clusters."""
    parent: dict[str, str] = {}
    groups: dict[str, set[str]] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        groups.setdefault(node, {node})
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    for left, right in edges:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            continue
        # Attach the smaller group to the larger one, then merge.
        if len(groups[left_root]) < len(groups[right_root]):
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        groups[left_root] |= groups[right_root]
        del groups[right_root]

    roots = {find(node) for node in parent}
    return [groups[root] for root in roots]


def clusters_from_events(
    events: Iterable[Mapping[str, Any]],
    *,
    min_co_occurrence: int = DEFAULT_MIN_CO_OCCURRENCE,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Pure co-occurrence clusters over whale events.

    ``window_hours`` and ``now`` bound the events considered (an event
    with no usable timestamp is kept, so the DB read — not this
    function — decides what is in range). The grouping is stable: the
    same event set always produces the same cluster list in the same
    order, which is what the tests assert.
    """
    materialized = [event for event in events if _wallet(event)]
    reference = now or utc_now()
    cutoff = None
    if window_hours and window_hours > 0:
        cutoff = reference - timedelta(hours=float(window_hours))
    if cutoff is not None:
        materialized = [
            event
            for event in materialized
            if _timestamp(event) is None or _timestamp(event) >= cutoff
        ]

    buckets = _grouped_buckets(materialized)
    edges = build_co_occurrence_graph(materialized, min_co_occurrence=min_co_occurrence)
    pair_evidence = _pair_evidence(buckets, edges)
    notional_by_wallet: dict[str, float] = defaultdict(float)
    for event in materialized:
        notional_by_wallet[_wallet(event)] += _notional(event)

    clusters: list[dict[str, Any]] = []
    for index, members in enumerate(
        sorted(
            _connected_components(edges),
            key=lambda group: (-len(group), sorted(group)),
        ),
        start=1,
    ):
        ordered = sorted(members)
        member_edges = [
            (left, right) for left, right in edges if left in members or right in members
        ]
        shared: dict[str, dict[str, Any]] = {}
        for pair in member_edges:
            for bucket in pair_evidence.get(pair, []):
                shared[bucket["key"]] = bucket
        clusters.append(
            {
                "cluster_id": f"c{index}",
                "size": len(ordered),
                "wallets": ordered,
                "co_occurrence": max((edges[pair] for pair in member_edges), default=0),
                "edges": [{"wallets": list(pair), "shared": edges[pair]} for pair in member_edges],
                # Every market/side these wallets traded together, so a
                # reviewer can check the cluster against the venue itself.
                "shared_markets": sorted(
                    shared.values(),
                    key=lambda bucket: (-len(bucket["wallets"]), bucket["key"]),
                ),
                "distinct_markets": len({bucket["market_id"] for bucket in shared.values()}),
                # Everything these wallets moved inside the window, not
                # only the shared markets: context for how large the
                # cluster is relative to what the platform normally sees.
                "total_notional": round(sum(notional_by_wallet[wallet] for wallet in ordered), 4),
            }
        )
    return clusters


def _pair_evidence(
    buckets: Mapping[str, set[str]],
    edges: Mapping[tuple[str, str], int],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """For every surviving edge, the buckets that witnessed it.

    Only buckets whose wallet pair is itself an edge are recorded, so
    the evidence for a cluster never includes a market where the pair
    met once and the edge was dropped.
    """
    evidence: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for key, wallets in buckets.items():
        ordered = sorted(wallets)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                if (left, right) in edges:
                    evidence[(left, right)].append(_bucket_summary(key, wallets))
    return evidence


def _grouped_buckets(
    events: Iterable[Mapping[str, Any]],
) -> dict[str, set[str]]:
    """(market, side) → wallets seen there in this event set."""
    buckets: dict[str, set[str]] = defaultdict(set)
    for event in events:
        wallet = _wallet(event)
        if wallet:
            buckets[co_occurrence_key(event)].add(wallet)
    return buckets


def _bucket_summary(key: str, wallets: set[str]) -> dict[str, Any]:
    market_id, _, side = key.partition(_SEP)
    return {
        "key": key,
        "market_id": market_id,
        "side": side,
        "wallets": sorted(wallets),
    }
