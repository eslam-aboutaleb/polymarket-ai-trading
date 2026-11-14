# Dependency security

The full-lock `Dependency security` job is required by `Quality gate`. It complements the pinned shared security workflow: dependency review protects PR deltas, CodeQL checks source, Gitleaks scans secrets, and this job audits the complete committed package graph, including pre-existing advisories. Scanner failures, unavailable advisory services, missing locks, incomplete inventories, unknown approvals and expired exceptions fail closed.

## Current evidence

Fresh baseline on **2026-11-10**, main `5d2f42c`: npm reported 22 vulnerable dependency findings (17 high, 3 moderate, 2 low). The backend audit reported 13 affected Python packages. `baseline.json` retains original advisory identities, package versions, dependency paths, scopes, fixes and lock hashes. Counts include native npm meta-advisories; they are not a count of independent exploits.

The fresh 2026-11-13 audit under Node 24.22.1 / npm 11.9.0 reports **zero high/critical and zero moderate**, with four low dependency findings arising from one underlying `deepmerge` advisory (`GHSA-gcqx-w48m-w2h4`, version 4.3.1, development build graph). It remains visible/informational under the documented threshold. All locked registry packages in backend, LLM service, CLI service and the scanner and SDK build toolchains have **zero known Python advisories**. The exact pinned Git SDK was separately checked against OSV commit advisories and its independently recomputed source tree; no advisory was reported. There are **no active security exceptions**.

GitHub push metadata separately reported 24 open default-branch dependency alerts (2 critical, 6 high, 12 moderate, 4 low). Those describe main, not this PR. The Dependabot alert-detail API returned HTTP 403, so advisory-by-advisory reconciliation remains unavailable to this integration; do not equate that count with the fresh PR lockfile audit. The existing shared `New dependency risk` job passed on this PR.

A passing advisory scan is not proof of absence of vulnerabilities, malicious code or supply-chain compromise. Registry signatures and available provenance are checked separately with native `npm audit signatures`; lock integrity and allowed registry origins are enforced. Review update source/changelogs, SBOM licenses and dependency paths before merging, including MCP SDK/OAuth dependencies. Record any license obligations in the update PR; missing/unclear license metadata needs source/license review rather than assumed permission. Remote tool descriptions are data, never executable installation commands or authority to read wallet keys. Only approved existing MCP/server origins may be used; this change introduces no remote MCP integration. A real stdio-server negative regression proves foreign tool descriptions requesting wallet keys and executable-plugin installation cannot register tools or disclose a synthetic environment key. Foreign package origins and mismatched release hashes are separately rejected by the dependency gate. A future remote transport needs an explicitly reviewed origin/capability allowlist before activation; third-party descriptions cannot authorize tools. TradingView CDP scraping and unattended official-MCP execution remain outside the automated product.

## Policy and artifacts

- Node: high/critical (and unknown severities) block in runtime **and development**. Lower advisories remain visible. Native `npm audit`, tree and CycloneDX SBOM cover every physical lock entry; explicit includes prevent omit configuration from hiding dev/optional/peer dependencies.
- Python: **every advisory blocks**, because pip-audit does not supply a dependable severity field. The audit never assumes an unknown severity is low, and unknown-severity findings cannot be excepted. Every universal `uv.lock` registry package/version is scanned, including alternate Python/platform versions and optional provider extras. Dev-only scope is determined from the frozen runtime SBOM; both scopes remain scanned.
- Arbitrary source dependencies and local/editable plugins are rejected. The sole reviewed source dependency is `llm-gateway-sdk` at the exact repository/tag/commit/tree/archive hash recorded in `sources.json`. The scanner independently recomputes its Git tree and content hashes, verifies upstream tag identity, MIT license and package identity, checks the complete reviewed file inventory, and queries OSV by commit. Missing provenance, moved tags, changed content, service failures and every reported source advisory block. An unsigned commit is not represented as signed; automated source review does not assert human owner approval. Only the owning virtual/editable project is excluded from registry scanning; its code remains subject to source review/CodeQL. New project manifests require committed locks. Python artifact URLs/hashes are matched to the official named-version PyPI release; npm SRI and registry origins are validated before scanning.
- `uv 0.13.5` exports frozen CycloneDX graphs; `pip-audit 2.10.0` has its own committed tool lock. Application packages are not installed or executed by the advisory scanner. Signature verification installs the frontend with lifecycle scripts disabled. CI duplication tooling is now in the audited frontend lock instead of a floating `npx` installation.
- The CLI service now has a lockfile. Service Dockerfiles use frozen `uv sync` and hash-constrained build inputs, and backend container commands do not reinstall development groups or re-resolve dependencies at startup.
- Job artifacts retain audit JSON, inventories, original lock hashes, full/runtime SBOMs, native signature results, toolchain versions and effective main rules. The job summary reports blocked, allowed-with-expiry, informational, new/regressed and resolved baseline entries. Artifacts contain no production credentials; scanner/provider error bodies are not logged.

The requested Node 20 baseline was reproduced before remediation. Build, browser, container and security jobs now use Node 24.22.1: the current optional Coinbase/Solana graph includes packages requiring Node >=22.12.0, which npm silently omits on Node 20. The actual SDK-schema regression must run against installed packages, not skip an absent connector. The frontend engine floor is now Node 22.12.0.

Run locally from the repository root with Node 24.22.1 and npm 11.9.0:

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

## Effective branch protection

On 2026-11-13, the authorized update to [ruleset 25528587](https://github.com/eslam-aboutaleb/polymarket-ai-trading/rules/25528587) succeeded. The effective main rules now target the default branch, require `Quality gate` from GitHub Actions app 15368 with strict up-to-date enforcement, require code-owner review and dismiss stale approvals. Deletion/non-fast-forward protections and the existing bypass policy are retained. `protection.py` verifies the effective rules on every dependency-security run; workflow success alone is insufficient. Native owner review remains authoritative and must not be fabricated or bypassed.

## Source build reproducibility

The SDK review records commit `ebaa064813f7c1b83f5efefdd22a9fc87af5db19` and the complete source-file hashes. Its keyless upstream suite passed 163 tests (one explicitly opt-in live test skipped), with 93.64% coverage. A clean wheel build succeeded with `--require-build-hashes` and the frozen, independently scanned `tools/gateway-sdk-build` graph. Backend, CLI and LLM manifests constrain each SDK build dependency to the exact SHA-256 artifact; registry/runtime dependencies remain in frozen locks. CI builds all three actual container installation paths and imports the SDK; local Docker availability is not assumed.

The 2026-11-13 audit resolved 27 baseline advisory entries, with zero blocked/excepted and one informational advisory identity (`deepmerge` in the frontend development graph). Native npm signature verification returned empty invalid/missing lists. No security exception was added. These observations describe this audited lock set, not future advisory state.

## Primary references

- [npm audit and signatures](https://docs.npmjs.com/cli/v11/commands/npm-audit)
- [npm SBOM](https://docs.npmjs.com/cli/v11/commands/npm-sbom)
- [uv export](https://docs.astral.sh/uv/reference/cli/#uv-export)
- [pip-audit](https://github.com/pypa/pip-audit)
- [GitHub required status checks](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
- [Zod oversized-collection fix](https://github.com/colinhacks/zod/commit/a0ebf970f2d752e5f58527cd2942ff48be4f46bd)
