# Binary contract valuation — binary-valuation-v1

## Objective and applicability

Research-only Decimal cashflow arithmetic and a read-side bridge to recorded rule, forecast, calibration, quote, fee and ownership evidence. `execution_eligible` is always false. No wallet keys, order submissions, risk overrides or strategy enablement are provided.

The pure `value_binary_contract` function evaluates explicitly hypothetical numerical assumptions. The authenticated `POST /api/valuation/binary` endpoint accepts evidence identifiers, never caller-approved probabilities, rule statuses, VWAPs, inventory or Kelly caps. `GET /api/valuation/schema` and the Research MCP descriptor publish the same versioned request schema. `BINARY_VALUATION_ENABLED` defaults off.

## Invocation

Select `condition_id`, `token_id`, `outcome`, `shares`, timezone-aware `as_of`, recorded `rules_hash` and `forecast_id`. SELL also requires an owned `account_id`. Optional limit price and explicit redemption, adverse-fill and funding-cost assumptions are allowed. Retrieve real IDs from existing recorded evidence; invented IDs abstain.

The Research MCP tools are `binary_contract_valuation` and `describe_binary_contract_valuation`. The schema includes exact Decimal bounds and rejects unknown approval fields.

## Evidence and algorithm

1. Load the immutable rule version available at the cutoff. Verify the token/outcome mapping, fixed binary one-USDC payout and matching archived metadata. Unsupported, amended, disputed and void rules abstain.
2. Load the recorded forecast, enforce its condition/time and private trade ownership. Calibrate from the same model/task's earlier resolved public scan forecasts in a fixed ten-bin frequency estimator. Require 20 distinct conditions, one sample per condition, with labels available strictly before the forecast. Publish a Wilson interval, sample count, training cutoff and content hash. The interval assumes binomial sampling; correlation and regime shifts can invalidate that interpretation. It is not individual-event calibration approval.
3. Reuse the native immutable book archive and existing binary-book depth walk. Verify checksum, identity, freshness, tick, share precision, full requested depth and rule consistency. BUY uses asks; SELL uses bids and server-read confirmed filled inventory. Unknown/pending inventory, foreign accounts and future observations abstain.
4. Use archived market-specific Gamma fee parameters. Zero fees require explicit noncontradictory evidence. Supported v1 `crypto_fees_v2` parameters use the documented price-dependent curve at every depth level with conservative five-decimal upward rounding. Unknown schedules abstain; actual fees paid and rebates are never inferred.
5. Compute YES/NO gross/net EV, break-even probability, interval lower bound and fee/probability sensitivities. Funding and redemption assumptions remain visible. Kelly stays zero because no independently approved allocation parameter is present.

The numerical example p=.60 and ask=.55 gives gross +.05/share; .06/share costs make net -.01/share. SELL uses proceeds minus retained outcome value and costs, with ownership checked separately. VWAP may lie between venue ticks; individual book/limit prices must obey the actual tick.

## Evidence, limitations and migration

`shadow-baseline.json` links the recreated public-source report and its frozen benchmark, market comparison, counterexamples, costs and exclusions. This is an implemented detector and small forward smoke study, not production-approved predictive evidence. No actual historical bot forecasts or fills were reconstructed as facts. Human review and independent replication remain prerequisites for future promotion.

Monitor abstention reasons, unknown fee regimes, missing calibration, book staleness, amendments and source/inventory conflicts. Disable the feature flag to roll back the analytical tool. Existing signing, execution, account caps and emergency halt remain authoritative.

The Decimal primitive and typed schemas can migrate through a versioned `financial-skills` adapter; Polymarket metadata, native archive, calibration and ownership joins remain in this application. No such external package is required by this implementation.

References: [market-specific fee metadata](https://docs.polymarket.com/market-data/market-details), [fee formula and precision](https://docs.polymarket.com/trading/fees), existing `binary_book.py`, `book_research.py`, `calibration_service.py` and `kelly_service.py`. This tool creates no signing client and does not approve the existing bot's probability forecasts.
