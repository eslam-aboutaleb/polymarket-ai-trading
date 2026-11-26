# Maker quote policy v1 (issue #8)

Status: **shadow / paper only. Not validated. No profitability claim.**

`app/services/trading/market_making/quote_policy.py` is a pure function from
market state to a versioned `QuotePlan`. It never signs, submits or cancels.

## Behaviour
- Reservation price `mid - risk_coefficient * inventory` (long YES lowers both quotes).
- Half-spread widens by `vol_widening * realized_sigma` (sigma = std of recent midpoint changes; unknown sigma is flagged `VOL_UNKNOWN`, not zero).
- Quotes never cross the midpoint, so skew cannot make a `post_only` quote marketable.
- A side that would add inventory past `max_inventory_shares` is dropped.
- Missing or older-than-`maker_policy_max_book_age_seconds` book: no quotes (`STALE_BOOK`).
- Worst-case exposure = old resting notional + new quotes, capped at `max_collateral`.
- Rebates count only when the caller marks them confirmed (none are today).
- Every plan carries `plan_hash`, `version` and the full inputs, so it can be reconstructed.

## Enabling
`MAKER_QUOTE_POLICY_V1=true` (default off). Only the paper path uses it; the live
path keeps the band baseline. `GET /api/market-maker/configs/{id}/quote-plan`
returns the shadow plan without persisting anything.

## Not done
Out-of-sample comparison against the band baseline needs real maker fills that do not
exist yet (modeled fills only). See #23 for measurement and #156 for the live pilot.
