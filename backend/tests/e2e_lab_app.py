"""Event replay laboratory API + SQLite for the browser test. No order path exists."""

import e2e_book_app as fixture
from event_replay_fixtures import book, settlement, signal

from app.api.routes.trading.strategy_lab import router
from app.config import get_settings

app = fixture.app
app.include_router(router)
get_settings().event_replay_lab_enabled = True


@app.post("/__test/recipe")
async def recipe(known_fees: bool = True):
    await fixture.reset()
    events = [
        book("b1", 0, [("0.48", 500)], [("0.50", 500)]),
        signal("s1", 1, source_pnl="5000"),
        book("b2", 2, [("0.58", 500)], [("0.60", 500)]),
        settlement("f", 100, "1"),
    ]
    config = {"funding": "1000", "seed": 3, "decision_latency_ms": 1500}
    if known_fees:
        config.update(taker_fee_bps="100", fee_schedule_id="fee-v1")
    return {
        "name": "browser run",
        "events": [e.model_dump(mode="json") for e in events],
        "config": config,
        "strategy": {"name": "follow_signals", "follower_notional_usdc": "50"},
        "baseline_tokens": ["tok-yes"],
    }
