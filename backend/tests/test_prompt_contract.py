"""Prompt contract tests.

The AI layer renders templates from ``prompts/prompts.json`` via ``str.format()``.
A template that requires a placeholder the call site does not supply raises
``KeyError`` at request time -- not at import time, not in CI, but in production
while a bot is deciding whether to move real capital.

These tests make that failure mode loud and static:

* every template in the store must parse cleanly as a format string
  (catches unescaped ``{``/``}`` in embedded JSON schemas);
* every ``prompts.get(category, key)`` call site in the repository is resolved
  to its template, and the keyword arguments supplied to ``.format()`` must be
  exactly the set of placeholders that template requires;
* the ``version`` field is present and readable, so prompt changes are traceable;
* templates are reachable from code -- no silently dead entries.

Run with::

    pytest backend/tests/test_prompt_contract.py -v
"""

from __future__ import annotations

import ast
import json
import string
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_PATH = REPO_ROOT / "prompts" / "prompts.json"

SCAN_GLOBS = ("backend/app/**/*.py", "services/**/*.py")

#: Expressions that resolve to a prompt store / manager.
PROMPT_GETTERS = frozenset(
    {
        "prompts",
        "PROMPTS",
        "self.prompts",
        "_prompts",
        "self._prompts",
        "prompt_manager",
        "self.prompt_manager",
    }
)

#: Getters that use a flat, single-argument lookup instead of (category, key).
FLAT_GETTERS = frozenset({"PROMPTS"})

#: Templates that no call site references yet. Each entry is a deliberate,
#: acknowledged decision -- a new unreferenced template fails the suite so dead
#: entries cannot accumulate silently. Delete the entry when the template is
#: either wired up or removed from the store.
KNOWN_UNUSED_TEMPLATES = {
    "research.source_evaluation",
    "research.synthesize_research",
    "trading.position_sizing",
    "trading.portfolio_rebalance",
    "risk_management.portfolio_risk_report",
    "monitoring.price_alert_analysis",
    "monitoring.daily_portfolio_summary",
    "trader_analysis.pattern_evaluation",
    "copy_trading.risk_check",
    "easy_trade_analysis.event_batch_scoring",
}


# --------------------------------------------------------------------------
# store access
# --------------------------------------------------------------------------


def _load_store() -> dict[str, dict[str, str]]:
    raw = json.loads(PROMPTS_PATH.read_text())
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


STORE = _load_store()


def _python_sources() -> list[Path]:
    found: set[Path] = set()
    for pattern in SCAN_GLOBS:
        found |= {p for p in REPO_ROOT.glob(pattern) if ".kilo" not in p.parts}
    return sorted(found)


def _placeholders(template: str) -> tuple[set[str], list[str]]:
    """Split a template into valid field names and malformed ones.

    ``string.Formatter`` correctly ignores ``{{``/``}}`` escapes, so a schema
    block that *is* escaped yields no fields. A schema block that is *not*
    escaped surfaces here as a bogus field name such as ``'\\n  "recommendation"'``.
    """
    required: set[str] = set()
    malformed: list[str] = []
    try:
        for _literal, field, _spec, _conv in string.Formatter().parse(template):
            if field is None:
                continue
            base = field.split(".")[0].split("[")[0].strip()
            if not base or not base.isidentifier():
                malformed.append(field)
            else:
                required.add(base)
    except ValueError as exc:  # unbalanced brace
        malformed.append(str(exc))
    return required, malformed


# --------------------------------------------------------------------------
# AST helpers
# --------------------------------------------------------------------------


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _expr_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _expr_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _is_prompt_getter(receiver: ast.AST) -> bool:
    return isinstance(receiver, ast.Name | ast.Attribute) and _expr_name(receiver) in PROMPT_GETTERS


def _category_of(call: ast.AST) -> str | None:
    """Return the category for ``prompts.get("category", ...)``."""
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
        return None
    if call.func.attr != "get" or not _is_prompt_getter(call.func.value):
        return None
    return _literal(call.args[0]) if call.args else None


def _getter_of(call: ast.AST) -> tuple[str | None, str] | None:
    """Return ``(category, key)`` if ``call`` reads the prompt store.

    Handles the manager form ``prompts.get(category, key)`` and the dict form
    ``prompts.get(category, {}).get(key, "")`` used by the analysis client.
    """
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
        return None
    if call.func.attr != "get":
        return None

    receiver = call.func.value
    if isinstance(receiver, ast.Call):
        # prompts.get(category, {}).get(key, "")
        category = _category_of(receiver)
        key = _literal(call.args[0]) if call.args else None
        return (category, key) if category and key else None

    if not _is_prompt_getter(receiver):
        return None
    args = [_literal(a) for a in call.args]
    if _expr_name(receiver) in FLAT_GETTERS:
        return (None, args[0]) if args and args[0] else None
    if len(args) >= 2 and args[0] and args[1]:
        return (args[0], args[1])
    return None


def _flatten(body: list[ast.stmt]) -> list[ast.stmt]:
    """Flatten nested blocks into leaf statements in source order."""
    out: list[ast.stmt] = []
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        out.append(stmt)
        for field in ("body", "orelse", "finalbody"):
            for child in getattr(stmt, field, []) or []:
                if isinstance(child, ast.stmt):
                    out.extend(_flatten([child]))
        for handler in getattr(stmt, "handlers", []) or []:
            out.extend(_flatten(handler.body))
    return out


def _own_expressions(stmt: ast.stmt):
    """Nodes belonging to this statement, without entering nested statements."""
    stack: list[ast.AST] = [stmt]
    while stack:
        node = stack.pop()
        if node is not stmt and isinstance(node, ast.stmt):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _scan_file(path: Path) -> list[dict]:
    """Resolve every formatted prompt call site in ``path``."""
    tree = ast.parse(path.read_text(), filename=str(path))
    rel = str(path.relative_to(REPO_ROOT))
    sites: list[dict] = []

    def visit(body: list[ast.stmt]) -> None:
        env: dict[str, tuple[str | None, str]] = {}
        for stmt in _flatten(body):
            for node in _own_expressions(stmt):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr != "format":
                    continue
                receiver = node.func.value
                binding = _getter_of(receiver)
                if binding is None:
                    name = _expr_name(receiver)
                    binding = env.get(name) if name else None
                if binding is not None:
                    sites.append(
                        {
                            "file": rel,
                            "line": node.lineno,
                            "category": binding[0],
                            "key": binding[1],
                            "call": node,
                        }
                    )

            if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                value = getattr(stmt, "value", None)
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                binding = _getter_of(value) if isinstance(value, ast.Call) else None
                # ``if not prompt: prompt = "inline fallback"`` renders the same
                # placeholders, so a string-literal reassignment keeps the binding.
                is_fallback = isinstance(value, ast.Constant) and isinstance(value.value, str)
                for target in targets:
                    if not isinstance(target, ast.Name):
                        continue
                    if binding:
                        env[target.id] = binding
                    elif not is_fallback:
                        env.pop(target.id, None)

        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(stmt.body)

    visit(tree.body)
    return sites


def _referenced_keys() -> set[str]:
    """Every (category, key) pair read anywhere in the scanned sources."""
    referenced: set[str] = set()
    for path in _python_sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            binding = _getter_of(node)
            if binding:
                category, key = binding
                referenced.add(f"{category}.{key}" if category else key)
    return referenced


def _all_sites() -> list[dict]:
    sites: list[dict] = []
    for path in _python_sources():
        try:
            sites.extend(_scan_file(path))
        except SyntaxError as exc:  # pragma: no cover - guards a broken checkout
            pytest.fail(f"{path} does not parse: {exc}")
    return sites


def _template_for(category: str | None, key: str) -> str | None:
    if category is None:
        return next((cat[key] for cat in STORE.values() if key in cat), None)
    return (STORE.get(category) or {}).get(key)


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------


def test_prompts_store_exists() -> None:
    assert PROMPTS_PATH.is_file(), f"missing prompt store at {PROMPTS_PATH}"
    assert STORE, "prompt store contains no categories"


def test_prompts_store_has_version() -> None:
    """`version` must be present and non-empty.

    It is the anchor for stamping every recorded decision, so an unversioned
    store makes "why did the bot do this last Tuesday?" unanswerable.
    """
    raw = json.loads(PROMPTS_PATH.read_text())
    version = raw.get("version")
    assert version, "prompts.json is missing a top-level 'version'"
    assert isinstance(version, str)
    parts = version.split(".")
    assert len(parts) >= 2, f"version {version!r} should look like MAJOR.MINOR"
    assert all(p.isdigit() for p in parts[:2]), f"version {version!r} should look like MAJOR.MINOR"


def test_every_template_is_a_valid_format_string() -> None:
    """Templates embed JSON schemas; unescaped braces break ``str.format``."""
    broken = []
    for category, keys in STORE.items():
        for key, template in keys.items():
            required, malformed = _placeholders(template)
            if malformed:
                broken.append(f"{category}.{key}: unescaped/invalid fields {malformed[:3]}")
            elif not isinstance(template, str) or not template.strip():
                broken.append(f"{category}.{key}: empty template")
    assert not broken, "templates must escape literal braces as {{ }}:\n" + "\n".join(broken)


def test_prompt_call_sites_satisfy_template_contract() -> None:
    """Every ``.format()`` must supply exactly the placeholders its template needs.

    This is the guard that turns a runtime ``KeyError`` inside a trading decision
    into a test failure.
    """
    violations: list[str] = []
    for site in _all_sites():
        category, key = site["category"], site["key"]
        label = f"{category}.{key}" if category else key
        where = f"{site['file']}:{site['line']}"

        template = _template_for(category, key)
        if template is None:
            violations.append(f"{where}: no template found for {label}")
            continue

        required, malformed = _placeholders(template)
        if malformed:
            violations.append(f"{where}: {label} template has unescaped fields {malformed[:2]}")
            continue

        call = site["call"]
        if call.args or any(kw.arg is None for kw in call.keywords):
            violations.append(
                f"{where}: {label} formatted with positional args or **kwargs; "
                "named placeholders require explicit keywords"
            )
            continue

        supplied = {kw.arg for kw in call.keywords}
        missing = required - supplied
        extra = supplied - required
        if missing:
            violations.append(f"{where}: {label} missing kwargs {sorted(missing)}")
        if extra:
            violations.append(f"{where}: {label} unexpected kwargs {sorted(extra)}")

    assert not violations, "prompt/template contract violations:\n" + "\n".join(violations)


def test_no_dead_prompt_templates() -> None:
    """Every template is either referenced by code or explicitly acknowledged."""
    referenced = _referenced_keys()
    dead = sorted(
        f"{category}.{key}"
        for category, keys in STORE.items()
        for key in keys
        if f"{category}.{key}" not in referenced
    )
    unexpected = sorted(set(dead) - KNOWN_UNUSED_TEMPLATES)
    assert not unexpected, (
        "templates defined but never referenced by any call site: "
        f"{unexpected}\nEither wire them up, or add them to KNOWN_UNUSED_TEMPLATES "
        "to acknowledge them, or delete them from prompts.json."
    )
    stale = sorted(KNOWN_UNUSED_TEMPLATES - set(dead))
    assert not stale, (
        f"KNOWN_UNUSED_TEMPLATES lists templates that are now referenced or removed: {stale}. "
        "Remove them from the allowlist."
    )
