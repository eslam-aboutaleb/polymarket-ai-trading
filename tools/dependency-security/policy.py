"""Exact advisory exceptions; scanner/metadata errors are never exceptions."""

import hashlib
import json
from datetime import date
from pathlib import Path

SEVERITIES = {"low", "moderate", "high", "critical", "unknown"}
IDENTITY = (
    "project",
    "ecosystem",
    "package",
    "version",
    "advisory",
    "path",
    "scope",
    "severity",
    "lock_sha256",
)
REVIEW = {
    "owner",
    "approved_by",
    "justification",
    "reachability",
    "compensating_control",
    "issue",
    "reviewed_on",
    "expires_on",
}


def fingerprint(finding):
    return hashlib.sha256(
        json.dumps({key: finding[key] for key in IDENTITY}, sort_keys=True).encode()
    ).hexdigest()


def evaluate(findings, manifest, today=None):
    today = today or date.today()
    if set(manifest) != {"schema_version", "exceptions"} or manifest["schema_version"] != 1:
        raise ValueError("Invalid exception manifest")
    approved = {}
    for entry in manifest["exceptions"]:
        if set(entry) != set(IDENTITY) | REVIEW or any(
            not isinstance(v, str) or not v.strip() for v in entry.values()
        ):
            raise ValueError(
                "Exceptions require exact identity and complete ownership/review evidence"
            )
        if entry["severity"] not in SEVERITIES or entry["scope"] not in {"runtime", "development"}:
            raise ValueError("Invalid exception severity/scope")
        if entry["severity"] == "unknown":
            raise ValueError("Unknown severity cannot be risk-excepted")
        reviewed, expires = (
            date.fromisoformat(entry["reviewed_on"]),
            date.fromisoformat(entry["expires_on"]),
        )
        if not reviewed <= today < expires or (expires - reviewed).days > 30:
            raise ValueError("Exception expired, future-dated or exceeds 30 days")
        if not entry["issue"].startswith(
            "https://github.com/eslam-aboutaleb/polymarket-ai-trading/issues/"
        ):
            raise ValueError("Exception needs a repository tracking issue")
        key = fingerprint(entry)
        if key in approved:
            raise ValueError("Duplicate exception")
        approved[key] = entry
    blocked, allowed, informational = [], [], []
    observed = set()
    for finding in findings:
        if finding["severity"] not in SEVERITIES:
            raise ValueError("Unknown scanner severity")
        key = fingerprint(finding)
        observed.add(key)
        if key in approved:
            allowed.append({**finding, "exception": approved[key]})
        elif finding["ecosystem"] == "pypi" or finding["severity"] in {
            "high",
            "critical",
            "unknown",
        }:
            blocked.append(finding)
        else:
            informational.append(finding)
    if set(approved) - observed:
        raise ValueError(
            "Unknown or changed exception: "
            "advisory/version/path/scope/severity/lock no longer matches"
        )
    return {"blocked": blocked, "allowed": allowed, "informational": informational}


def write_report(directory, result, inventory):
    directory = Path(directory)
    baseline_path = Path(__file__).resolve().parents[2] / "security/baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {"findings": []}
    identity = ("project", "ecosystem", "package", "advisory", "path")
    prior = {tuple(f[k] for k in identity): f for f in baseline["findings"]}
    current = {tuple(f[k] for k in identity): f for values in result.values() for f in values}
    rank = {"low": 0, "moderate": 1, "high": 2, "critical": 3, "unknown": 4}
    comparison = {
        "new": [f for k, f in current.items() if k not in prior],
        "regressed": [
            f
            for k, f in current.items()
            if k in prior and rank[f["severity"]] > rank[prior[k]["severity"]]
        ],
        "resolved": [f for k, f in prior.items() if k not in current],
    }
    report = {**result, "inventory": inventory, "baseline_comparison": comparison}
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        "# Full locked dependency audit",
        "",
        f"Blocked: {len(result['blocked'])}; "
        f"allowed with expiry: {len(result['allowed'])}; "
        f"informational: {len(result['informational'])}.",
        "",
        "| Project | Package | Version | Advisory | Severity | Scope | Decision |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines[3:3] = [
        f"New: {len(comparison['new'])}; regressed: {len(comparison['regressed'])}; "
        f"resolved baseline entries: {len(comparison['resolved'])}.",
        "",
    ]
    for decision, findings in result.items():
        for finding in findings:
            lines.append(
                "| "
                + " | ".join(
                    str(finding[k]).replace("|", "\\|")
                    for k in ("project", "package", "version", "advisory", "severity", "scope")
                )
                + f" | {decision} |"
            )
    for finding in result["allowed"]:
        entry = finding["exception"]
        lines += [
            "",
            f"Exception {finding['advisory']}: owner {entry['owner']}; "
            f"reviewer {entry['approved_by']}; expires {entry['expires_on']}; {entry['issue']}",
            entry["reachability"],
            entry["compensating_control"],
        ]
    lines += [
        "",
        "Node high/critical and every Python advisory block unless exactly excepted. "
        "Scanner/service errors always block. Runtime includes optional/platform-specific "
        "locked packages; dev-only packages remain scanned.",
        "",
    ]
    (directory / "summary.md").write_text("\n".join(lines))


def enforce(findings, manifest, directory, inventory=()):
    result = evaluate(findings, manifest)
    write_report(directory, result, inventory)
    return bool(result["blocked"])


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("findings", type=Path)
    parser.add_argument("exceptions", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        sys.exit(
            enforce(
                json.loads(args.findings.read_text()),
                json.loads(args.exceptions.read_text()),
                args.output,
            )
        )
    except (ValueError, KeyError, TypeError, OSError):
        raise SystemExit("Dependency policy failed closed: invalid findings or approval") from None
