"""Exercise the production renderer and parsers without model or search credentials."""

import asyncio
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage
from prompt_contracts import (
    IncompleteCompletionError,
    InputValidationError,
    LockMismatchError,
    OutputValidationError,
    PromptRegistry,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/llm-chain"))
sys.path.insert(0, str(ROOT / "services/shared"))

from src.analysis_chain import TradingAnalysisChain  # noqa: E402
from src.contract_prompts import (  # noqa: E402
    SENTIMENT_REF,
    SUPERFORECAST_REF,
    _load_registry,
    invoke_contract,
)

SENTIMENT = {
    "sentiment_score": 0.7,
    "label": "bullish",
    "summary": "Public opinion favors YES.",
    "bullish_factors": ["Published polling"],
    "bearish_factors": [],
}
FORECAST = {
    "question": "Will the measure pass?",
    "p_yes": 0.7,
    "probability_no": 0.3,
    "confidence": 0.8,
    "base_rate": 0.5,
    "key_factors": [{"factor": "Polling", "direction": "+", "magnitude": "medium"}],
    "decomposition": ["Evaluate polling evidence"],
    "reasoning": "Evidence favors passage.",
}


@pytest.fixture
def registry():
    return PromptRegistry.from_directory(ROOT / "prompts", lock_file="contracts.lock.json").freeze()


@pytest.mark.parametrize("sample", ["ordinary text", "Arabic عربي 🧪", '{{question}} {"x":1}'])
def test_migrated_messages_match_legacy_templates_byte_for_byte(registry, sample):
    legacy = json.loads((ROOT / "services/tests/fixtures/prompt_contracts_legacy.json").read_text())
    for reference in (SENTIMENT_REF, SUPERFORECAST_REF):
        contract = registry.get(reference)
        inputs = dict.fromkeys(contract.input_schema["properties"], sample)
        rendered = contract.render(inputs).to_messages_list()
        previous = legacy["templates"][reference.split("@")[0]]
        expected = []
        if previous["system"] is not None:
            expected.append({"role": "system", "content": previous["system"]})
        expected.append({"role": "user", "content": previous["user"].format(**inputs)})
        assert rendered == expected


def test_invoke_validates_before_model_call_and_returns_identity(registry):
    contract = registry.get(SENTIMENT_REF)
    model = SimpleNamespace(
        ainvoke=AsyncMock(return_value=AIMessage(content=json.dumps(SENTIMENT)))
    )
    with pytest.raises(InputValidationError):
        asyncio.run(invoke_contract(model, contract, {"question": "private caller data"}))
    model.ainvoke.assert_not_awaited()
    result, identity = asyncio.run(
        invoke_contract(
            model, contract, {"question": "Question", "outcome": "YES", "content": "Evidence"}
        )
    )
    assert result == SENTIMENT
    assert identity["contract_ref"] == SENTIMENT_REF
    assert identity["bundle_digest"] == contract.bundle_digest
    assert all(isinstance(value, str) for value in identity.values())


@pytest.mark.parametrize(
    "raw",
    [
        "```json\n" + json.dumps(SENTIMENT) + "\n```",
        "Here is the answer: " + json.dumps(SENTIMENT),
        "{}",
        json.dumps({**SENTIMENT, "sentiment_score": 2}),
        json.dumps({**SENTIMENT, "sentiment_score": "0.7"}),
        json.dumps({**SENTIMENT, "sentiment_score": True}),
        json.dumps({**SENTIMENT, "sentiment_score": float("nan")}),
        json.dumps({**SENTIMENT, "unexpected": "private model data"}),
        json.dumps(SENTIMENT)[:-1] + ',"sentiment_score":0.2}',
    ],
)
def test_invalid_model_output_is_rejected(registry, raw):
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content=raw)))
    with pytest.raises(OutputValidationError) as caught:
        asyncio.run(
            invoke_contract(
                model,
                registry.get(SENTIMENT_REF),
                {
                    "question": "Question",
                    "outcome": "YES",
                    "content": "Evidence",
                },
            )
        )
    assert "private model data" not in str(caught.value)


@pytest.mark.parametrize(
    "metadata,additional",
    [
        ({"finish_reason": "length"}, {}),
        ({"finish_reason": "content_filter"}, {}),
        ({"finish_reason": "tool_calls"}, {}),
        ({"finish_reason": "stop"}, {"refusal": "Cannot comply"}),
    ],
)
def test_incomplete_or_refused_response_never_becomes_valid_json(registry, metadata, additional):
    model = SimpleNamespace(
        ainvoke=AsyncMock(
            return_value=AIMessage(
                content=json.dumps(SENTIMENT),
                response_metadata=metadata,
                additional_kwargs=additional,
            )
        )
    )
    with pytest.raises(IncompleteCompletionError):
        asyncio.run(
            invoke_contract(
                model,
                registry.get(SENTIMENT_REF),
                {
                    "question": "Question",
                    "outcome": "YES",
                    "content": "Evidence",
                },
            )
        )


def test_startup_rejects_tampering_and_freezes_loaded_files(tmp_path):
    root = tmp_path / "prompts"
    shutil.copytree(ROOT / "prompts", root)
    registry = _load_registry(root)
    contract = registry.get(SENTIMENT_REF)
    before = contract.render({"question": "Q", "outcome": "YES", "content": "Evidence"})
    template = root / "contracts/polymarket.sentiment-analysis/1.0.0/user.md"
    template.write_text("tampered {{question}} {{outcome}} {{content}}")
    assert contract.render({"question": "Q", "outcome": "YES", "content": "Evidence"}) == before
    _load_registry.cache_clear()
    with pytest.raises(LockMismatchError):
        _load_registry(root)


def sentiment_engine(registry, output):
    engine = object.__new__(TradingAnalysisChain)
    engine.prompt_contracts = registry
    engine.search_tool = Mock()
    engine.search_tool.run.return_value = "Independent polling report"
    engine._research_with_tavily = AsyncMock(return_value="")
    engine._get_binance_context = AsyncMock(return_value="")
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content=json.dumps(output))))
    engine._get_task_llm = Mock(return_value=model)
    return engine, model


def test_actual_sentiment_chain_uses_contract_and_preserves_identity(registry):
    engine, model = sentiment_engine(registry, SENTIMENT)
    result = asyncio.run(
        engine.analyze_sentiment("Question", include_news=False, include_social=False)
    )
    assert result["sentiment_score"] == 0.7
    assert result["label"] == "bullish"
    assert result["source_count"] == 1
    assert result["prompt_contract"]["contract_ref"] == SENTIMENT_REF
    assert "Independent polling report" in model.ainvoke.call_args.args[0][0].content


@pytest.mark.parametrize(
    "output,error",
    [
        ({}, OutputValidationError),
        ({**SENTIMENT, "label": "neutral"}, ValueError),
    ],
)
def test_actual_sentiment_chain_has_no_neutral_fallback(registry, output, error):
    engine, _ = sentiment_engine(registry, output)
    with pytest.raises(error):
        asyncio.run(engine.analyze_sentiment("Question", include_news=False, include_social=False))


def forecast_engine(registry, forecast):
    engine = object.__new__(TradingAnalysisChain)
    engine.prompt_contracts = registry
    store = json.loads((ROOT / "prompts/prompts.json").read_text())
    engine.prompts = SimpleNamespace(get=lambda category, key: store[category][key])
    engine.search_tool = Mock()
    engine.search_tool.run.return_value = "Independent polling report"
    model = SimpleNamespace(
        ainvoke=AsyncMock(
            side_effect=[
                AIMessage(content='["fixture"]'),
                AIMessage(content=json.dumps(forecast)),
                AIMessage(content='{"side":"YES","size":10,"reasoning":"Evidence favors passage"}'),
            ]
        )
    )
    engine._get_task_llm = Mock(return_value=model)
    return engine, model


MARKETS = [
    {
        "slug": "fixture",
        "condition_id": "condition-1",
        "question": FORECAST["question"],
        "description": "Official tally rule",
        "outcomes": ["YES", "NO"],
        "outcomePrices": ["0.4", "0.6"],
        "volume_24h": 100,
        "liquidity": 1000,
    }
]


def test_actual_forecast_uses_validated_probability_and_confidence_units(registry):
    engine, model = forecast_engine(registry, FORECAST)
    result = asyncio.run(engine.discover_best_trade(MARKETS))
    candidate = result["all_candidates"][0]
    assert candidate["p_yes"] == 0.7
    assert candidate["edge"] == 0.3
    assert candidate["confidence"] == 80
    assert candidate["prompt_contract"]["contract_ref"] == SUPERFORECAST_REF
    assert model.ainvoke.await_count == 3


@pytest.mark.parametrize(
    "forecast,error",
    [
        ({}, OutputValidationError),
        ({**FORECAST, "p_yes": 1.1}, OutputValidationError),
        ({**FORECAST, "confidence": 80}, OutputValidationError),
        ({**FORECAST, "question": "Another market"}, ValueError),
    ],
)
def test_invalid_forecast_stops_before_best_trade_selection(registry, forecast, error):
    engine, model = forecast_engine(registry, forecast)
    with pytest.raises(error):
        asyncio.run(engine.discover_best_trade(MARKETS))
    assert model.ainvoke.await_count == 2


@pytest.mark.parametrize("change", ["none", "mutate", "remove", "add"])
def test_ci_gate_rejects_rewriting_published_versions(tmp_path, change):
    import subprocess

    from prompt_contracts.identity import write_lock_atomic
    from prompt_contracts.registry import generate_lock_for_directory

    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/check-prompt-contracts.py", tmp_path / "scripts")
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")

    def git(*args):
        return subprocess.check_output(  # noqa: S603 - controlled temporary repository
            ["git", *args],  # noqa: S607 - controlled Git arguments
            cwd=tmp_path,
            text=True,
        ).strip()

    git("init", "-q")
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-qm",
        "baseline",
    )
    baseline = git("rev-parse", "HEAD")
    bundle = tmp_path / "prompts/contracts/polymarket.sentiment-analysis/1.0.0"
    if change == "mutate":
        template = bundle / "user.md"
        template.write_text(template.read_text() + "\nChanged behavior.\n")
    elif change == "remove":
        shutil.rmtree(bundle)
    elif change == "add":
        new = bundle.parent / "1.1.0"
        shutil.copytree(bundle, new)
        manifest = new / "contract.toml"
        manifest.write_text(manifest.read_text().replace('version = "1.0.0"', 'version = "1.1.0"'))
    if change != "none":
        candidate, _ = generate_lock_for_directory(tmp_path / "prompts/contracts")
        write_lock_atomic(
            lock=candidate, target_path=tmp_path / "prompts/contracts.lock.json", allow_update=True
        )
    result = subprocess.run(  # noqa: S603 - fixed script and interpreter, controlled Git SHA
        [
            sys.executable,
            str(tmp_path / "scripts/check-prompt-contracts.py"),
            "--baseline-ref",
            baseline,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if change in {"none", "add"}:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert "create a new version" in result.stderr
