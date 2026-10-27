# Issue #34

The first pure-engine step has been extended with account-locked SQL persistence,
shared journal reservations/fills/cancellations, captured-source wiring, immutable
legacy sidecars, API/UI reporting, fixed-horizon mark evidence and PostgreSQL/browser
regressions. See [the lifecycle guide and acceptance matrix](maker-lifecycle.md).

The native adapter deliberately abstains from fills when source aggressor/continuity
is unverified. Positive modeled fills are tested with controlled attributed-flow
fixtures, not represented as actual native or exchange-confirmed executions.
