"""Git sources must be inventoried, content-pinned and fail closed without executing source."""

import copy
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audit
import source_provenance as source
from policy import evaluate


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        files = {
            "LICENSE": b"MIT License\nSynthetic fixture only\n",
            "pyproject.toml": (
                b'[project]\nname="example-sdk"\nversion="1.0.0"\n'
                b'[build-system]\nrequires=["hatchling>=1.24,<1.27"]\n'
                b'build-backend="hatchling.build"\n'
            ),
            "src/example.py": b"raise RuntimeError('Source must never be imported by the audit')\n",
        }
        checkout = self.root / "checkout"
        checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)  # noqa: S603,S607
        for name, data in files.items():
            path = checkout / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        subprocess.run(["git", "add", "."], cwd=checkout, check=True)  # noqa: S603,S607
        tree = subprocess.check_output(["git", "write-tree"], cwd=checkout, text=True).strip()  # noqa: S603,S607
        self.records = [
            {"path": p, "sha256": hashlib.sha256(b).hexdigest()} for p, b in sorted(files.items())
        ]
        content = hashlib.sha256(
            json.dumps(self.records, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.archive = self.archive_bytes(files)
        constraint = {"requirement": "hatchling==1.26.3", "hashes": ["sha256:" + "b" * 64]}
        self.entry = {
            "name": "example-sdk",
            "version": "1.0.0",
            "repository": "example/sdk",
            "tag": "v1.0.0",
            "commit": "a" * 40,
            "tree": tree,
            "content_sha256": content,
            "license": "MIT",
            "build_project": "tools/build",
            "build_constraints": [constraint],
            "review_evidence": "security/source-reviews/sdk.json",
        }
        self.manifest = {"schema_version": 1, "sources": [self.entry]}
        self.package = {
            "name": "example-sdk",
            "version": "1.0.0",
            "source": {"git": "https://github.com/example/sdk.git?rev=v1.0.0#" + "a" * 40},
        }
        self.config = {
            "project": {"name": "application"},
            "tool": {
                "uv": {"require-build-hashes": True, "build-constraint-dependencies": [constraint]}
            },
        }
        build = self.root / "tools/build"
        build.mkdir(parents=True)
        (build / "uv.lock").write_text(
            '[[package]]\nname="hatchling"\nversion="1.26.3"\n'
            'source={registry="https://pypi.org/simple"}\n'
            'wheels=[{url="https://files.pythonhosted.org/hatchling-1.26.3-py3-none-any.whl",'
            'hash="sha256:' + "b" * 64 + '"}]\n'
        )
        evidence = self.root / self.entry["review_evidence"]
        evidence.parent.mkdir(parents=True)
        evidence.write_text(json.dumps({"commit": "a" * 40, "files": self.records}))

    @staticmethod
    def archive_bytes(files):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as archive:
            for name, data in files.items():
                item = tarfile.TarInfo("source/" + name)
                item.size = len(data)
                item.mode = 0o644
                archive.addfile(item, io.BytesIO(data))
        return raw.getvalue()

    def response(self, url, payload=None):
        if "/git/ref/" in url:
            return json.dumps(
                {
                    "object": {
                        "sha": "a" * 40,
                        "type": "commit",
                        "url": "https://api.github.com/repos/example/sdk/git/commits/" + "a" * 40,
                    }
                }
            ).encode()
        if "/git/commits/" in url:
            return json.dumps({"sha": "a" * 40, "tree": {"sha": self.entry["tree"]}}).encode()
        if "codeload.github.com" in url:
            return self.archive
        self.assertEqual(payload, {"commit": "a" * 40})
        return b"{}"

    def test_approved_source_is_verified_and_present_as_a_separate_inventory_entry(self):
        with patch.object(source, "fetch", side_effect=self.response):
            record = source.audit_source(self.package, self.manifest, self.root, self.config, {})
        self.assertEqual(record["package"], "example-sdk")
        self.assertEqual(record["commit_advisory_scan"], {})
        self.assertEqual(record["registry_advisory_scan"], "not_a_registry_package")
        self.assertEqual(
            audit.python_packages({"package": [self.package]}, self.config, self.manifest), set()
        )
        with self.assertRaises(ValueError):
            audit.python_packages({"package": [self.package]}, self.config)

    def test_unreviewed_source_origin_tag_and_commit_cannot_be_excluded(self):
        for value in [
            "https://evil.invalid/sdk.git#" + "a" * 40,
            "https://github.com/example/sdk.git?rev=main#" + "a" * 40,
            "https://github.com/example/sdk.git?rev=v1.0.0#" + "b" * 40,
        ]:
            changed = {**self.package, "source": {"git": value}}
            with self.subTest(value=value), self.assertRaises(ValueError):
                audit.python_packages({"package": [changed]}, self.config, self.manifest)

    def test_reviewed_commit_pin_keeps_tree_content_and_build_verification(self):
        self.entry.pop("tag")
        self.package["source"]["git"] = (
            "https://github.com/example/sdk.git?rev=" + "a" * 40 + "#" + "a" * 40
        )
        with patch.object(source, "fetch", side_effect=self.response) as fetch:
            record = source.audit_source(self.package, self.manifest, self.root, self.config, {})
        self.assertEqual(record["tree"], self.entry["tree"])
        self.assertFalse(any("/git/ref/" in call.args[0] for call in fetch.call_args_list))
        self.package["source"]["git"] = "https://github.com/example/sdk.git?rev=main#" + "a" * 40
        with self.assertRaises(ValueError):
            source.source_entry(self.package, self.manifest)

    def test_modified_content_or_unsafe_archive_cannot_match_reviewed_tree(self):
        for archive in [
            self.archive_bytes({"src/example.py": b"changed"}),
            self.archive_bytes({"../escape": b"bad"}),
        ]:
            with self.assertRaises(ValueError):
                source.verify_archive(archive, self.entry)

    def test_moved_tag_and_advisory_service_failure_fail_closed(self):
        with (
            patch.object(source, "fetch", return_value=b'{"object":{"sha":"changed"}}'),
            self.assertRaises(ValueError),
        ):
            source.audit_source(self.package, self.manifest, self.root, self.config, {})

        def failed(url, payload=None):
            return b'{"error":"unavailable"}' if "osv.dev" in url else self.response(url, payload)

        with patch.object(source, "fetch", side_effect=failed), self.assertRaises(ValueError):
            source.audit_source(self.package, self.manifest, self.root, self.config, {})

    def test_floating_missing_or_different_build_inputs_fail(self):
        for change in [{"require-build-hashes": False}, {"build-constraint-dependencies": []}]:
            config = copy.deepcopy(self.config)
            config["tool"]["uv"].update(change)
            project, _ = source.verify_archive(self.archive, self.entry)
            with self.assertRaises(ValueError):
                source.verify_build(self.root, self.entry, project, config)

    def test_every_git_advisory_blocks_including_low_or_unknown_severity(self):
        for severity in ["low", "unknown", "high"]:
            finding = {
                "project": "backend",
                "ecosystem": "git",
                "package": "example-sdk",
                "version": "1.0.0",
                "advisory": "CVE-SYNTHETIC",
                "path": "uv.lock:sdk",
                "scope": "runtime",
                "severity": severity,
                "lock_sha256": "a" * 64,
            }
            result = evaluate([finding], {"schema_version": 1, "exceptions": []})
            self.assertEqual(result["blocked"], [finding])
