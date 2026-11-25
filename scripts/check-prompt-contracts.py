"""Reject mutation of locked prompt versions relative to a trusted Git baseline."""

# Git uses argument lists without a shell; resolve the supplied ref to a commit SHA first.
# ruff: noqa: S603, S607

import argparse
import subprocess
from pathlib import Path

from prompt_contracts import PromptRegistry
from prompt_contracts.identity.lock import parse_lock_bytes, read_lock_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    PromptRegistry.from_directory(root / "prompts", lock_file="contracts.lock.json").freeze()
    baseline = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{args.baseline_ref}^{{commit}}"],
        cwd=root,
        text=True,
    ).strip()
    path = "prompts/contracts.lock.json"
    exists = subprocess.check_output(
        ["git", "ls-tree", "--name-only", baseline, "--", path],
        cwd=root,
        text=True,
    ).strip()
    if not exists:
        return
    old = parse_lock_bytes(subprocess.check_output(["git", "show", f"{baseline}:{path}"], cwd=root))
    new = read_lock_file(root / path)
    digests = {entry.ref: entry.bundle_digest for entry in new.contracts}
    if any(digests.get(entry.ref) != entry.bundle_digest for entry in old.contracts):
        raise SystemExit("A locked prompt version was removed or modified; create a new version")


if __name__ == "__main__":
    main()
