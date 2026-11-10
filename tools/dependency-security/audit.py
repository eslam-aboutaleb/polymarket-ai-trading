"""Audit every committed lockfile without installing application code."""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path
from urllib.parse import quote

from policy import enforce

ROOT = Path(__file__).resolve().parents[2]


def run(argv, cwd, codes=(0,), input_data=None):
    result = subprocess.run(  # noqa: S603 - fixed executables/argv, no shell or provider-supplied commands
        argv, cwd=cwd, capture_output=True, text=True, timeout=300, input=input_data
    )
    if result.returncode not in codes:
        # Provider stderr can include request metadata: retain it in the local
        # runner, never copy untrusted response/error text to logs or summaries.
        raise RuntimeError(f"{argv[0]} {argv[1]} failed (exit {result.returncode})")
    return result.stdout


def load_json(raw):
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("error"):
        raise ValueError("Invalid scanner response")
    return value


def inventory_projects(tracked):
    competing = {"yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json", "bun.lock", "bun.lockb"}
    if any(Path(path).name in competing for path in tracked):
        raise ValueError("Unreviewed competing Node lockfile/manager")
    nodes = sorted(str(Path(p).parent) for p in tracked if p.endswith("package.json"))
    python = sorted(str(Path(p).parent) for p in tracked if p.endswith("pyproject.toml"))
    for project in nodes:
        if str(Path(project) / "package-lock.json") not in tracked:
            raise ValueError("Every Node project requires its committed npm lockfile")
    if any(str(Path(p).parent) not in nodes for p in tracked if p.endswith("package-lock.json")):
        raise ValueError("Orphan Node lockfile")
    for project in python:
        if str(Path(project) / "uv.lock") not in tracked:
            raise ValueError("Every Python project requires its committed uv lockfile")
    return nodes, python


def node_command(*args):
    return [
        "npm",
        *args,
        "--registry=https://registry.npmjs.org/",
        "--include=prod",
        "--include=dev",
        "--include=optional",
        "--include=peer",
        "--ignore-scripts",
        "--userconfig=/dev/null",
        f"--globalconfig={ROOT / 'security/npmrc'}",
    ]


def validate_node_lock(lock):
    if lock.get("lockfileVersion") != 3 or len(lock.get("packages", {})) < 2:
        raise ValueError("Expected complete npm v3 lockfile")
    for path, package in lock["packages"].items():
        if not path:
            continue
        integrity = package.get("integrity", "")
        if (
            not integrity.startswith("sha512-")
            or len(base64.b64decode(integrity[7:], validate=True)) != 64
        ):
            raise ValueError("Node integrity missing or malformed")
        if not package.get("resolved", "").startswith(
            "https://registry.npmjs.org/"
        ) or not package.get("version"):
            raise ValueError("Node package lacks reviewed registry origin/version")


def python_packages(lock, config):
    own_name = re.sub(r"[-_.]+", "-", config["project"]["name"].lower())
    packages = set()
    for package in lock["package"]:
        source = package["source"]
        if source in ({"virtual": "."}, {"editable": "."}) and package["name"] == own_name:
            continue
        if source != {"registry": "https://pypi.org/simple"}:
            raise ValueError("Unaudited local, editable, git or non-PyPI dependency")
        artifacts = package.get("wheels", []) + ([package["sdist"]] if package.get("sdist") else [])
        if not artifacts or any(
            not re.fullmatch(r"sha256:[0-9a-f]{64}", a.get("hash", "")) for a in artifacts
        ):
            raise ValueError("Python package lacks artifact integrity")
        packages.add((package["name"], package["version"]))
    return packages


def verify_python_artifacts(package, metadata):
    official = {item["url"]: item["digests"]["sha256"] for item in metadata["urls"]}
    artifacts = package.get("wheels", []) + ([package["sdist"]] if package.get("sdist") else [])
    for artifact in artifacts:
        if not artifact["url"].startswith("https://files.pythonhosted.org/"):
            raise ValueError("Unreviewed Python artifact origin")
        if official.get(artifact["url"]) != artifact["hash"].removeprefix("sha256:"):
            raise ValueError("Python artifact differs from its named PyPI release")


def registry_integrity(lock, cache):
    records = []
    for package in lock["package"]:
        if "registry" not in package["source"]:
            continue
        key = package["name"], package["version"]
        if key not in cache:
            name, version = (quote(value, safe="") for value in key)
            url = f"https://pypi.org/pypi/{name}/{version}/json"
            with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - fixed HTTPS registry
                cache[key] = json.load(response)
        verify_python_artifacts(package, cache[key])
        records.append({"package": key[0], "version": key[1], "registry_sha256_verified": True})
    return records


def bind_npm_ranges(report, lock):
    # Use the audit toolchain's own semver implementation, never an application
    # dependency. JSON travels through stdin, not executable source/arguments.
    code = """
const {createRequire} = require('node:module');
const fs = require('node:fs');
const semver = createRequire(fs.realpathSync(process.argv[1]))('semver');
const {report,lock} = JSON.parse(fs.readFileSync(0, 'utf8'));
for (const value of Object.values(report.vulnerabilities)) {
  for (const advisory of value.via) {
    if (typeof advisory === 'object') {
      advisory.affected_nodes = value.nodes.filter(path => {
        const version = lock.packages[path].version;
        if (!semver.valid(version) || !semver.validRange(advisory.range)) {
          throw Error('Invalid version/range');
        }
        return semver.satisfies(version, advisory.range, {includePrerelease:true});
      });
    }
  }
}
process.stdout.write(JSON.stringify(report));
"""
    npm = shutil.which("npm")
    if not npm:
        raise ValueError("Pinned npm toolchain unavailable")
    return load_json(
        run(
            ["node", "-e", code, npm], ROOT, input_data=json.dumps({"report": report, "lock": lock})
        )
    )


def npm_findings(report, lock, project, lock_hash):
    if (
        report.get("auditReportVersion") != 2
        or not isinstance(report.get("vulnerabilities"), dict)
        or "metadata" not in report
    ):
        raise ValueError("Incomplete npm audit")
    findings = {}
    for name, problem in report["vulnerabilities"].items():
        for advisory in problem["via"]:
            if isinstance(advisory, str):
                continue  # Native npm meta-advisory; underlying advisory owns the decision.
            url = advisory.get("url", "")
            if not re.fullmatch(r"https://github.com/advisories/GHSA-[a-z0-9-]+", url):
                raise ValueError("Unknown npm advisory identity")
            for path in advisory["affected_nodes"]:
                package = lock["packages"][path]
                finding = {
                    "project": project,
                    "ecosystem": "npm",
                    "package": name,
                    "version": package["version"],
                    "advisory": url.rsplit("/", 1)[-1],
                    "path": path,
                    "scope": "development" if package.get("dev") else "runtime",
                    "severity": advisory["severity"],
                    "lock_sha256": lock_hash,
                    "direct": problem["isDirect"],
                    "fix": problem["fixAvailable"],
                }
                findings[json.dumps(finding, sort_keys=True)] = finding
    if (
        report["vulnerabilities"] or report["metadata"].get("vulnerabilities", {}).get("total", 0)
    ) and not findings:
        raise ValueError("No underlying advisory evidence in npm result")
    return list(findings.values())


def python_findings(report, project, lock_hash, packages, runtime, direct):
    dependencies = report.get("dependencies")
    if not isinstance(dependencies, list) or report.get("fixes") not in ({}, [], None):
        raise ValueError("Incomplete Python audit")
    scanned = set()
    findings = {}
    for dependency in dependencies:
        if dependency.get("skip_reason") or "vulns" not in dependency:
            raise ValueError("Python package was skipped")
        key = dependency["name"], dependency["version"]
        scanned.add(key)
        if key not in packages:
            raise ValueError("Python audit contains an unlocked package")
        for vulnerability in dependency["vulns"]:
            aliases = [a for a in vulnerability.get("aliases", []) if a.startswith("GHSA-")]
            advisory = sorted(aliases)[0] if aliases else vulnerability["id"]
            finding = {
                "project": project,
                "ecosystem": "pypi",
                "package": key[0],
                "version": key[1],
                "advisory": advisory,
                "path": f"uv.lock:{key[0]}=={key[1]}",
                "scope": "runtime" if key in runtime else "development",
                "severity": "unknown",
                "lock_sha256": lock_hash,
                "direct": key[0] in direct,
                "fix": vulnerability["fix_versions"],
            }
            findings[json.dumps(finding, sort_keys=True)] = finding
    if scanned != packages:
        raise ValueError("Python scanner did not cover every locked registry package")
    return list(findings.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "security-artifacts")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    tracked = run(["git", "ls-files", "-z"], ROOT).split("\0")
    nodes, projects = inventory_projects(tracked)
    findings, inventory = [], []
    registry_cache = {}
    for project in nodes:
        lock_path = ROOT / project / "package-lock.json"
        raw = lock_path.read_bytes()
        lock = json.loads(raw)
        validate_node_lock(lock)
        lock_hash = hashlib.sha256(raw).hexdigest()
        directory = output / project.replace("/", "-")
        directory.mkdir(parents=True, exist_ok=True)
        report = load_json(
            run(
                node_command(
                    "audit",
                    "--package-lock-only",
                    "--json",
                    "--fetch-retries=0",
                    "--fetch-timeout=60000",
                ),
                ROOT / project,
                (0, 1),
            )
        )
        (directory / "audit.json").write_text(json.dumps(report, indent=2))
        for filename, command in (
            ("tree.json", node_command("ls", "--all", "--package-lock-only", "--json")),
            ("sbom.json", node_command("sbom", "--package-lock-only", "--sbom-format=cyclonedx")),
        ):
            data = load_json(run(command, ROOT / project))
            (directory / filename).write_text(json.dumps(data, indent=2))
        if report["metadata"]["dependencies"]["total"] != len(lock["packages"]) - 1:
            raise ValueError("npm audit omitted part of the locked graph")
        findings += npm_findings(bind_npm_ranges(report, lock), lock, project, lock_hash)
        inventory.append(
            {"project": project, "lock_sha256": lock_hash, "metadata": report["metadata"]}
        )
    for project in projects:
        lock_path = ROOT / project / "uv.lock"
        raw = (
            lock_path.read_bytes()
        )  # Missing lockfiles fail; no floating project silently omitted.
        lock = tomllib.loads(raw.decode())
        lock_hash = hashlib.sha256(raw).hexdigest()
        directory = output / project.replace("/", "-")
        directory.mkdir(parents=True, exist_ok=True)
        config = tomllib.loads((ROOT / project / "pyproject.toml").read_text())
        packages = python_packages(lock, config)
        integrity = registry_integrity(lock, registry_cache)
        (directory / "registry-integrity.json").write_text(json.dumps(integrity, indent=2))
        run(["uv", "lock", "--project", project, "--check"], ROOT)
        for label, extra in (("all", ["--all-groups"]), ("runtime", ["--no-dev"])):
            data = load_json(
                run(
                    [
                        "uv",
                        "export",
                        "--project",
                        project,
                        "--frozen",
                        "--all-extras",
                        "--no-emit-project",
                        "--format",
                        "cyclonedx1.5",
                        *extra,
                    ],
                    ROOT,
                )
            )
            (directory / f"{label}-sbom.json").write_text(json.dumps(data, indent=2))
        runtime = {(p["name"], p["version"]) for p in data["components"]}
        direct = {
            re.split(r"[\[<>=!~; ]", d, maxsplit=1)[0].lower().replace("_", "-")
            for d in config["project"]["dependencies"]
        }
        # Audit every universal lock entry, including alternate Python/platform
        # versions. A requirements export would discard non-host marker branches.
        reports = []
        by_name = {}
        for name, version in sorted(packages):
            by_name.setdefault(name, []).append(version)
        for index in range(max(map(len, by_name.values()))):
            entries = [
                (name, versions[index])
                for name, versions in by_name.items()
                if len(versions) > index
            ]
            requirements = directory / f"locked-{index}.txt"
            requirements.write_text(
                "\n".join(f"{name}=={version}" for name, version in entries) + "\n"
            )
            report = load_json(
                run(
                    [
                        sys.executable,
                        "-m",
                        "pip_audit",
                        "--requirement",
                        str(requirements),
                        "--no-deps",
                        "--disable-pip",
                        "--strict",
                        "--progress-spinner",
                        "off",
                        "--format",
                        "json",
                    ],
                    ROOT,
                    (0, 1),
                )
            )
            reports.extend(report["dependencies"])
        report = {"dependencies": reports, "fixes": {}}
        (directory / "audit.json").write_text(json.dumps(report, indent=2))
        findings += python_findings(report, project, lock_hash, packages, runtime, direct)
        inventory.append({"project": project, "lock_sha256": lock_hash, "packages": len(packages)})
    if not projects or not inventory:
        raise ValueError("Empty dependency inventory")
    blocked = enforce(
        findings, json.loads((ROOT / "security/exceptions.json").read_text()), output, inventory
    )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as handle:
            handle.write((output / "summary.md").read_text())
    return blocked


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
    ):
        raise SystemExit(
            "Dependency audit failed closed: invalid/missing evidence or scanner failure"
        ) from None
