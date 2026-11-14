"""Fill/no-fill and coherent payoff counterexamples, independent simulated bound checks."""

import itertools
import random
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.analytics.event_correlation_risk import (
    EventCorrelationRequest,
    ExposureLeg,
    LogicalRelation,
    assess_event_correlation_risk,
)

NOW = datetime(2026, 11, 13, tzinfo=UTC)
A, B, C = ("0x" + s * 64 for s in ["a", "b", "c"])


def leg(cid=A, outcome="YES", **changes):
    values = {
        "condition_id": cid,
        "token_id": str(int(cid, 16) * 2 + int(outcome == "NO")),
        "outcome": outcome,
        "shares": Decimal("100"),
        "entry_price": Decimal(".55"),
    }
    values.update(changes)
    return ExposureLeg(**values)


@pytest.mark.parametrize("filled,expected", [("0", "55"), ("40", "31"), ("100", "0")])
def test_unfilled_no_order_cannot_supply_a_guaranteed_hedge(filled, expected):
    result = assess_event_correlation_risk(
        EventCorrelationRequest(
            legs=[
                leg(),
                leg(outcome="NO", entry_price=".40", status="pending_order", filled_shares=filled),
            ],
            as_of=NOW,
        )
    )
    assert result.worst_case_portfolio_loss == Decimal(expected)


def test_event_tags_cannot_hide_shared_asset_or_oracle_concentration():
    legs = [
        leg(
            cid, entry_price=".5", event_id=f"event-{i}", underlying_asset="BTC", oracle="Chainlink"
        )
        for i, cid in enumerate([A, B, C])
    ]
    result = assess_event_correlation_risk(EventCorrelationRequest(legs=legs, as_of=NOW))
    assert result.worst_case_portfolio_loss == 150
    assert "CLUSTER_CONCENTRATION_EXCEEDED:asset:BTC" in result.concentration_warnings
    assert "CLUSTER_CONCENTRATION_EXCEEDED:oracle:chainlink" in result.concentration_warnings
    assert all(
        cluster.concentration_pct == 1
        for cluster in result.clusters
        if cluster.cluster_basis in {"oracle", "underlying_asset"}
    )


@pytest.mark.parametrize(
    "kind,outcomes,expected",
    [
        ("identical", ("YES", "NO"), "0"),
        ("complementary_hedge", ("YES", "YES"), "0"),
        ("implies", ("NO", "YES"), "0"),
        ("implies", ("YES", "NO"), "95"),
        ("mutually_exclusive", ("YES", "YES"), "95"),
    ],
)
def test_declared_logical_relations_have_directional_coherent_payoffs(kind, outcomes, expected):
    result = assess_event_correlation_risk(
        EventCorrelationRequest(
            legs=[leg(A, outcomes[0]), leg(B, outcomes[1], entry_price=".4")],
            relations=[
                LogicalRelation(relation_type=kind, left_condition_id=A, right_condition_id=B)
            ],
            as_of=NOW,
        )
    )
    assert result.worst_case_portfolio_loss == Decimal(expected)


def test_infeasible_relations_and_duplicate_identity_produce_unknown_not_zero():
    req = EventCorrelationRequest(
        legs=[leg(A), leg(B)],
        relations=[
            LogicalRelation(relation_type=k, left_condition_id=A, right_condition_id=B)
            for k in ["identical", "complementary_hedge"]
        ],
        as_of=NOW,
    )
    result = assess_event_correlation_risk(req)
    assert result.status == "abstain" and result.worst_case_portfolio_loss is None
    req = EventCorrelationRequest(
        legs=[leg(leg_id="same"), leg(outcome="NO", leg_id="same")], as_of=NOW
    )
    assert assess_event_correlation_risk(req).status == "abstain"
    req = EventCorrelationRequest(
        legs=[leg(token_id="1"), leg(outcome="NO", token_id="1")], as_of=NOW
    )
    assert assess_event_correlation_risk(req).status == "abstain"


def test_independently_simulated_fill_and_outcome_losses_never_exceed_bound():
    rng = random.Random(1729)  # noqa: S311 - deterministic simulation, no cryptographic use.
    for count in [3, 13]:
        legs = []
        for i in range(count):
            cid = "0x" + f"{i + 1:064x}"
            legs.append(
                leg(
                    cid,
                    shares=Decimal(rng.randint(1, 100)),
                    entry_price=Decimal(rng.randint(1, 99)) / 100,
                    status="pending_order" if i % 2 else "open_position",
                    fee_per_share=Decimal(".01"),
                )
            )
        req = EventCorrelationRequest(legs=legs, as_of=NOW)
        result = assess_event_correlation_risk(req)
        states = (
            itertools.product([False, True], repeat=count)
            if count == 3
            else ([rng.choice([False, True]) for _ in legs] for _ in range(200))
        )
        for state in states:
            pnl = Decimal(0)
            for position, won in zip(legs, state, strict=True):
                fill = (
                    Decimal(rng.choice([0, 1, 2])) / 2
                    if position.status == "pending_order"
                    else Decimal(1)
                )
                pnl += (
                    fill
                    * position.shares
                    * (Decimal(int(won)) - position.entry_price - position.fee_per_share)
                )
            assert -pnl <= result.worst_case_portfolio_loss
        assert assess_event_correlation_risk(req).model_dump() == result.model_dump()


def test_large_inconsistent_graph_abstains_before_using_conservative_bound():
    legs = [leg("0x" + f"{i + 1:064x}") for i in range(13)]
    relations = [
        LogicalRelation(
            relation_type=kind,
            left_condition_id=legs[0].condition_id,
            right_condition_id=legs[1].condition_id,
        )
        for kind in ("identical", "complementary_hedge")
    ]
    result = assess_event_correlation_risk(
        EventCorrelationRequest(legs=legs, relations=relations, as_of=NOW)
    )
    assert result.status == "abstain" and result.worst_case_portfolio_loss is None


def test_concentration_warning_uses_unrounded_ratio_at_the_cap():
    req = EventCorrelationRequest(
        legs=[
            leg(A, shares=Decimal("80.008"), entry_price=Decimal(".5")),
            leg(B, shares=Decimal("119.992"), entry_price=Decimal(".5")),
        ],
        as_of=NOW,
    )
    result = assess_event_correlation_risk(req)
    assert f"CLUSTER_CONCENTRATION_EXCEEDED:condition:{A}" in result.concentration_warnings
