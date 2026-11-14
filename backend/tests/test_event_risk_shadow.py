"""The diagnostic source comparison is reproducible and does not claim actual fills."""

import importlib.util
import json
from decimal import localcontext
from pathlib import Path

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT

ROOT = Path(__file__).parents[2]


def test_forward_quote_shadow_risk_comparison_reproduces():
    spec = importlib.util.spec_from_file_location(
        "event_shadow", ROOT / "scripts/event-risk-shadow.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with localcontext(ACCOUNTING_CONTEXT):
        result = module.report()
    expected = json.loads(
        (ROOT / "user guide/skills/event-correlation-risk/shadow-baseline.json").read_text()
    )
    assert result == expected
    basket = result["diagnostics"][0]
    assert basket["single_condition_concentration_flags"] == []
    assert basket["group_concentration_flags"]
    assert basket["observed_counterfactual_loss_within_bound"]
    assert not basket["actual_fills"]
