# Versioned prompts in LLM-chain

LLM-chain uses `prompt-contracts` for public sentiment analysis and the superforecast stage of trade discovery. These calls validate their inputs, use locked prompt files, and validate model responses before returning a sentiment score or calculating forecast edges.

Other prompt operations, including discovery filtering and final trade selection, continue to use `prompts/prompts.json`. CLI-agent is unchanged.

## Run and deploy

Install the locked dependencies with `uv sync --project services/llm-chain --frozen`. The dependency is pinned to a full commit in the public `eslam-aboutaleb/prompt-contracts` repository. Its source manifest, content hashes, license, and build constraints are recorded under `security/` and checked by the existing source-provenance audit.

Both LLM-chain Docker images include the `prompts/` directory. Compose mounts the same directory read-only. When deploying manually, include these files together:

- `prompts/prompts.json` for the remaining legacy operations.
- `prompts/contracts/` for the versioned bundles.
- `prompts/contracts.lock.json` for their identities.

The registry loads and verifies the bundles at service startup, then retains a frozen snapshot. A missing bundle or lock mismatch prevents startup. `PROMPT_CONTRACTS_ROOT` overrides the directory containing `contracts/` and `contracts.lock.json`; otherwise it is the directory containing `PROMPTS_PATH`. Restart the service after deploying changed prompt assets.

## Response behavior

The initial bundle versions preserve the existing prompt messages byte for byte. Responses must be JSON matching their declared schema: fenced JSON, prose wrappers, duplicate keys, wrong field names, numeric strings, missing fields, and out-of-range probabilities fail the request. Truncated completions, refusals, and unexpected tool calls also fail. There is no neutral sentiment or market-price forecast fallback for invalid responses.

Sentiment labels must match the existing score thresholds. A superforecast must echo the requested market question. The forecast prompt requests confidence in the range 0–1; this is now converted to 0–100 for downstream candidate ranking and display.

Sentiment results and gRPC metadata include `contract_ref`, `bundle_digest`, `renderer`, `policy_id`, and `library_version`. Forecast candidates include the same fields under `prompt_contract`. These identify the prompt used without logging its input data. Structural validation does not establish factual accuracy or make a recommendation suitable for execution.

## Change a prompt

Create a new version directory and update its manifest, then update the reference in `services/llm-chain/src/contract_prompts.py`. Regenerate and review the lockfile:

```bash
uv run --project services/llm-chain --frozen prompt-contracts lint prompts/contracts
uv run --project services/llm-chain --frozen prompt-contracts lock prompts/contracts --output prompts/contracts.lock.json --update
uv run --project services/llm-chain --frozen prompt-contracts verify prompts/contracts --lock prompts/contracts.lock.json
uv run --project services/llm-chain --frozen pytest services/tests/test_prompt_contracts.py -q
```

CI verifies the catalog and rejects changing or deleting an already locked version relative to `main`. Add versions rather than rewriting existing ones. Evaluate behavioral changes separately with representative market inputs.

To roll back, redeploy the previous application image and matching prompt directory together. Do not mix the previous code references with a newer incomplete catalog.
