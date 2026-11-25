# Durable redemption evidence

Issue #56 is a P0 transaction safety dependency. Trading settings and the paper/live selector are unchanged. The existing server-owned live-execution barrier remains authoritative. This feature does not authorize funded signing or broadcasting.

Open `/portfolio/redemptions` and enter a binary CTF condition ID to save a preview. Preview reads finalized public chain evidence; it never loads signing credentials, reads a nonce, reserves a nonce, broadcasts, or changes balances or settlement accounting. The displayed payout is an estimate of entitlement, not received cash. Save the evidence download to retain the exact snapshot and its hash.

The production API accepts preview creation and read-only receipt reconciliation only. There is no submission endpoint or submit button. Internal submission code exists for fault-injected testing and remains behind the existing release barrier.

| State | Meaning |
| --- | --- |
| Preview / not broadcast | Immutable entitlement snapshot; no signed transaction. |
| Unknown | Broadcast outcome cannot be verified; a saved signed hash does not prove node acceptance. Never blindly retry. |
| Submitted | Matching transaction hash recorded; payout remains unknown until finality. |
| Confirmed | Matching chain, transaction identity, canonical finalized receipt, token movements and actual collateral payout verified. |
| Failed | Preflight failure or finalized canonical revert; reason distinguishes them. |
| Reorged | Previously observed finality is invalidated; verified payout projection is withdrawn and user execution requires recovery review. |

## Evidence and recovery

Account/idempotency-key uniqueness freezes execution mode and entitlement. A preview cannot be promoted to submission. A wallet-wide durable lease excludes concurrent intents. An unsigned transaction and nonce are saved before signing, and the locally calculated signed hash is committed before the first send. Signed raw bytes and private keys are never stored in intents or events.

A crash or ambiguous send retains the identity for reconciliation. Background recovery observes at most three records within a 15-second budget per account; it cannot sign or resend. Missing, replaced, inconsistent or unsupported evidence stays unverified. A stranded nonce or lease requires operator investigation; there is no automatic reset or replacement transaction.

Receipt proofs are append-only and content-addressed. A finalized event is emitted once, including after reorg and recovery. Reconciliation changes evidence projections only; existing attribution and ledger owners retain financial recognition. Legacy attempts without an intent remain `legacy_unverified`; no historical execution provenance is fabricated.

## Supported protocol and trust boundary

The adapter supports standard binary CTF entitlement, including fractional/void payouts from on-chain numerators and denominator. Unresolved or invalid vectors abstain. It derives collection and position identifiers through contract calls, rather than guessing from market price or close time. Frozen quantities must still match before signing, so externally redeemed holdings invalidate the request.

Pinned contracts, chain 137, collateral, calldata, signer, nonce and transaction value are validated. Execution is limited to direct EOA custody; smart/proxy/deposit wallets, negative-risk routing and newer router position systems require separately verified adapters and manual handling. A configured RPC is an evidence trust boundary; contradictory or unavailable finality never establishes a payout.

Protocol references: [position management](https://docs.polymarket.com/trading/positions/manage) and [contract addresses](https://docs.polymarket.com/resources/contracts), checked November 22, 2026.

## Migration and validation

Migration `20261122_0043_redemption` adds intent, wallet lease and observation tables plus a nullable link on legacy attempts. It preserves existing users, accounts, attempts and settings. Empty-schema downgrade is tested. Once intents exist, downgrade refuses deletion of evidence; roll back application code while retaining the additive schema.

Tests use synthetic funded wallets, real local Ethereum signing and intercepted JSON-RPC. They cover durable-before-send ordering, duplicate requests and PostgreSQL worker races, ambiguous timeout, restart recovery, account isolation, release barriers, finality, canonical reverts/reorgs, malformed receipt identity, missing payout, void/unresolved outcomes and externally redeemed inventory. Browser tests exercise the real API and disposable PostgreSQL schema. No funded on-chain transaction is used for verification.

## Replaced transactions

If a submitted intent never gets a receipt, it stays `unknown` while the wallet nonce is still free (the transaction may be pending). When the wallet's nonce at the finalized block is greater than the intent's nonce and no receipt exists for the intent's hash, a different transaction owns that nonce: the intent becomes `replaced` (reason `REDEMPTION_NONCE_CONSUMED_BY_DIFFERENT_TRANSACTION`). Nothing is rebroadcast, no cash is recognized, no finalized event is written, and the wallet lease is released so an operator can review and create a new intent with a new idempotency key.
