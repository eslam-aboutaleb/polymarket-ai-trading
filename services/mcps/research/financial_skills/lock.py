"""Regenerate ``manifest.lock.json`` after a reviewed change to a skill source.

Run from ``services/mcps/research``: ``python -m financial_skills.lock``.
The lock is committed; CI fails if it differs from the source hashes.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
SKILLS = [
    {
        "skill_id": "quote_consistency",
        "version": "1.0.0",
        "source_file": "quote_consistency.py",
        "supported_market_types": ["binary"],
        "required_observations": ["yes_quote", "no_quote", "rule_hash", "decision_time_utc"],
        "timeframes": ["snapshot"],
        "evidence_class": "descriptive_unvalidated",
        "license": "PolyForm-Noncommercial-1.0.0",
        "permission": "read_only",
        "credential_access": False,
        "network_access": False,
        "resource_budget": {
            "wall_seconds": 1.0,
            "max_input_bytes": 16384,
            "max_output_bytes": 16384,
            "network": False,
        },
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "condition_id",
                "token_ids",
                "observed_at_utc",
                "decision_time_utc",
                "quotes",
                "data_quality",
            ],  # fmt: skip
            "properties": {
                "condition_id": {"type": "string", "pattern": r"[A-Za-z0-9_\-:.]{1,100}"},
                "token_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "items": {"type": "string", "pattern": r"[A-Za-z0-9_\-:.]{1,100}"},
                },
                "rule_hash": {"type": "string", "pattern": r"[0-9a-f]{64}"},
                "observed_at_utc": {"type": "string", "pattern": r"[0-9T:\-.+Z]{20,40}"},
                "decision_time_utc": {"type": "string", "pattern": r"[0-9T:\-.+Z]{20,40}"},
                "data_quality": {"type": "string", "enum": ["ok", "degraded", "unknown"]},
                "quotes": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["yes", "no"],
                    "properties": {
                        side: {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["bid", "ask", "observed_at_utc"],
                            "properties": {
                                "bid": {"type": "number", "minimum": 0, "maximum": 1},
                                "ask": {"type": "number", "minimum": 0, "maximum": 1},
                                "observed_at_utc": {
                                    "type": "string",
                                    "pattern": r"[0-9T:\-.+Z]{20,40}",
                                },
                            },
                        }
                        for side in ("yes", "no")
                    },
                },
                "external_reference": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source", "implied_yes", "observed_at_utc"],
                    "properties": {
                        "source": {"type": "string", "enum": ["oracle", "spot_feed", "other"]},
                        "implied_yes": {"type": "number", "minimum": 0, "maximum": 1},
                        "observed_at_utc": {
                            "type": "string",
                            "pattern": r"[0-9T:\-.+Z]{20,40}",
                        },
                    },
                },
            },
        },
        "output_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["features", "probability", "interval", "abstain_reason", "warnings"],
            "properties": {
                "features": {"type": "object"},
                "probability": {"type": "null"},
                "interval": {
                    "type": "object",
                    "nullable": True,
                    "additionalProperties": False,
                    "required": ["low", "high", "basis"],
                    "properties": {
                        "low": {"type": "number", "minimum": 0, "maximum": 1},
                        "high": {"type": "number", "minimum": 0, "maximum": 1},
                        "basis": {"type": "string"},
                    },
                },
                "abstain_reason": {"type": "string", "nullable": True},
                "warnings": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
            },
        },
    }
]


def build() -> dict:
    skills = []
    for skill in SKILLS:
        entry = dict(skill)
        entry["sha256"] = hashlib.sha256((HERE / skill["source_file"]).read_bytes()).hexdigest()
        skills.append(entry)
    return {"lock_version": "financial-skills-lock-v1", "skills": skills}


def main() -> None:
    (HERE / "manifest.lock.json").write_text(json.dumps(build(), indent=2, sort_keys=True) + "\n")
    sys.stdout.write("wrote manifest.lock.json\n")


if __name__ == "__main__":
    main()
