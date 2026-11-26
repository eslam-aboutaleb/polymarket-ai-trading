"""Deterministic book/spec/rules builders for the arbitrage feasibility tests."""

from datetime import UTC, datetime
from decimal import Decimal

from app.services.analytics.arbitrage.depth_aware_pricer import PricedLeg
from app.services.analytics.arbitrage.payout_validator import ContractRules
from app.services.market_data.binary_book import BinaryBook, Level, MarketSpec

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
CID = "0x" + "cd" * 32


def spec(cid=CID, yes="11", no="22", rule_hash="r" * 8, **kw):
    return MarketSpec(
        condition_id=cid,
        yes_token=yes,
        no_token=no,
        rule_hash=rule_hash,
        metadata_id="meta-1",
        received_at=NOW,
        active=True,
        tick=Decimal("0.01"),
        minimum_shares=Decimal(1),
        lot=Decimal("0.01"),
        **kw,
    )


def book(sp, token, asks, bids=None, age="1", grade="VERIFIED_BOOK", as_of=NOW):
    return BinaryBook(
        condition_id=sp.condition_id,
        token_id=token,
        rule_hash=sp.rule_hash,
        as_of=as_of,
        grade=grade,
        reasons=[],
        source_ids=[f"snap-{token}"],
        book_hash=f"hash-{token}",
        age_seconds=Decimal(age),
        asks=[Level(price=Decimal(p), shares=Decimal(s)) for p, s in asks],
        bids=[Level(price=Decimal(p), shares=Decimal(s)) for p, s in (bids or [])],
        needs_resnapshot=False,
    )


def leg(label, sp, outcome, asks, bids=None, fee="0", version="flat-test", **bk):
    token = sp.yes_token if outcome == "YES" else sp.no_token
    return PricedLeg(
        label=label,
        spec=sp,
        book=book(sp, token, asks, bids, **bk),
        outcome=outcome,
        fee_bps=None if fee is None else Decimal(fee),
        fee_version=None if fee is None else version,
    )


def rules(sp=None, venue="polymarket", **kw):
    sp = sp or spec()
    base = {
        "venue": venue,
        "market_id": sp.condition_id,
        "yes_token": sp.yes_token,
        "no_token": sp.no_token,
        "rule_hash": sp.rule_hash,
        "neg_risk": False,
    }
    base.update(kw)
    return ContractRules(**base)


def pair(yes_asks, no_asks, *, yes_bids=None, no_bids=None, fee="0", **bk):
    sp = spec()
    return (
        sp,
        [
            leg("YES", sp, "YES", yes_asks, yes_bids, fee=fee, **bk),
            leg("NO", sp, "NO", no_asks, no_bids, fee=fee, **bk),
        ],
    )
