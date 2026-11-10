# Dependency security

The full-lock `Dependency security` job is required by `Quality gate`. It complements the pinned shared security workflow: dependency review protects PR deltas, CodeQL checks source, Gitleaks scans secrets, and this job audits the complete committed package graph, including pre-existing advisories. Scanner failures, unavailable advisory services, missing locks, incomplete inventories, unknown approvals and expired exceptions fail closed.

## Current evidence

Fresh baseline on **2026-11-10**, main `5d2f42c`: npm reported 22 vulnerable dependency findings (17 high, 3 moderate, 2 low). The backend audit reported 13 affected Python packages. `baseline.json` retains original advisory identities, package versions, dependency paths, scopes, fixes and lock hashes. Counts include native npm meta-advisories; they are not a count of independent exploits.

After remediation, the Node 20.20.2 / npm 11.9.0 full-lock audit reports **zero high/critical and zero moderate**, with four low dependency findings arising from one underlying `deepmerge` advisory (`GHSA-gcqx-w48m-w2h4`, version 4.3.1, development build graph). It remains visible/informational under the documented threshold. All locked registry packages in backend, LLM service, CLI service and the scanner toolchain have **zero known Python advisories**. There are **no active security exceptions**.

A passing advisory scan is not proof of absence of vulnerabilities, malicious code or supply-chain compromise. Registry signatures and available provenance are checked separately with native `npm audit signatures`; lock integrity and allowed registry origins are enforced. Review update source/changelogs and dependency paths before merging, including MCP SDK/OAuth dependencies. Remote tool descriptions are data, never executable installation commands or authority to read wallet keys. Only approved existing MCP/server origins may be used; this change introduces no remote MCP integration.

## Policy and artifacts

- Node: high/critical (and unknown severities) block in runtime **and development**. Lower advisories remain visible. Native `npm audit`, tree and CycloneDX SBOM cover every physical lock entry; explicit includes prevent omit configuration from hiding dev/optional/peer dependencies.
- Python: **every advisory blocks**, because pip-audit does not supply a dependable severity field. The audit never assumes an unknown severity is low, and unknown-severity findings cannot be excepted. Every universal `uv.lock` registry package/version is scanned, including alternate Python/platform versions and optional provider extras. Dev-only scope is determined from the frozen runtime SBOM; both scopes remain scanned.
- Source dependencies and arbitrary local/editable plugins are rejected. Only the owning virtual/editable project is excluded from registry scanning; its code remains subject to source review/CodeQL. New project manifests require committed locks. Python artifact hashes, npm SRI and registry origins are validated before scanning.
- `uv 0.13.5` exports frozen CycloneDX graphs; `pip-audit 2.10.0` has its own committed tool lock. Application packages are not installed or executed by the advisory scanner. Signature verification installs the frontend with lifecycle scripts disabled. CI duplication tooling is now in the audited frontend lock instead of a floating `npx` installation.
- The CLI service now has a lockfile. Service Dockerfiles consume frozen exports with hashes, and backend container commands do not reinstall development groups or re-resolve dependencies at startup.
- Job artifacts retain audit JSON, inventories, original lock hashes, full/runtime SBOMs, native signature results, toolchain versions and effective main rules. The job summary reports blocked, allowed-with-expiry, informational, new/regressed and resolved baseline entries. Artifacts contain no production credentials; scanner/provider error bodies are not logged.

Run locally from the repository root with Node 20 and npm 11.9.0:

```sh
uv sync --project tools/dependency-security --frozen
python -m unittest discover -s tools/dependency-security/tests -v
uv run --project tools/dependency-security --frozen python tools/dependency-security/audit.py
npm ci --prefix frontend --ignore-scripts
npm audit signatures --prefix frontend --json
```

Evidence is written to ignored `security-artifacts/`. A network/provider failure is a failed audit, never permission to skip or use yesterday's clean result.

## Compatible remediation

The wallet provider is upgraded through its supported upstream release. Zod 3 has no patched release for oversized-collection exhaustion. **Do not alias to `zod/v3`**, even inside a nominally patched Zod 4 package: its legacy parsers still traverse oversized input. The app uses patched v4 parsers with a narrow `record(value)` compatibility signature for the existing wallet schemas. Regression tests import the actual Coinbase EIP-712 and AppKit frame schemas and prove max-constrained arrays/sets reject before evaluating elements. The real SDK project/relay/QR canary was also exercised without pairing or signing a wallet.

Glob/minimatch overrides are scoped to the affected major ranges. Legacy minimatch 3 consumers retain their callable API. Glob 14 and minimatch 11 are tested together, and every production build verifies the generated service worker actually precaches its JS/CSS assets. A clean version audit alone would not catch the empty-precache regression found during remediation.

Python JOSE's unpatched algorithm-confusion and ECDSA dependency findings are removed by using maintained PyJWT. The existing server-controlled algorithm allowlist, access/refresh claims, wallet-signature flow, cookie handling and HTTP 401 behavior are preserved and regression-tested. Other Python updates target patched compatible releases, not forced bulk upgrades.

## Reviewed exceptions

`exceptions.json` is deliberately empty. A future exception must match the exact project, ecosystem, package, version, advisory, installed path, runtime/dev scope, severity **and lock hash**. It also requires a named owner and independent approver, technical reachability evidence, justification, compensating control, tracking issue, review date and expiry within 30 days. Mere absence of known exploitation is not justification. Changed paths, new/worsened advisories, expired approvals and unused/stale entries fail. A lock update invalidates the affected project's old exceptions and requires re-review.

Do not fill in another person's approval. CODEOWNERS assigns exception, policy, workflow and dependency-manifest/lock changes to the repository owner. Native required code-owner review must enforce this; the JSON's reviewer field alone is not evidence of human approval. Synthetic approvals in negative-control tests are fixtures only. Upstream fixes should remove exceptions rather than extend them automatically.

## Administrator setup — required for acceptance

The existing ruleset audit found `Protected Production Branch` active but targeting **no branches**, with **no required status checks** and code-owner review disabled. Effective rules for main were empty. The integration can read rules but GitHub returned **HTTP 403** when attempting the authorized update. Workflow success cannot establish branch protection.

An administrator must update [ruleset 25528587](https://github.com/eslam-aboutaleb/polymarket-ai-trading/rules/25528587):

1. Include the default branch (`~DEFAULT_BRANCH` / main), retaining other intended targets.
2. Require `Quality gate` from the **GitHub Actions app (15368)** and require the branch to be up to date.
3. Require code-owner review and dismiss stale approvals when new commits are pushed; retain the deletion/non-fast-forward protections and existing bypass policy.

`required-main-rules.json` is the concrete proposal based on the observed existing ruleset. Compare it with current settings before applying; do not overwrite concurrent administrator changes. `protection.py` reads **effective** main rules and fails the CI job until the required checks, ownership policy and stale-approval dismissal are actually enforced. Once configured, rerun the failed job. Policy and lock changes then require owner review; bots cannot self-authorize risk exceptions.

The implementation must not be reported as meeting this administrator-controlled acceptance criterion until that effective-rule verification passes. No live trading setting or signing authorization changes as part of this task.

## Primary references

- [npm audit and signatures](https://docs.npmjs.com/cli/v11/commands/npm-audit)
- [npm SBOM](https://docs.npmjs.com/cli/v11/commands/npm-sbom)
- [uv export](https://docs.astral.sh/uv/reference/cli/#uv-export)
- [pip-audit](https://github.com/pypa/pip-audit)
- [GitHub required status checks](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
- [Zod oversized-collection fix](https://github.com/colinhacks/zod/commit/a0ebf970f2d752e5f58527cd2942ff48be4f46bd)
