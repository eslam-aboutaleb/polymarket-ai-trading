"""Conservative maker quote capture until timestamped flow/queue evidence exists."""

import hashlib
import json
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.trading.market_maker_config import MarketMakerConfig
from app.models.trading.paper_maker_intent import PaperMakerIntent

POLICY = "maker-no-evidence-no-fill-v1"


def sync_intents(db: Session, config: MarketMakerConfig, expected: list[dict]) -> dict:
    # Serialize definition prices/notional explicitly. These are not executable
    # shares or cash reservations, and no market midpoint proves a fill.
    desired = {}
    for quote in expected:
        price = Decimal(str(quote["price"]))
        notional = Decimal(str(quote["size"]))
        if not price.is_finite() or not notional.is_finite() or not 0 < price < 1 or notional <= 0:
            raise ValueError("invalid maker intent price/notional")
        if quote["side"] not in ("BUY", "SELL") or quote["token_id"] not in (
            config.token_id_yes,
            config.token_id_no,
        ):
            raise ValueError("invalid maker intent token/side")
        payload = json.dumps(
            {
                "token_id": quote["token_id"],
                "side": quote["side"],
                "limit_price": str(price.normalize()),
                "notional_usdc": str(notional.normalize()),
                "policy": POLICY,
            },
            sort_keys=True,
        )
        desired[hashlib.sha256(payload.encode()).hexdigest()] = payload
    # The owning config row lock serializes sync/reprice across workers.
    db.query(MarketMakerConfig).filter_by(
        id=config.id, user_id=config.user_id
    ).with_for_update().one()
    rows = db.query(PaperMakerIntent).filter_by(config_id=config.id, user_id=config.user_id).all()
    existing = {row.quote_key: row for row in rows}
    created = 0
    withdrawn = 0
    for key, row in existing.items():
        if key not in desired and row.state != "WITHDRAWN":
            row.state = "WITHDRAWN"
            withdrawn += 1
    for key, payload in desired.items():
        row = existing.get(key)
        if row is None:
            db.add(
                PaperMakerIntent(
                    id=str(uuid4()),
                    config_id=config.id,
                    user_id=config.user_id,
                    account_id=config.account_id,
                    quote_key=key,
                    payload_json=payload,
                    state="UNFILLED_DATA_MISSING",
                )
            )
            created += 1
        else:
            row.state = "UNFILLED_DATA_MISSING"
    return {
        "intent_count": len(desired),
        "intents_created": created,
        "intents_withdrawn": withdrawn,
        "policy": POLICY,
        "fill_status": "unfilled_missing_flow_and_queue_evidence",
    }
