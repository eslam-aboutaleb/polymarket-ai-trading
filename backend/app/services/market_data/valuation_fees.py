"""Read-only taker-cost upper bound from archived market-specific fee parameters.

Formula: https://docs.polymarket.com/trading/fees . Five-decimal upward
rounding is deliberately conservative; it is not a claim about actual match
rounding, rebates, fees paid or exchange fills. Legacy base_fee alone is not
sufficient to identify the modern price-dependent schedule.
"""

import hashlib
import json
from decimal import ROUND_CEILING, Decimal, localcontext

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT


def archived_taker_fee(metadata, curve):
    clob, gamma = metadata["clob"], metadata["gamma"]
    if gamma.get("feesEnabled") is False or clob.get("fees_enabled") is False:
        if clob.get("taker_base_fee", 0) != 0 or gamma.get("feeSchedule", {}).get("rate", 0) != 0:
            raise ValueError("ABSTAIN_CONTRADICTORY_FEE_METADATA")
        return Decimal(0), "venue-zero-fee-v1"
    schedule = gamma.get("feeSchedule") or {}
    if (
        gamma.get("feesEnabled") is not True
        or gamma.get("feeType") != "crypto_fees_v2"
        or gamma.get("version") != "v1"
        or schedule.get("exponent") != 1
        or schedule.get("takerOnly") is not True
    ):
        raise ValueError("ABSTAIN_UNSUPPORTED_OR_UNKNOWN_FEE_CURVE")
    rate = Decimal(str(schedule.get("rate")))
    if not rate.is_finite() or not 0 <= rate <= 1:
        raise ValueError("ABSTAIN_INVALID_FEE_RATE")
    with localcontext(ACCOUNTING_CONTEXT):
        fee = Decimal(0)
        for level in curve:
            price, shares = Decimal(level["price"]), Decimal(level["shares"])
            if not 0 < price < 1 or shares <= 0:
                raise ValueError("ABSTAIN_INVALID_FEE_DEPTH")
            fee += shares * rate * price * (1 - price)
        bound = fee.quantize(Decimal(".00001"), rounding=ROUND_CEILING)
    digest = hashlib.sha256(
        json.dumps(
            {"exponent": 1, "rate": str(rate), "taker_only": True},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return bound, "gamma-crypto-fees-v2-ceiling5-" + digest[:16]
