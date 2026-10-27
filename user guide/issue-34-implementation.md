# Issue #34 implementation status

Branch: `feat/maker-event-lifecycle`, based on merged #98/#99.

The existing intent service already stops generation-time phantom fills. It is
still the production default and is unchanged by this first implementation step.

## Implemented first step

`paper_maker_lifecycle.py` is a deterministic, side-effect-free replay engine.
Versioned contracts carry quote eligibility/expiry, snapshot provenance, explicit
queue and fee assumptions, cumulative quantity and lifecycle state. Attributed
opposing flow can yield partial modeled fill proposals at the resting limit.
Each print has one shared volume budget. Missing assumptions abstain, gaps freeze
eligibility, and changed duplicate IDs reject replay. Serialized replay state
preserves deduplication across process restarts. Cancellation uses a conservative
receipt-time boundary. Outputs are low-confidence modeled proposals; they do not
create trades or mutate cash, inventory, orders, signers or gateways.

## Remaining before a PR is ready

- Persist typed orders and processed evidence atomically, with owner/account locks,
  uniqueness constraints, migrations and PostgreSQL concurrency/crash checks.
- Connect authoritative #18 snapshot/rule/tick/lot validation, source-event
  adapters, latency, subscription gaps and terminal resolution. Native prints
  without proven aggressor attribution must remain ineligible.
- Reserve cash/shares and book accepted proposals through #27's shared durable
  journal. Its persistent integration is still pending outside main; do not
  replace it with another maker-specific cash ledger.
- Update quote reconciliation to preserve active orders and create new IDs after
  terminal cancellation/reprice; never resurrect terminal lifecycle state.
- Add the disabled-by-default rollout flag, shadow comparison and telemetry.
- Add account-scoped lifecycle API, UI, legacy-unverified report exclusion and
  adverse-selection evidence at fixed later horizons.
- Add real API/SQL/browser E2E, PostgreSQL race tests, no-signing regressions,
  operator/screenshot guide and full acceptance matrix; run complete CI.

This step does not complete #34 and must not be enabled or represented as an
exchange-confirmed execution model. No merge-ready PR has been opened yet.
