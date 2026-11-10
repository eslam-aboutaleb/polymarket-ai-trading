"""Verify effective server-side rules; a green workflow cannot establish these."""

import json
import os
import re
import subprocess
from pathlib import Path


def validate(rules):
    if not isinstance(rules, list):
        raise ValueError("Invalid GitHub rules response")
    checks = [r["parameters"] for r in rules if r.get("type") == "required_status_checks"]
    if not any(
        p.get("strict_required_status_checks_policy")
        and any(
            c.get("context") == "Quality gate" and c.get("integration_id") == 15368
            for c in p.get("required_status_checks", [])
        )
        for p in checks
    ):
        raise ValueError(
            "Main does not require the GitHub Actions Quality gate on the current base"
        )
    reviews = [r["parameters"] for r in rules if r.get("type") == "pull_request"]
    if not any(
        p.get("require_code_owner_review") and p.get("dismiss_stale_reviews_on_push")
        for p in reviews
    ):
        raise ValueError(
            "Final-content CODEOWNERS review and stale-approval dismissal are not enforced"
        )


def main():
    repository = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository identity")
    result = subprocess.run(  # noqa: S603 - fixed gh command; validated repository, no shell
        ["gh", "api", f"repos/{repository}/rules/branches/main"],  # noqa: S607 - runner-owned gh
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    rules = json.loads(result.stdout)
    directory = Path("security-artifacts")
    directory.mkdir(exist_ok=True)
    (directory / "effective-main-rules.json").write_text(json.dumps(rules, indent=2))
    validate(rules)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as handle:
            handle.write(
                "\nEffective main rules require the Actions Quality gate "
                "and security policy CODEOWNERS review.\n"
            )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        raise SystemExit(
            "Branch policy verification failed closed; inspect effective-main-rules.json "
            "or repository rules permissions"
        ) from None
