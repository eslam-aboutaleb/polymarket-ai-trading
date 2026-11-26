"""Issue #159: sizing refuses categories without demonstrated skill; net-edge Kelly; drift."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from app.services.intelligence import forecast_scoring as fs
from app.services.intelligence import forecast_sizing_gate as gate
from app.services.trading import kelly_service

T0 = datetime(2026, 11, 1, tzinfo=UTC)
RNG = random.Random(159)  # noqa: S311


def rows(category, n, *, skilled):
    """Matched forecasts: skilled = LLM beats a noisy market, else LLM is worse."""
    out = []
    for i in range(n):
        y = float(RNG.random() < 0.5)
        market = min(max(0.5 + (0.1 if y else -0.1) * RNG.random(), 0.05), 0.95)
        llm = (
            min(max(0.5 + (0.4 if y else -0.4) * RNG.random(), 0.02), 0.98)
            if skilled
            else min(max(0.5 + (-0.3 if y else 0.3) * RNG.random(), 0.02), 0.98)
        )
        out.append(
            fs.PredictionScoreInput(
                forecast_id=f"{category}{i}",
                as_of=T0 + timedelta(hours=i),
                rule_hash="r",
                p=llm,
                baseline_p=market,
                final_label=y,
                event_cluster=f"{category}-e{i}",
                source_quality="matched",
                market_close_at=T0 + timedelta(days=30),
                resolved_at=T0 + timedelta(days=31),
                group=category,
            )
        )
    return out


@pytest.fixture(scope="module")
def reports():
    data = rows("politics", 120, skilled=True) + rows("sports", 120, skilled=False)
    data += rows("crypto", 6, skilled=True)  # too few independent events
    return gate.category_reports(data, resamples=300)


def test_per_category_report_has_sample_sizes(reports):
    assert set(reports) == {"politics", "sports", "crypto"}
    assert reports["politics"]["paired_events"] == 120
    assert reports["crypto"]["paired_events"] == 6
    assert reports["politics"]["status"] == fs.SkillStatus.BETTER_THAN_MARKET.value
    assert reports["sports"]["status"] == fs.SkillStatus.WORSE_THAN_MARKET.value
    assert reports["crypto"]["status"] == fs.SkillStatus.INSUFFICIENT_SAMPLE.value
    assert reports["politics"]["paired_brier_delta_ci95"][0] > 0


def test_sizing_refuses_categories_without_demonstrated_skill(reports):
    kwargs = {
        "bankroll": 1000.0,
        "p_llm": 0.8,
        "p_market": 0.5,
        "price": 0.5,
        "fee_per_share": 0.0,
        "weight": 1.0,
    }
    ok = gate.size_with_gate(gate.decide("politics", reports["politics"]), **kwargs)
    assert ok["mode"] == "kelly" and ok["size"] > 1.0
    for cat in ("sports", "crypto"):
        refused = gate.size_with_gate(gate.decide(cat, reports[cat]), **kwargs)
        assert refused["size"] == 0.0 and refused["mode"] == "none"
        assert any(r.startswith("SKILL_NOT_DEMONSTRATED") for r in refused["reasons"])
    missing = gate.size_with_gate(gate.decide("unknown", None), **kwargs)
    assert missing["size"] == 0.0 and "NO_SKILL_REPORT" in missing["reasons"]
    assert (
        gate.size_with_gate(gate.decide("uncategorised", reports["politics"]), **kwargs)["size"]
        == 0.0
    )
    assert ok["execution_eligible"] is False


def test_flat_tiny_is_opt_in_and_never_overrides_drift(reports):
    kwargs = {
        "bankroll": 1000.0,
        "p_llm": 0.8,
        "p_market": 0.5,
        "price": 0.5,
        "fee_per_share": 0.0,
        "weight": 1.0,
    }
    tiny = gate.size_with_gate(
        gate.decide("sports", reports["sports"], flat_tiny_allowed=True), **kwargs
    )
    assert tiny["mode"] == "flat_tiny" and tiny["size"] == gate.FLAT_TINY_USDC
    drifted = gate.decide("politics", reports["politics"], drifted=True, flat_tiny_allowed=True)
    assert drifted.mode == "none" and "MODEL_DRIFT_DISABLED" in drifted.reasons
    assert gate.size_with_gate(drifted, **kwargs)["size"] == 0.0


def test_drift_alert_disables_a_category_that_was_allowed(reports):
    before = gate.decide("politics", reports["politics"], drifted=False)
    after = gate.decide("politics", reports["politics"], drifted=True)
    assert before.allowed and before.mode == "kelly"
    assert not after.allowed and after.mode == "none"


def test_kelly_uses_net_edge_after_fees():
    d = gate.decide(
        "politics",
        {
            "status": fs.SkillStatus.BETTER_THAN_MARKET.value,
            "paired_events": 99,
            "paired_brier_delta_ci95": [0.01, 0.02],
        },
    )
    common = {"bankroll": 1000.0, "p_llm": 0.6, "p_market": 0.6, "price": 0.5, "weight": 1.0}
    gross = gate.size_with_gate(d, fee_per_share=0.0, **common)
    net = gate.size_with_gate(d, fee_per_share=0.05, **common)
    assert net["net_edge"] == pytest.approx(0.05) and gross["net_edge"] == pytest.approx(0.10)
    assert net["size"] < gross["size"]
    # Kelly saw price+fee as the cost: identical to calling kelly directly on net terms.
    assert net["size"] == kelly_service.size(1000.0, 0.6, 0.55)
    # A fee that wipes the edge sizes to zero, with an explicit reason.
    wiped = gate.size_with_gate(d, fee_per_share=0.10, **common)
    assert wiped["size"] == 0.0 and "NO_NET_EDGE" in wiped["reasons"]


def test_shrinkage_weight_is_fitted_clamped_and_never_invented():
    skilled = rows("a", 200, skilled=True)
    unskilled = rows("b", 200, skilled=False)
    w_good = gate.fit_shrinkage_weight(skilled)["weight"]
    w_bad = gate.fit_shrinkage_weight(unskilled)["weight"]
    assert 0.0 <= w_bad < w_good <= 1.0
    assert w_bad == 0.0  # an anti-skilled LLM gets no weight, not a negative one
    empty = gate.fit_shrinkage_weight([])
    assert empty == {"weight": 0.0, "fit_rows": 0, "reason": "NO_FIT_DATA"}
    assert gate.shrink(0.9, 0.5, 0.0) == 0.5 and gate.shrink(0.9, 0.5, 1.0) == 0.9
    assert gate.shrink(0.9, 0.5, 0.25) == pytest.approx(0.6)
    with pytest.raises(ValueError):
        gate.shrink(0.9, 0.5, 1.5)
    with pytest.raises(ValueError):
        gate.net_edge(0.6, 0.5, -0.01)


def test_shrunk_estimate_never_exceeds_llm_estimate_and_improves_overconfident_llm():
    over = []
    for i in range(300):
        y = float(RNG.random() < 0.55)
        market = 0.55
        llm = 0.95 if RNG.random() < 0.6 else 0.05  # overconfident noise
        over.append(
            fs.PredictionScoreInput(
                f"o{i}", T0, "r", llm, market, y, f"e{i}", "matched", resolved_at=T0, group="x"
            )
        )
    w = gate.fit_shrinkage_weight(over)["weight"]
    assert w < 0.2
    raw = sum((i.p - i.final_label) ** 2 for i in over) / len(over)
    shr = sum((gate.shrink(i.p, i.baseline_p, w) - i.final_label) ** 2 for i in over) / len(over)
    assert shr < raw


def test_docs_state_no_skill_is_assumed_until_measured():
    from pathlib import Path

    text = (Path(__file__).parents[2] / "docs" / "forecast-sizing-gate.md").read_text()
    assert "No skill is assumed" in text and "measured" in text


def test_gate_module_is_read_only_and_does_not_edit_live_paths():
    from pathlib import Path

    src = Path(gate.__file__).read_text()
    import ast

    modules = {
        n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert not {m for m in modules if "pre_trade_gate" in m or "live_trading_policy" in m}
    assert "httpx" not in src and "requests" not in src
