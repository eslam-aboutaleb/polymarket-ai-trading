# Financial skills adapter (issue #28)

Read-only, hash-pinned research skills behind the research MCP tool
`financial_skill` (schema via `describe_financial_skill`).

- Off by default. `FINANCIAL_SKILLS_ENABLED=1` turns it on; it is read on every
  call, so unsetting it is an immediate kill switch (`status: "disabled"`).
- Allowlist: skills must be in `manifest.lock.json` and in the adapter's code
  allowlist. The skill source sha256 is re-checked before every run.
- Fails closed with a reason code: `UNKNOWN_SKILL`, `SKILL_VERSION_MISMATCH`,
  `INPUT_SCHEMA_MISMATCH`, `OUTPUT_SCHEMA_MISMATCH`, `SKILL_HASH_MISMATCH`,
  `SKILL_TIMEOUT`, `INPUT_TOO_LARGE`, `MANIFEST_NOT_READ_ONLY`.
- Always `execution_eligible: false`. No network, credentials, signing or orders.
- Evidence class is `descriptive_unvalidated`; the skill never emits a probability.
- Changing a skill: edit it, then run `python -m financial_skills.lock` from this
  directory and commit the new `manifest.lock.json` (a test checks it).
- Client entry: `ResearchMCPClient.financial_skill_evidence(...)`.
