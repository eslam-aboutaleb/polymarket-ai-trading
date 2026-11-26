"""SkillsAdapter: allowlisted, hash-pinned, read-only skill execution.

Safety model (issue #28):

* Allowlist: only skills listed in ``manifest.lock.json`` AND registered in
  ``_ENTRYPOINTS`` below can run. Nothing is imported dynamically from
  market, user or network content, and ``SKILL.md`` text is never executed.
* Pinning: the manifest records the SHA-256 of each skill source file. The
  adapter re-hashes the file before every run; a mismatch fails closed.
* Fail closed: unknown skill, version mismatch, input or output schema
  mismatch, hash mismatch, timeout or an unexpected exception all return
  ``status="error"`` with a reason code and ``execution_eligible=False``.
* Bounded: serialized input and output size caps and a wall-clock limit.
* Read-only: the adapter has no network, credential or order surface and the
  result is always ``execution_eligible=False``.
* Kill switch: ``FINANCIAL_SKILLS_ENABLED`` (default off) is read on every
  call, so turning it off restores existing behaviour immediately.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from financial_skills import quote_consistency

logger = logging.getLogger(__name__)

RESULT_SCHEMA_VERSION = "financial-skill-result-v1"
LOCK_PATH = Path(__file__).with_name("manifest.lock.json")
SKILL_DIR = Path(__file__).parent
ENABLED_ENV = "FINANCIAL_SKILLS_ENABLED"

# Code-level allowlist: the lock file cannot name an entrypoint that is not here.
_ENTRYPOINTS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "quote_consistency": quote_consistency.evaluate,
}


def skills_enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ── minimal JSON-schema validator (type, required, properties, enum, ...) ──


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return a list of violations; empty means valid. Unknown keywords fail closed."""
    known = {
        "type", "required", "properties", "additionalProperties", "enum", "pattern",
        "minimum", "maximum", "minLength", "maxLength", "items", "minItems", "maxItems",
        "description", "const", "nullable", "format",
    }  # fmt: skip
    errors = [f"{path}: unsupported schema keyword {k}" for k in schema if k not in known]
    if value is None and schema.get("nullable"):
        return errors
    expected = schema.get("type")
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if expected and not checks[expected](value):
        return [*errors, f"{path}: expected {expected}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: not an allowed value")
    if isinstance(value, str):
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            errors.append(f"{path}: does not match pattern")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10**9):
            errors.append(f"{path}: bad length")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10**9):
            errors.append(f"{path}: bad item count")
        for index, item in enumerate(value):
            errors += validate(item, schema.get("items", {}), f"{path}[{index}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errors += [f"{path}.{k}: required" for k in schema.get("required", []) if k not in value]
        if schema.get("additionalProperties") is False:
            errors += [f"{path}.{k}: unexpected field" for k in value if k not in props]
        for key, sub in props.items():
            if key in value:
                errors += validate(value[key], sub, f"{path}.{key}")
    return errors


class SkillsAdapter:
    """Runs allowlisted read-only skills and records telemetry."""

    def __init__(self, lock_path: Path = LOCK_PATH, skill_dir: Path = SKILL_DIR):
        self.lock_path = lock_path
        self.skill_dir = skill_dir
        self._lock = threading.Lock()
        self._telemetry: Counter[str] = Counter()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="financial-skill"
        )

    # ── manifest ──────────────────────────────────────────────────
    def _load_manifest(self, skill_id: str) -> dict[str, Any] | None:
        try:
            lock = json.loads(self.lock_path.read_text())
        except (OSError, ValueError):
            return None
        if lock.get("lock_version") != "financial-skills-lock-v1":
            return None
        for entry in lock.get("skills", []):
            if entry.get("skill_id") == skill_id:
                return entry
        return None

    @staticmethod
    def manifest_sha256(manifest: dict[str, Any]) -> str:
        return sha256_text(canonical_json(manifest))

    def describe(self) -> dict[str, Any]:
        """Public, read-only description of the allowlisted skills."""
        try:
            lock = json.loads(self.lock_path.read_text())
            skills = [
                {
                    k: s[k]
                    for k in (
                        "skill_id",
                        "version",
                        "supported_market_types",
                        "required_observations",
                        "timeframes",
                        "input_schema",
                        "output_schema",
                        "evidence_class",
                        "license",
                        "resource_budget",
                        "permission",
                        "credential_access",
                    )
                }  # fmt: skip
                for s in lock["skills"]
            ]
        except (OSError, ValueError, KeyError):
            skills = []
        return {
            "name": "financial_skill",
            "read_only": True,
            "version": RESULT_SCHEMA_VERSION,
            "enabled": skills_enabled(),
            "description": (
                "Versioned read-only research skills over a frozen market context. Research "
                "evidence only: no orders, signing, credentials or network access."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string"},
                    "skill_version": {"type": "string"},
                    "context": {"type": "object"},
                    "experiment_id": {"type": "string"},
                },
                "required": ["skill_id", "skill_version", "context"],
                "additionalProperties": False,
            },
            "skills": skills,
        }

    # ── execution ─────────────────────────────────────────────────
    def telemetry(self) -> dict[str, int]:
        with self._lock:
            return dict(self._telemetry)

    def _count(self, key: str) -> None:
        with self._lock:
            self._telemetry[key] += 1

    def _result(self, status: str, reason: str, skill_id: str = "", **extra: Any) -> dict[str, Any]:
        self._count(f"status:{status}")
        if reason:
            self._count(f"reason:{reason}")
        # Callable extras are evaluated after counting so telemetry includes this call.
        extra = {k: (v() if callable(v) else v) for k, v in extra.items()}
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "execution_eligible": False,
            "read_only": True,
            "status": status,
            "reason": reason,
            "skill_id": skill_id,
            **extra,
        }

    def run(self, request: Any, *, now: str | None = None) -> dict[str, Any]:
        """Run one skill. Never raises; every failure is an explicit result."""
        started = time.perf_counter()
        if not skills_enabled():
            return self._result("disabled", "FINANCIAL_SKILLS_DISABLED")
        try:
            return self._run(request, now, started)
        except Exception as exc:  # fail closed on anything unexpected
            logger.warning("financial skill failed closed: %s", type(exc).__name__)
            return self._result("error", "SKILL_INTERNAL_ERROR")

    def _run(self, request: Any, now: str | None, started: float) -> dict[str, Any]:
        request_schema = self.describe()["input_schema"]
        if not isinstance(request, dict) or validate(request, request_schema):
            return self._result("error", "INPUT_SCHEMA_MISMATCH")
        skill_id = request["skill_id"]
        manifest = self._load_manifest(skill_id)
        entrypoint = _ENTRYPOINTS.get(skill_id)
        if manifest is None or entrypoint is None:
            return self._result("error", "UNKNOWN_SKILL", skill_id)
        if manifest.get("permission") != "read_only" or manifest.get("credential_access"):
            return self._result("error", "MANIFEST_NOT_READ_ONLY", skill_id)
        if request["skill_version"] != manifest["version"]:
            return self._result(
                "error", "SKILL_VERSION_MISMATCH", skill_id, expected_version=manifest["version"]
            )
        source = self.skill_dir / manifest["source_file"]
        try:
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
        except OSError:
            return self._result("error", "SKILL_SOURCE_UNAVAILABLE", skill_id)
        if actual != manifest["sha256"]:
            return self._result("error", "SKILL_HASH_MISMATCH", skill_id)

        budget = manifest["resource_budget"]
        context = dict(request["context"])
        if len(canonical_json(context)) > budget["max_input_bytes"]:
            return self._result("error", "INPUT_TOO_LARGE", skill_id)
        context.setdefault("decision_time_utc", now or "")
        if validate(context, manifest["input_schema"]):
            return self._result("error", "INPUT_SCHEMA_MISMATCH", skill_id)

        future = self._executor.submit(entrypoint, context)
        try:
            evidence = future.result(timeout=budget["wall_seconds"])
        except concurrent.futures.TimeoutError:
            future.cancel()
            return self._result("error", "SKILL_TIMEOUT", skill_id)
        if validate(evidence, manifest["output_schema"]) or (
            len(canonical_json(evidence)) > budget["max_output_bytes"]
        ):
            return self._result("error", "OUTPUT_SCHEMA_MISMATCH", skill_id)

        context_hash = sha256_text(canonical_json(context))
        manifest_hash = self.manifest_sha256(manifest)
        lineage = {
            "context_sha256": context_hash,
            "manifest_sha256": manifest_hash,
            "skill_source_sha256": actual,
        }
        # Reproducibility: same context + same pinned skill => same experiment id.
        experiment_id = request.get("experiment_id") or (
            f"skill-shadow:{skill_id}:{manifest['version']}:{context_hash[:16]}"
        )
        interval = evidence["interval"]
        baseline = evidence["features"].get("yes_mid")
        shadow = {
            "baseline": {"name": "market_yes_mid", "value": baseline},
            "skill_interval": interval,
            "baseline_within_skill_interval": (
                interval["low"] <= baseline <= interval["high"]
                if interval is not None and baseline is not None
                else None
            ),
            "decision_effect": "none_shadow_only",
        }
        abstain = evidence["abstain_reason"]
        self._count(f"skill:{skill_id}:{'abstain' if abstain else 'ok'}")
        return self._result(
            "abstain" if abstain else "ok",
            abstain or "",
            skill_id,
            skill_version=manifest["version"],
            evidence_level=manifest["evidence_class"],
            evidence=evidence,
            provenance={
                "lineage_hashes": lineage,
                "decision_time_utc": context["decision_time_utc"],
                "condition_id": context["condition_id"],
                "token_ids": context["token_ids"],
                "rule_hash": context.get("rule_hash"),
            },
            experiment_id=experiment_id,
            shadow_comparison=shadow,
            telemetry=lambda: {
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "counters": self.telemetry(),
            },
        )
