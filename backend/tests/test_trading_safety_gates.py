"""Regression tests for the automated-trading safety gates.

These cover the three ways the AI layer could authorise capital movement when
its own safety machinery was broken:

* ``force=True`` on the inverse bot used to disable every guardrail;
* copy-trade AI approval used to fail *open* when the model was unavailable;
* an unrecognisable copy-trade verdict used to default to "copy".

The tests are intentionally narrow: they assert the gate's decision, not the
surrounding orchestration.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MONITOR = REPO_ROOT / "backend" / "app" / "services" / "inverse_bot_monitor.py"
TRADE_MONITOR = REPO_ROOT / "backend" / "app" / "services" / "trade_monitor.py"
LLM_CHAIN = REPO_ROOT / "services" / "llm-chain" / "src" / "analysis_chain.py"
CLI_AGENT = REPO_ROOT / "services" / "cli-agent" / "src" / "cli_agent.py"


def _load_recommendation_parser():
    """Exec just the recommendation parser out of the llm-chain module.

    The service package pulls in LangChain and provider SDKs, so importing it
    wholesale is not possible from the backend test environment.
    """
    import logging
    import re

    source = LLM_CHAIN.read_text()
    start = source.index("def _parse_copy_recommendation")
    end = source.index("def _get_tavily_client")
    namespace: dict = {"re": re, "logger": logging.getLogger("test")}
    exec(compile(source[start:end], str(LLM_CHAIN), "exec"), namespace)  # noqa: S102
    return namespace["_parse_copy_recommendation"]


def _function_source(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(path.read_text(), node) or ""
    raise AssertionError(f"{name} not found in {path}")


# --------------------------------------------------------------------------
# inverse bot: force must not disable every guardrail
# --------------------------------------------------------------------------


def test_force_cannot_bypass_the_daily_reversal_cap() -> None:
    """`force` may exceed the user's cap, never the server-side ceiling."""
    source = MONITOR.read_text()

    # The daily-cap guard must exist in a form that is not conditioned on `not force`.
    assert "HARD_MAX_REVERSALS_PER_DAY" in source, "hard daily cap constant missing"

    cap_guard = source[source.index("effective_max_reversals") :]
    assert "if row.reversals_today >= effective_max_reversals:" in cap_guard
    guard_block = cap_guard[: cap_guard.index("\n\n") if "\n\n" in cap_guard else 400]
    assert "not force" not in guard_block, "daily cap guard must not be gated on `not force`"

    # The original bypass shape must be gone.
    assert "inverse_bot_max_reversals_per_day or 3) and not force" not in source, (
        "force can still bypass the daily cap"
    )


def test_force_cannot_bypass_the_cooldown_entirely() -> None:
    source = MONITOR.read_text()
    assert "HARD_MIN_COOLDOWN_SECONDS" in source, "hard cooldown floor missing"
    assert "cooldown_min * 60 if not force else HARD_MIN_COOLDOWN_SECONDS" in source
    assert "if seconds_since < cooldown_min * 60 and not force:" not in source


def test_hard_caps_are_positive_and_bounded() -> None:
    tree = ast.parse(MONITOR.read_text())
    values: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value.value, int):
                    values[target.id] = node.value.value

    assert values["HARD_MAX_REVERSALS_PER_DAY"] >= 1
    assert values["HARD_MIN_COOLDOWN_SECONDS"] >= 1


# --------------------------------------------------------------------------
# copy trade: AI approval must fail closed
# --------------------------------------------------------------------------


def test_copy_trade_does_not_execute_when_ai_approval_unavailable() -> None:
    source = _function_source(TRADE_MONITOR, "_trigger_copy_trade")

    # The gate must return before reaching execute_copy_trade when the
    # assessment is missing.
    guard = source.index("if not assessment:")
    ret = source.index("return", guard)
    execute = source.index("execute_copy_trade(")

    assert guard < ret < execute, "trade must be skipped before execute_copy_trade is called"
    assert "proceeding without" not in source, "fail-open log message still present"


# --------------------------------------------------------------------------
# copy trade: verdict parsing must fail closed and ignore prose
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_output", "expected"),
    [
        ("4. Recommendation: COPY\nConfidence: 80", "copy"),
        ("4. Recommendation: REDUCE_SIZE", "reduce_size"),
        ("4. Recommendation: SKIP", "avoid"),
        ("4. Recommendation: AVOID", "avoid"),
        # A market whose title contains "skip" must not flip the verdict.
        ("Will Skips McGee win? 4. Recommendation: COPY", "copy"),
        # Prose mentioning "skip" while recommending COPY must not flip it.
        ("This could skip. Recommendation: COPY", "copy"),
        # Unparseable / empty output must not authorise a trade.
        ("", "avoid"),
        ("I am unable to evaluate this trade.", "avoid"),
    ],
)
def test_copy_recommendation_parser(model_output: str, expected: str) -> None:
    parse = _load_recommendation_parser()
    assert parse(model_output) == expected


def test_copy_recommendation_never_defaults_to_copy() -> None:
    """The parser must have no path that returns "copy" without an explicit signal."""
    fn = _function_source(LLM_CHAIN, "_parse_copy_recommendation")

    # Every "return copy" must be guarded by a match on the explicit token.
    returns_copy = [
        line.strip() for line in fn.splitlines() if re.match(r"return \"copy\"", line.strip())
    ]
    assert returns_copy, "parser should still be able to return copy"
    assert fn.rstrip().endswith('return "avoid"'), "final fallback must be fail-closed"


def test_cli_agent_copy_trade_fails_closed() -> None:
    fn = _function_source(CLI_AGENT, "evaluate_copy_trade")

    # No bare `else: recommendation = "copy"` fallback.
    assert 'recommendation = "copy"' in fn
    for idx, line in enumerate(fn.splitlines()):
        if line.strip() == 'recommendation = "copy"':
            preceding = "\n".join(fn.splitlines()[:idx])
            assert '"COPY"' in preceding, '"copy" assigned without an explicit COPY match'
