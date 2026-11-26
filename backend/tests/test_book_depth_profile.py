"""Depth profile arithmetic plus WS reconnect / crossed-frame abstention end to end."""

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from book_research_fixtures import CID, NOW
from book_research_fixtures import research_db as _research_db
from sqlalchemy.orm import sessionmaker
from test_binary_book import NOW as BOOK_NOW
from test_binary_book import delta, snapshot, spec

from app.services.market_data import book_resnapshot, clob_ws_manager
from app.services.market_data.binary_book import Level, depth_profile, reconstruct
from app.services.market_data.book_research import ResearchQuery, load_analysis

research_db = _research_db


def verified():
    return reconstruct(spec(), "11", [snapshot()], BOOK_NOW)


def test_depth_profile_exact_values_and_uncovered_sizes_are_withheld():
    profile = depth_profile(spec(), verified(), [Decimal(v) for v in ("5", "10", "20", "40")])
    assert profile.status == "modeled"
    assert profile.measured_price_impact is None and profile.fill_probability is None
    by_size = {p.shares: p for p in profile.points}
    five, ten, twenty, forty = (by_size[Decimal(v)] for v in ("5", "10", "20", "40"))
    # Touch: bid 0.40 x10, ask 0.50 x10 then 0.60 x20.
    assert (five.ask_marginal_price, five.bid_marginal_price) == (Decimal("0.5"), Decimal("0.4"))
    assert five.spread_at_depth == Decimal("0.1")
    assert five.buy_walk_slippage_vs_touch == 0 and five.sell_walk_slippage_vs_touch == 0
    assert ten.spread_at_depth == Decimal("0.1")
    # 20 shares: asks covered (VWAP 0.55), bids hold only 10 so the bid side is uncovered.
    assert twenty.ask_covered and not twenty.bid_covered
    assert twenty.ask_vwap == Decimal("0.55")
    assert twenty.buy_walk_slippage_vs_touch == Decimal("0.05")
    assert twenty.spread_at_depth is None and twenty.bid_vwap is None
    # 40 shares exceed all displayed asks (30): nothing is invented.
    assert not forty.ask_covered and forty.ask_vwap is None


def test_depth_profile_cost_is_monotonic_in_size():
    book = reconstruct(
        spec(),
        "11",
        [
            snapshot(
                asks=[Level(price=f"0.{50 + i}", shares="3") for i in range(9)],
                bids=[Level(price=f"0.{40 - i}", shares="3") for i in range(9)],
            )
        ],
        BOOK_NOW,
    )
    sizes = [Decimal(v) for v in range(1, 28)]
    points = depth_profile(spec(), book, sizes).points
    assert all(p.ask_covered and p.bid_covered for p in points)
    for first, second in zip(points, points[1:], strict=False):
        assert second.buy_walk_slippage_vs_touch >= first.buy_walk_slippage_vs_touch
        assert second.sell_walk_slippage_vs_touch >= first.sell_walk_slippage_vs_touch
        assert second.spread_at_depth >= first.spread_at_depth
        assert second.ask_vwap >= first.ask_vwap and second.bid_vwap <= first.bid_vwap
    assert points[0].spread_at_depth == points[0].ask_marginal_price - points[0].bid_marginal_price


@pytest.mark.parametrize(
    "book_events,reason",
    [
        ([snapshot(bids=[Level(price="0.7", shares="1")])], "BOOK_NOT_USABLE"),
        ([snapshot(bids=[])], "BOOK_NOT_USABLE"),
        ([snapshot(), delta(sequence=3)], "BOOK_NOT_USABLE"),
    ],
)
def test_depth_profile_abstains_on_unusable_books(book_events, reason):
    book = reconstruct(spec(), "11", book_events, BOOK_NOW + timedelta(seconds=2))
    profile = depth_profile(spec(), book, [Decimal(5)])
    assert (profile.status, profile.reason, profile.points) == ("abstain", reason, [])


def test_depth_profile_rejects_identity_mismatch_bad_and_small_sizes():
    book = verified()
    other = spec().model_copy(update={"rule_hash": "different"})
    assert depth_profile(other, book, [Decimal(5)]).reason == "BOOK_IDENTITY_MISMATCH"
    assert depth_profile(spec(), book, []).reason == "POSITIVE_SIZES_REQUIRED"
    assert depth_profile(spec(), book, [Decimal("0.5")]).reason == "BELOW_MINIMUM_ORDER_SIZE"


def query(**changes):
    return ResearchQuery(
        **{
            "condition_id": CID,
            "as_of": NOW,
            "sizes": ["5", "10", "20"],
            "fee_bps": "100",
            "fee_version": "test-flat",
            **changes,
        }
    )


def test_depth_profile_is_opt_in_so_legacy_replay_hashes_do_not_change(research_db):
    default, _ = load_analysis(research_db, query())
    assert "depth_profile" not in default
    enabled, _ = load_analysis(research_db, query(include_depth_profile=True))
    assert {k: v for k, v in enabled.items() if k != "depth_profile"} == default
    yes = enabled["depth_profile"]["YES"]
    assert yes["status"] == "modeled" and yes["measured_price_impact"] is None
    assert [p["shares"] for p in yes["points"]] == ["5", "10", "20"]
    assert yes["book_hash"] == enabled["books"]["YES"]["book_hash"]


class FrozenDatetime(datetime):
    frozen = NOW + timedelta(seconds=1)

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003
        return cls.frozen


def test_ws_disconnect_and_crossed_frame_withhold_costs_until_resnapshot(research_db, monkeypatch):
    from app.utils import database

    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(bind=research_db.get_bind(), expire_on_commit=False)
    )
    monkeypatch.setattr(clob_ws_manager, "datetime", FrozenDatetime)
    requested = []

    async def refresh(condition):
        requested.append(condition)

    monkeypatch.setattr(book_resnapshot, "_resnapshot", refresh)

    async def run():
        manager = clob_ws_manager.ClobWsManager()
        await manager.subscribe(["11", "22"])
        # 1. Connection drops: archived status marks continuity unknown for both tokens.
        await manager._archive_status("SOURCE_DISCONNECTED")
        gap, _ = load_analysis(
            research_db, query(as_of=NOW + timedelta(seconds=2), include_depth_profile=True)
        )
        for label in ("YES", "NO"):
            assert gap["books"][label]["grade"] == "PARTIAL"
            assert "DISCONNECTED" in gap["books"][label]["reasons"]
            assert gap["books"][label]["asks"] == []
        assert gap["status"] == "abstain" and gap["estimates"][0]["vwap"] is None
        assert gap["depth_profile"]["YES"]["status"] == "abstain"
        assert gap["depth_profile"]["YES"]["points"] == []
        # 2. Synthetic crossed frame arrives over the socket: no cost claim, resnapshot asked.
        FrozenDatetime.frozen = NOW + timedelta(seconds=3)
        frame = {
            "event_type": "book",
            "market": CID,
            "asset_id": "11",
            "timestamp": str(int((NOW + timedelta(seconds=3)).timestamp() * 1000)),
            "bids": [{"price": "0.70", "size": "5"}],
            "asks": [{"price": "0.50", "size": "5"}],
        }
        await manager._archive_message(json.dumps(frame))
        crossed, _ = load_analysis(research_db, query(as_of=NOW + timedelta(seconds=4)))
        assert "CROSSED_BOOK" in crossed["books"]["YES"]["reasons"]
        assert crossed["books"]["YES"]["bids"] == []
        assert crossed["estimates"][0]["status"] == "abstain"
        await asyncio.sleep(0)
        assert requested == [CID]
        await manager.stop()

    try:
        asyncio.run(run())
    finally:
        FrozenDatetime.frozen = NOW + timedelta(seconds=1)
