import json
from decimal import Decimal

import pytest

from app.services.trading.btc_agent_contract import evaluate_agent_reply
from app.services.trading.btc_paper_decision import Quote, decide


def forecast(p, *, twap=".80", evidence="proxy_twap", verified=True, midpoint=".60"):
    return evaluate_agent_reply(
        json.dumps({"p_up": p, "evidence": evidence, "reason": "Approximate proxy only."}),
        midpoint=Decimal(midpoint),
        twap=Decimal(twap) if twap else None,
        reference_verified=verified,
    )


def test_extreme_model_output_is_bounded_and_shrunk_towards_market():
    result = forecast(".99")
    assert result.raw == Decimal(".99")
    assert result.bounded == Decimal(".68")
    assert result.actionable == Decimal(".64")


@pytest.mark.parametrize(
    "options", [{"twap": None}, {"evidence": "baseline"}, {"verified": False}, {"twap": ".59"}]
)
def test_uncorroborated_model_forecast_falls_back_to_market(options):
    result = forecast(".90", **options)
    assert result.actionable == Decimal(".60")
    assert result.evidence == "baseline"


def test_supported_signal_can_still_clear_the_execution_threshold():
    result = forecast(".72")
    quote = Quote(Decimal(".601"), Decimal(".001"), 99, Decimal(100), Decimal(5))
    order = decide(
        result.actionable,
        {"Up": quote},
        now=100,
        close_at=300,
        reference_verified=True,
        market_midpoint=Decimal(".60"),
        venue_taker_rate=Decimal(".05"),
    )
    assert order.action == "Up"


def test_malformed_probability_cannot_become_a_trade():
    with pytest.raises(ValueError):
        forecast(True)
    with pytest.raises(ValueError):
        forecast(".75", evidence={"tool": "unverified"})


def test_reference_claim_conflicts_are_audited_and_cannot_move_the_market_prior():
    reply = json.dumps(
        {"p_up": ".66", "evidence": "proxy_twap", "reason": "The settlement reference is missing."}
    )
    signal = evaluate_agent_reply(
        reply,
        midpoint=Decimal(".60"),
        twap=Decimal(".70"),
        reference_verified=True,
    )
    assert signal.actionable == Decimal(".60")
    assert signal.evidence == "baseline"
