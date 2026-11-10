"""Negative controls for the real policy/parser and subprocess entry point."""

import copy
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audit
from policy import evaluate

TODAY = date(2026, 11, 10)
FINDING = {
    "project": "frontend",
    "ecosystem": "npm",
    "package": "example",
    "version": "1.0.0",
    "advisory": "GHSA-aaaa-bbbb-cccc",
    "path": "node_modules/example",
    "scope": "runtime",
    "severity": "high",
    "lock_sha256": "a" * 64,
}
EMPTY = {"schema_version": 1, "exceptions": []}


def exception():
    return {
        **FINDING,
        "owner": "repository-owner",
        "approved_by": "independent-reviewer",
        "justification": "Synthetic test approval, never a live risk decision",
        "reachability": "Synthetic code path is bounded by its existing input cap",
        "compensating_control": "Bounded inputs and capability disabled until upstream fix",
        "issue": "https://github.com/eslam-aboutaleb/polymarket-ai-trading/issues/42",
        "reviewed_on": "2026-11-09",
        "expires_on": "2026-11-20",
    }


class GateTests(unittest.TestCase):
    def test_new_high_and_critical_block_in_runtime_and_dev(self):
        for severity in ("high", "critical"):
            for scope in ("runtime", "development"):
                result = evaluate([{**FINDING, "severity": severity, "scope": scope}], EMPTY, TODAY)
                self.assertEqual(len(result["blocked"]), 1)

    def test_python_unknown_severity_is_never_treated_as_low(self):
        result = evaluate([{**FINDING, "ecosystem": "pypi", "severity": "unknown"}], EMPTY, TODAY)
        self.assertEqual(len(result["blocked"]), 1)

    def test_exact_current_approval_is_visible_with_all_evidence(self):
        result = evaluate([FINDING], {**EMPTY, "exceptions": [exception()]}, TODAY)
        self.assertFalse(result["blocked"])
        self.assertEqual(result["allowed"][0]["exception"]["owner"], "repository-owner")
        self.assertEqual(result["allowed"][0]["exception"]["expires_on"], "2026-11-20")

    def test_expired_future_and_long_lived_approvals_fail(self):
        for change in (
            {"expires_on": "2026-11-10"},
            {"reviewed_on": "2026-11-11"},
            {"expires_on": "2027-01-01"},
            {"approved_by": ""},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                evaluate([FINDING], {**EMPTY, "exceptions": [{**exception(), **change}]}, TODAY)

    def test_changed_or_unknown_approval_fails_instead_of_wildcard_matching(self):
        for field in (
            "project",
            "package",
            "version",
            "advisory",
            "path",
            "scope",
            "severity",
            "lock_sha256",
        ):
            finding = {
                **FINDING,
                field: {"scope": "development", "severity": "critical"}.get(field, "changed"),
            }
            with self.subTest(field=field), self.assertRaises(ValueError):
                evaluate([finding], {**EMPTY, "exceptions": [exception()]}, TODAY)
        with self.assertRaises(ValueError):
            evaluate([], {**EMPTY, "exceptions": [exception()]}, TODAY)

    def test_new_advisory_alongside_existing_approval_blocks(self):
        result = evaluate(
            [FINDING, {**FINDING, "advisory": "GHSA-dddd-eeee-ffff"}],
            {**EMPTY, "exceptions": [exception()]},
            TODAY,
        )
        self.assertEqual(len(result["allowed"]), 1)
        self.assertEqual(len(result["blocked"]), 1)

    def test_scanner_failure_and_timeout_cannot_be_an_exception(self):
        for error in (subprocess.TimeoutExpired("npm", 1),):
            with (
                patch.object(audit.subprocess, "run", side_effect=error),
                self.assertRaises(subprocess.SubprocessError),
            ):
                audit.run(["npm", "audit"], Path.cwd())
        with (
            patch.object(
                audit.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 2, "", "credential-like stderr"),
            ),
            self.assertRaises(RuntimeError) as raised,
        ):
            audit.run(["npm", "audit"], Path.cwd())
        self.assertNotIn("credential-like", str(raised.exception))

    def test_incomplete_error_and_empty_json_cannot_pass(self):
        for response in ('{"error":{"code":"E404"}}', "[]", "not-json"):
            with self.subTest(response=response), self.assertRaises(ValueError):
                audit.load_json(response)
        with self.assertRaises(ValueError):
            audit.npm_findings({}, {}, "frontend", "a" * 64)

    def test_parser_retains_transitive_path_and_environment(self):
        lock = {
            "packages": {
                "node_modules/example/node_modules/leaf": {"version": "1.0.0", "dev": True}
            }
        }
        report = {
            "auditReportVersion": 2,
            "metadata": {"vulnerabilities": {"total": 1}},
            "vulnerabilities": {
                "leaf": {
                    "via": [
                        {
                            "url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc",
                            "severity": "high",
                            "affected_nodes": ["node_modules/example/node_modules/leaf"],
                        }
                    ],
                    "nodes": list(lock["packages"]),
                    "isDirect": False,
                    "fixAvailable": True,
                }
            },
        }
        finding = audit.npm_findings(report, lock, "frontend", "a" * 64)[0]
        self.assertEqual(finding["path"], "node_modules/example/node_modules/leaf")
        self.assertEqual(finding["scope"], "development")
        self.assertFalse(finding["direct"])

    def test_every_locked_python_version_must_be_scanned_no_skip(self):
        package = {"name": "example", "version": "1.0.0", "vulns": []}
        packages = {("example", "1.0.0"), ("example", "2.0.0")}
        with self.assertRaises(ValueError):
            audit.python_findings(
                {"dependencies": [package]}, "backend", "a" * 64, packages, set(), set()
            )
        package["skip_reason"] = "unavailable"
        with self.assertRaises(ValueError):
            audit.python_findings(
                {"dependencies": [package]},
                "backend",
                "a" * 64,
                {("example", "1.0.0")},
                set(),
                set(),
            )

    def test_unknown_schema_duplicate_exceptions_and_extra_fields_rejected(self):
        for manifest in (
            {"schema_version": 2, "exceptions": []},
            {**EMPTY, "exceptions": [exception(), exception()]},
            {**EMPTY, "exceptions": [{**exception(), "ignore_all": True}]},
        ):
            with self.assertRaises(ValueError):
                evaluate([FINDING], manifest, TODAY)


class ProtectionTests(unittest.TestCase):
    def test_effective_server_rule_must_target_actions_quality_gate_and_owner_review(self):
        from protection import validate

        rules = [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": "Quality gate", "integration_id": 15368}
                    ],
                },
            },
            {
                "type": "pull_request",
                "parameters": {
                    "require_code_owner_review": True,
                    "dismiss_stale_reviews_on_push": True,
                },
            },
        ]
        validate(rules)
        stale = copy.deepcopy(rules)
        stale[1]["parameters"]["dismiss_stale_reviews_on_push"] = False
        with self.assertRaises(ValueError):
            validate(stale)
        for changed in ([], rules[:1], rules[1:]):
            with self.assertRaises(ValueError):
                validate(changed)
        for field, value in (("context", "Unrelated check"), ("integration_id", None)):
            changed = copy.deepcopy(rules)
            changed[0]["parameters"]["required_status_checks"][0][field] = value
            with self.assertRaises(ValueError):
                validate(changed)


class ProcessContractTests(unittest.TestCase):
    def test_real_gate_process_exits_nonzero_for_new_high_critical_and_expired_approval(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            findings, manifest = root / "findings.json", root / "exceptions.json"
            for severity in ("high", "critical"):
                findings.write_text(json.dumps([{**FINDING, "severity": severity}]))
                manifest.write_text(json.dumps(EMPTY))
                process = subprocess.run(  # noqa: S603 - repository policy in the current interpreter
                    [
                        sys.executable,
                        str(Path(audit.__file__).with_name("policy.py")),
                        str(findings),
                        str(manifest),
                        str(root / "out"),
                    ],
                    capture_output=True,
                )
                self.assertEqual(process.returncode, 1)
                self.assertEqual(
                    len(json.loads((root / "out/report.json").read_text())["blocked"]), 1
                )
            manifest.write_text(
                json.dumps({**EMPTY, "exceptions": [{**exception(), "expires_on": "2020-01-01"}]})
            )
            process = subprocess.run(  # noqa: S603 - repository policy in the current interpreter
                [
                    sys.executable,
                    str(Path(audit.__file__).with_name("policy.py")),
                    str(findings),
                    str(manifest),
                    str(root / "out"),
                ],
                capture_output=True,
            )
            self.assertEqual(process.returncode, 1)
            findings.write_text("[]")
            manifest.write_text(json.dumps(EMPTY))
            process = subprocess.run(  # noqa: S603 - repository policy in the current interpreter
                [
                    sys.executable,
                    str(Path(audit.__file__).with_name("policy.py")),
                    str(findings),
                    str(manifest),
                    str(root / "out"),
                ],
                capture_output=True,
            )
            self.assertEqual(process.returncode, 0)


class ProvenanceTests(unittest.TestCase):
    def test_unlocked_projects_and_omission_configuration_cannot_bypass(self):
        for files in (
            ["frontend/package.json"],
            ["new-service/pyproject.toml"],
            ["orphan/package-lock.json"],
        ):
            with self.assertRaises(ValueError):
                audit.inventory_projects(files)
        nodes, python = audit.inventory_projects(
            [
                "frontend/package.json",
                "frontend/package-lock.json",
                "backend/pyproject.toml",
                "backend/uv.lock",
            ]
        )
        self.assertEqual(nodes, ["frontend"])
        self.assertEqual(python, ["backend"])
        command = audit.node_command("audit")
        for group in ("prod", "dev", "optional", "peer"):
            self.assertIn(f"--include={group}", command)
        self.assertIn("--registry=https://registry.npmjs.org/", command)

    def test_unreviewed_executable_plugin_or_integrity_mismatch_is_rejected(self):
        import base64

        package = {
            "version": "1.0.0",
            "resolved": "https://registry.npmjs.org/example/-/example-1.0.0.tgz",
            "integrity": "sha512-" + base64.b64encode(b"a" * 64).decode(),
        }
        lock = {"lockfileVersion": 3, "packages": {"": {}, "node_modules/example": package}}
        audit.validate_node_lock(lock)
        for mutation in (
            {"resolved": "https://evil.invalid/mcp-send-wallet-key.tgz"},
            {"integrity": "invalid"},
            {"integrity": "sha512-YQ=="},
        ):
            changed = copy.deepcopy(lock)
            changed["packages"]["node_modules/example"].update(mutation)
            with self.assertRaises(ValueError):
                audit.validate_node_lock(changed)

    def test_unreviewed_python_editable_sources_never_disappear(self):
        config = {"project": {"name": "own-project"}}
        own = {"name": "own-project", "version": "1.0", "source": {"virtual": "."}}
        self.assertEqual(audit.python_packages({"package": [own]}, config), set())
        for source in (
            {"editable": "../unreviewed-plugin"},
            {"virtual": "../other"},
            {"git": "https://evil.invalid/tool"},
        ):
            with self.assertRaises(ValueError):
                audit.python_packages(
                    {"package": [own, {"name": "unreviewed", "version": "1.0", "source": source}]},
                    config,
                )


class NativeRangeTests(unittest.TestCase):
    def test_advisories_bind_only_to_the_installed_versions_in_their_range(self):
        report = {
            "vulnerabilities": {
                "example": {
                    "nodes": ["node_modules/one", "node_modules/two"],
                    "via": [{"range": "<2.0.0"}],
                }
            }
        }
        lock = {
            "packages": {
                "node_modules/one": {"version": "1.0.0"},
                "node_modules/two": {"version": "2.0.0"},
            }
        }
        bound = audit.bind_npm_ranges(report, lock)
        self.assertEqual(
            bound["vulnerabilities"]["example"]["via"][0]["affected_nodes"], ["node_modules/one"]
        )


class UnknownSeverityTests(unittest.TestCase):
    def test_unknown_severity_cannot_hide_a_later_severity_escalation(self):
        finding = {**FINDING, "severity": "unknown", "ecosystem": "pypi"}
        manifest = {**EMPTY, "exceptions": [{**exception(), **finding}]}
        with self.assertRaises(ValueError):
            evaluate([finding], manifest, TODAY)


class ManagerSelectionTests(unittest.TestCase):
    def test_competing_lock_manager_cannot_shadow_the_audited_graph(self):
        for path in (
            "frontend/npm-shrinkwrap.json",
            "frontend/yarn.lock",
            "frontend/pnpm-lock.yaml",
            "frontend/bun.lock",
        ):
            with self.assertRaises(ValueError):
                audit.inventory_projects(
                    ["frontend/package.json", "frontend/package-lock.json", path]
                )


class RegistryIntegrityTests(unittest.TestCase):
    def test_plausible_hash_for_another_release_or_origin_is_not_accepted(self):
        package = {
            "wheels": [
                {"url": "https://files.pythonhosted.org/package.whl", "hash": "sha256:" + "a" * 64}
            ]
        }
        metadata = {
            "urls": [
                {
                    "url": "https://files.pythonhosted.org/package.whl",
                    "digests": {"sha256": "a" * 64},
                }
            ]
        }
        audit.verify_python_artifacts(package, metadata)
        wrong = copy.deepcopy(package)
        wrong["wheels"][0]["hash"] = "sha256:" + "b" * 64
        with self.assertRaises(ValueError):
            audit.verify_python_artifacts(wrong, metadata)
        wrong["wheels"][0]["url"] = "https://evil.invalid/package.whl"
        with self.assertRaises(ValueError):
            audit.verify_python_artifacts(wrong, metadata)
