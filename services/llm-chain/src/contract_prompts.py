"""Locked prompt artifacts shared by production calls and offline evaluations."""

from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from prompt_contracts import Contract, PromptRegistry, __version__
from prompt_contracts.adapters.gateway import validate_gateway_response

from src.config import get_settings

SENTIMENT_REF = "polymarket.sentiment-analysis@1.0.0"
SUPERFORECAST_REF = "polymarket.superforecast@1.0.0"
_MESSAGE_TYPES = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}


@lru_cache(maxsize=4)
def _load_registry(root: Path) -> PromptRegistry:
    registry = PromptRegistry.from_directory(root, lock_file="contracts.lock.json").freeze()
    for reference in (SENTIMENT_REF, SUPERFORECAST_REF):
        registry.get(reference)
    return registry


def load_prompt_contracts() -> PromptRegistry:
    """Verify at startup; only the default container path has a checkout equivalent."""
    settings = get_settings()
    root = Path(settings.prompt_contracts_root or Path(settings.prompts_path).parent)
    if not settings.prompt_contracts_root and root == Path("/app/prompts") and not root.exists():
        root = Path(__file__).resolve().parents[3] / "prompts"
    return _load_registry(root.resolve())


async def invoke_contract(
    llm: Any, contract: Contract, inputs: dict[str, Any]
) -> tuple[dict, dict]:
    """Render once, use the existing transport, then validate completion status and JSON."""
    rendered = contract.render(inputs)
    response = await llm.ainvoke(
        [
            _MESSAGE_TYPES[str(message.role)](content=message.content)
            for message in rendered.messages
        ]
    )
    # Reuse the library's status normalizer with LangChain's completion fields;
    # it does not construct an SDK client or change provider/retry ownership.
    metadata = response.response_metadata or {}
    additional = response.additional_kwargs or {}
    output = validate_gateway_response(
        contract,
        {
            "choices": [
                {
                    "finish_reason": metadata.get("finish_reason"),
                    "message": {
                        "content": response.content,
                        "refusal": additional.get("refusal"),
                        "tool_calls": response.tool_calls or additional.get("tool_calls"),
                    },
                }
            ]
        },
    )
    identity = {
        "contract_ref": str(rendered.contract_ref),
        "bundle_digest": rendered.bundle_digest,
        "renderer": rendered.renderer,
        "policy_id": rendered.policy_id,
        "library_version": __version__,
    }
    return output, identity
