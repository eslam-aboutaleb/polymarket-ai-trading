"""Prompt/parser schema contract tests for the fixed P0 templates.

These tests pin the field names the LLM is asked to return to the
field names the parsers actually read. A template that asks for
``probability_yes`` while the parser reads ``p_yes`` silently turns
every edge calculation into zero, so the mismatch must fail here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_PATH = REPO_ROOT / "prompts" / "prompts.json"
STORE = json.loads(PROMPTS_PATH.read_text())


@pytest.mark.parametrize(
    ("category", "key", "required", "forbidden"),
    [
        (
            "auto_discovery",
            "superforecast",
            ['"p_yes"'],
            ['"probability_yes"'],
        ),
        (
            "auto_discovery",
            "best_trade",
            ['"side"', '"size"'],
            ['"size_usdc"', '"action"'],
        ),
        (
            "research",
            "sentiment_analysis",
            [
                '"sentiment_score"',
                '"label"',
                '"summary"',
                '"bullish_factors"',
                '"bearish_factors"',
            ],
            [],
        ),
        (
            "auto_discovery",
            "filter_markets",
            ['"id"', '"question"', '"edge_estimate"'],
            [],
        ),
    ],
)
def test_fixed_template_schema_matches_its_parser(
    category: str, key: str, required: list[str], forbidden: list[str]
) -> None:
    template = STORE[category][key]

    for field in required:
        assert field in template, f"{category}.{key} must ask for {field}"

    for field in forbidden:
        assert field not in template, f"{category}.{key} must not ask for {field}"


def test_sentiment_template_encodes_the_label_thresholds() -> None:
    template = STORE["research"]["sentiment_analysis"]

    assert "sentiment_score > 0.55" in template
    assert "sentiment_score < 0.45" in template
    assert '"bullish" | "bearish" | "neutral"' in template


def test_copy_trade_evaluation_ends_with_the_parseable_fields() -> None:
    template = STORE["copy_trading"]["trade_evaluation"]

    assert "Recommendation: COPY|REDUCE_SIZE|SKIP" in template
    assert "Confidence: <integer 0-100>" in template


def test_copy_trade_evaluation_uses_the_prose_system_prompt() -> None:
    system_prompt = STORE["copy_trading"]["system_prompt"]

    assert "copy-trade risk evaluator" in system_prompt.lower()
    assert "Recommendation: COPY" not in system_prompt
