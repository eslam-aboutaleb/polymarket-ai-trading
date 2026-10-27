"""Explicit native binary outcome aliases, with original receipts left intact."""

import json


def canonical_binary_metadata(clob, gamma):
    """Map an explicit paired Up/Down contract to the binary model's YES/NO axes.

    No positional inference: both independent providers must declare the same
    two labels. All condition/token equality checks remain in the caller.
    """
    tokens = clob.get("tokens") if isinstance(clob, dict) else None
    labels = gamma.get("outcomes") if isinstance(gamma, dict) else None
    labels = json.loads(labels) if isinstance(labels, str) else labels
    if (
        not isinstance(tokens, list)
        or not isinstance(labels, list)
        or any(not isinstance(token, dict) for token in tokens)
    ):
        raise ValueError("BINARY_OUTCOME_MAP_UNAVAILABLE")
    native_labels = {str(v.get("outcome", "")).strip().upper() for v in tokens}
    gamma_labels = {str(v).strip().upper() for v in labels}
    if native_labels == gamma_labels == {"UP", "DOWN"} and len(tokens) == len(labels) == 2:
        aliases = {"UP": "Yes", "DOWN": "No"}
        return (
            {
                **clob,
                "tokens": [
                    {**v, "outcome": aliases[str(v["outcome"]).strip().upper()]} for v in tokens
                ],
            },
            {**gamma, "outcomes": [aliases[str(v).strip().upper()] for v in labels]},
        )
    return clob, gamma
