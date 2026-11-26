"""Issue #8: inventory-aware, volatility-aware maker quote policy.

Pure-policy tests use exact Decimal inputs. Wiring tests use a real SQLite
session with real ``PaperMakerOrder`` rows; nothing is mocked.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from book_research_fixtures import CID, NOW
from book_research_fixtures import research_db as _research_db

from app.config import get_settings
from app.models import MarketMakerConfig, PaperMakerOrder
from app.services.trading import market_maker_service as service
from app.services.trading.market_making.quote_policy import (
    POLICY_VERSION,
    MarketState,
    PolicyParams,
    build_quote_plan,
)
from app.services.trading.paper_maker_lifecycle import MakerOrder

research_db = _research_db
D = Decimal


def params(**changes):
    base = {
        "min_spread": D("0.02"),
        "max_spread": D("0.06"),
        "num_bands": 3,
        "band_order_size": D("10"),
        "max_collateral": D("500"),
    }
    base.update(changes)
    return PolicyParams(**base)


def state(**changes):
    base = {"midpoint": D("0.50"), "book_age_seconds": D("1"), "realized_sigma": D("0")}
    base.update(changes)
    return MarketState(**base)


def sides(plan, side):
    return sorted(o["price"] for o in plan.orders if o["side"] == side)


def test_zero_inventory_is_symmetric_around_midpoint():
    plan = build_quote_plan(params(), state(), "yes")
    bids, asks = sides(plan, "BUY"), sides(plan, "SELL")
    assert len(bids) == len(asks) == 3
    assert [round(0.5 - b, 4) for b in sorted(bids, reverse=True)] == [
        round(a - 0.5, 4) for a in asks
    ]
    assert "INVENTORY_SKEW" not in plan.reason_codes
    assert plan.version == POLICY_VERSION and plan.shadow_only


def test_long_inventory_lowers_both_sides_and_short_raises_them():
    flat = build_quote_plan(params(), state(), "yes")
    long_ = build_quote_plan(params(), state(inventory_shares=D("40")), "yes")
    short = build_quote_plan(params(), state(inventory_shares=D("-40")), "yes")
    assert max(sides(long_, "BUY")) < max(sides(flat, "BUY"))
    assert min(sides(long_, "SELL")) <= min(sides(flat, "SELL"))
    assert max(sides(short, "BUY")) >= max(sides(flat, "BUY"))
    assert min(sides(short, "SELL")) > min(sides(flat, "SELL"))
    assert "INVENTORY_SKEW" in long_.reason_codes


def test_skew_never_crosses_the_midpoint():
    plan = build_quote_plan(
        params(risk_coefficient=D("0.01")), state(inventory_shares=D("150")), "yes"
    )
    for order in plan.orders:
        if order["side"] == "SELL":
            assert order["price"] > 0.5
        else:
            assert order["price"] < 0.5


def test_inventory_cap_stops_the_side_that_adds_inventory():
    long_ = build_quote_plan(
        params(max_inventory_shares=D("50")), state(inventory_shares=D("50")), "yes"
    )
    assert not sides(long_, "BUY") and sides(long_, "SELL")
    assert "INVENTORY_CAP_LONG" in long_.reason_codes
    short = build_quote_plan(
        params(max_inventory_shares=D("50")), state(inventory_shares=D("-50")), "yes"
    )
    assert not sides(short, "SELL") and sides(short, "BUY")
    assert "INVENTORY_CAP_SHORT" in short.reason_codes


def test_volatility_widens_every_band_and_unknown_volatility_is_flagged():
    calm = build_quote_plan(params(), state(realized_sigma=D("0")), "yes")
    wild = build_quote_plan(params(), state(realized_sigma=D("0.03")), "yes")
    assert min(sides(wild, "SELL")) > min(sides(calm, "SELL"))
    assert max(sides(wild, "BUY")) < max(sides(calm, "BUY"))
    assert "VOL_WIDENED" in wild.reason_codes
    unknown = build_quote_plan(params(), state(realized_sigma=None), "yes")
    assert "VOL_UNKNOWN" in unknown.reason_codes


@pytest.mark.parametrize("age", [None, D("11")])
def test_stale_or_unknown_book_age_disables_quoting(age):
    plan = build_quote_plan(params(), state(book_age_seconds=age), "yes")
    assert plan.abstained and plan.orders == [] and plan.reason_codes == ["STALE_BOOK"]


def test_missing_midpoint_abstains():
    plan = build_quote_plan(params(), state(midpoint=None), "yes")
    assert plan.abstained and plan.reason_codes == ["NO_MIDPOINT"]


def test_worst_case_exposure_counts_old_and_new_orders():
    plan = build_quote_plan(params(max_collateral=D("100")), state(resting_notional=D("70")), "yes")
    assert D(plan.worst_case_exposure) <= D("100")
    assert sum(o["size"] for o in plan.orders) == 30
    assert "EXPOSURE_TRIMMED" in plan.reason_codes
    full = build_quote_plan(
        params(max_collateral=D("100")), state(resting_notional=D("100")), "yes"
    )
    assert full.orders == [] and "NO_QUOTES" in full.reason_codes


def test_rebate_counts_only_when_confirmed_eligible():
    base = build_quote_plan(params(rebate_per_share=D("0.001")), state(), "yes")
    assert not base.rebate_included
    confirmed = build_quote_plan(
        params(rebate_per_share=D("0.001"), rebate_confirmed=True), state(), "yes"
    )
    assert confirmed.rebate_included
    assert D(confirmed.expected_net_spread) == D(base.expected_net_spread) + D("0.002")


def test_plan_is_deterministic_versioned_and_reconstructable():
    a = build_quote_plan(params(), state(inventory_shares=D("5")), "yes")
    b = build_quote_plan(params(), state(inventory_shares=D("5")), "yes")
    c = build_quote_plan(params(), state(inventory_shares=D("6")), "yes")
    assert a.plan_hash == b.plan_hash != c.plan_hash
    assert a.inputs["params"]["risk_coefficient"] == "0.0005"
    assert a.inputs["state"]["inventory_shares"] == "5"
    assert all(o["policy"] == POLICY_VERSION for o in a.orders)


def test_price_bounds_prune_quotes():
    plan = build_quote_plan(params(min_price=D("0.48"), max_price=D("0.52")), state(), "yes")
    assert all(0.48 <= o["price"] <= 0.52 for o in plan.orders)


# ---- wiring against a real database ----


def _config(db):
    config = MarketMakerConfig(
        id=1,
        user_id=1,
        account_id=1,
        enabled=True,
        condition_id=CID,
        token_id_yes="11",
        token_id_no="22",
    )
    db.add(config)
    db.commit()
    return config


def _order(db, side, token, filled, shares="10", state_="RESTING"):
    order = MakerOrder(
        id=f"o-{side}-{token}-{filled}",
        token_id=token,
        condition_id=CID,
        side=side,
        limit_price=D("0.40"),
        shares=D(shares),
        active_from=NOW,
        expires_at=NOW + timedelta(minutes=5),
        source_snapshot_id="snap",
        cumulative_filled=D(filled),
        state=state_,
    )
    db.add(
        PaperMakerOrder(
            id=order.id,
            config_id=1,
            user_id=1,
            account_id=1,
            quote_key=order.id,
            source_snapshot_id="snap",
            metadata_snapshot_id="snap",
            payload_json=order.model_dump_json(),
            state=state_,
            shares=order.shares,
            limit_price=order.limit_price,
            cumulative_filled=order.cumulative_filled,
        )
    )
    db.commit()


def test_paper_inventory_is_net_yes_shares(research_db):
    config = _config(research_db)
    _order(research_db, "BUY", "11", "6")
    _order(research_db, "SELL", "11", "2")
    _order(research_db, "BUY", "22", "1")  # buying NO is short YES
    assert service.paper_inventory(research_db, config) == D("3")


def test_build_plan_for_config_uses_paper_state_and_book_age(research_db):
    config = _config(research_db)
    _order(research_db, "BUY", "11", "0", shares="10", state_="RESTING")
    book = {"midpoint": 0.5, "age_seconds": 1.0}
    plan = service.build_plan_for_config(research_db, config, book, record=False)
    assert D(plan.inputs["state"]["resting_notional"]) == D("4.0")
    stale = service.build_plan_for_config(research_db, config, {"midpoint": 0.5}, record=False)
    assert stale.abstained and stale.reason_codes == ["STALE_BOOK"]


def test_realized_sigma_needs_history_and_is_never_zero_when_unknown():
    service._midpoint_history.clear()
    assert service._record_midpoint(9, 0.50) is None
    assert service._record_midpoint(9, 0.52) is None
    sigma = service._record_midpoint(9, 0.50)
    assert sigma is not None and sigma > 0


def test_book_age_from_clob_timestamp_milliseconds():
    assert service._book_age_seconds("1700000000000", now=1700000004.0) == 4.0
    assert service._book_age_seconds(None) is None
    assert service._book_age_seconds("bad") is None


def test_flag_defaults_off_so_band_baseline_is_unchanged():
    assert get_settings().maker_quote_policy_v1 is False


def test_quote_plan_endpoint_reads_a_real_http_book_and_enforces_ownership(
    research_db, monkeypatch
):
    import asyncio
    import json
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api.routes.trading import market_maker as routes

    body = json.dumps(
        {
            "timestamp": str(int((time.time() - 1) * 1000)),
            "bids": [{"price": "0.49", "size": "100"}],
            "asks": [{"price": "0.51", "size": "100"}],
        }
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(service, "POLYMARKET_CLOB_API", f"http://127.0.0.1:{server.server_port}")
    _config(research_db)
    account = SimpleNamespace(id=1, owner_user_id=1, is_default=False)
    try:
        plan = asyncio.run(routes.maker_quote_plan(1, account=account, db=research_db))
        assert plan["version"] == POLICY_VERSION and plan["orders"] and not plan["abstained"]
        other = SimpleNamespace(id=1, owner_user_id=2, is_default=False)
        with pytest.raises(HTTPException) as err:
            asyncio.run(routes.maker_quote_plan(1, account=other, db=research_db))
        assert err.value.status_code == 404
    finally:
        server.shutdown()
