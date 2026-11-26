"""Issue #23: maker economics. Exact Decimal fixtures; real SQLite for the adapter. No mocks."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from book_research_fixtures import CID, NOW
from book_research_fixtures import research_db as _research_db

from app.models import MarketMakerConfig, PaperMakerFill, PaperMakerMark, PaperMakerOrder
from app.services.analytics import maker_economics as me
from app.services.analytics.maker_economics import Fill, Quote, Rebate
from app.services.analytics.maker_economics_paper import economics_for_config
from app.services.trading.paper_maker_lifecycle import MakerOrder

research_db = _research_db
D = Decimal
T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def fill(fid="f1", price="0.48", shares="10", source="venue_confirmed", **kw):
    base = {
        "fill_id": fid,
        "occurred_at": T0,
        "price": D(price),
        "shares": D(shares),
        "source": source,
        "fee": D("0"),
        "mid_at_fill": D("0.50"),
        "mid_after": {1: D("0.50"), 10: D("0.50"), 60: D("0.50")},
    }
    base.update(kw)
    return Fill(**base)


def quote(qid="q1", side="BUY", fills=(), **kw):
    base = {
        "quote_id": qid,
        "token_id": "yes",
        "condition_id": "c1",
        "side": side,
        "limit_price": D("0.48"),
        "shares": D("10"),
        "final_state": "filled",
        "evidence_age_seconds": D("1"),
        "fills": tuple(fills),
        "settlement_payout": D("1"),
        "holding_cost": D("0"),
        "rebate": Rebate("not_applicable"),
    }
    base.update(kw)
    return Quote(**base)


def run(quotes, **kw):
    return me.analyze(quotes, as_of=T0, **kw)


def test_apparent_spread_profit_with_negative_net_settlement_pnl():
    # Bought at 0.48 against a 0.50 mid (looks like +0.04 effective spread) then the
    # market resolved NO: spread was "captured" but the position lost its whole cost.
    q = quote(fills=[fill()], settlement_payout=D("0"))
    out = run([q])
    assert D(out["venue_confirmed"]["effective_spread_fill_weighted"]) == D("0.04")
    assert out["realized"]["pnl_usdc"] == "-4.80"
    assert out["realized"]["quotes_counted"] == 1


def test_winning_settlement_pnl_and_exit_before_settlement():
    won = run([quote(fills=[fill()], settlement_payout=D("1"))])
    assert won["realized"]["pnl_usdc"] == "5.20"
    sold = run([quote(fills=[fill()], settlement_payout=None, exit_cashflow=D("4.90"))])
    assert sold["realized"]["pnl_usdc"] == "0.10"


def test_sell_quote_short_inventory_settles_against_the_maker():
    q = quote(side="SELL", fills=[fill(price="0.52")], settlement_payout=D("1"))
    assert run([q])["realized"]["pnl_usdc"] == "-4.80"  # sold at .52, token paid 1


def test_rebate_not_applicable_or_unconfirmed_counts_zero_and_confirmed_counts():
    base = {"fills": [fill()], "settlement_payout": D("1")}
    na = run([quote(rebate=Rebate("not_applicable", D("9")), **base)])
    un = run([quote(rebate=Rebate("unconfirmed", D("9")), **base)])
    ok = run([quote(rebate=Rebate("confirmed", D("0.07")), **base)])
    assert na["realized"]["pnl_usdc"] == un["realized"]["pnl_usdc"] == "5.20"
    assert ok["realized"]["pnl_usdc"] == "5.27"
    assert na["rebates"]["counted"] == "0" and ok["rebates"]["counted"] == "0.07"
    assert na["rebates"]["unconfirmed_or_not_applicable_quotes"] == 1


def test_partial_fill_counts_only_filled_shares():
    q = quote(final_state="partial", fills=[fill(shares="4")], settlement_payout=D("1"))
    out = run([q])
    assert out["realized"]["pnl_usdc"] == "2.08"  # 4 * (1 - 0.48)
    assert out["venue_confirmed"]["shares"] == "4"


def test_cancel_race_fill_is_counted_and_its_markout_loss_reported():
    racing = fill(after_cancel_request=True, mid_after={1: D("0.47"), 10: D("0.46"), 60: D("0.45")})
    out = run(
        [quote(final_state="cancelled", shares=D("12"), fills=[fill("f0", shares="2"), racing])]
    )
    stats = out["venue_confirmed"]
    assert stats["cancel_race_fills"] == 1
    assert stats["cancel_race_markout_loss"] == "-0.30"  # 10 * (0.45 - 0.48)


def test_adverse_selection_rate_and_markouts_by_horizon():
    bad = fill("b", mid_after={1: D("0.47"), 10: D("0.46"), 60: D("0.44")})
    good = fill("g", mid_after={1: D("0.49"), 10: D("0.52"), 60: D("0.55")})
    out = run([quote(fills=[bad, good], shares=D("20"))])
    stats = out["venue_confirmed"]
    assert stats["adverse_selection_rate"] == "0.5" and stats["adverse_selection_samples"] == 2
    assert stats["markouts"]["10"] == {"mean_per_share": "0.01", "samples": 2}
    assert stats["markouts"]["60"]["mean_per_share"] == "0.015"


def test_stale_or_missing_evidence_abstains_not_zero():
    for age in (D("31"), None):
        out = run([quote(fills=[fill()], evidence_age_seconds=age)])
        assert out["realized"]["pnl_usdc"] is None
        assert "STALE_OR_MISSING_EVIDENCE" in out["realized"]["abstain_reasons"]


def test_unknown_fee_holding_cost_and_unresolved_inventory_abstain():
    out = run([quote(fills=[fill(fee=None)], holding_cost=None, settlement_payout=None)])
    reasons = out["realized"]["abstain_reasons"]
    assert {"UNKNOWN_MAKER_FEE", "UNKNOWN_HOLDING_COST", "UNRESOLVED_INVENTORY"} <= set(reasons)
    assert out["realized"]["unrealized_inventory_shares"] == "10"
    assert out["realized"]["pnl_usdc"] is None


def test_modeled_fills_are_never_realized():
    out = run([quote(fills=[fill(source="modeled")])])
    assert out["realized"]["pnl_usdc"] is None and out["realized"]["quotes_counted"] == 0
    assert out["venue_confirmed"]["fills"] == 0
    assert out["counterfactual_modeled"]["fills"] == 1
    assert "never realized" in out["counterfactual_modeled"]["label"]


def test_unfilled_quotes_are_nonfills_with_no_assumed_queue_value():
    out = run([quote("a", final_state="cancelled"), quote("b", final_state="expired")])
    assert out["nonfills"]["quotes"] == 2 and out["nonfills"]["quoted_notional_usdc"] == "9.60"
    assert out["nonfills"]["missed_fill_value"] is None
    assert out["realized"]["pnl_usdc"] is None
    assert "none" in out["queue_assumption"]


def test_fills_without_mid_are_excluded_from_spread_not_zeroed():
    out = run([quote(fills=[fill(mid_at_fill=None)])])
    assert out["venue_confirmed"]["effective_spread_fill_weighted"] is None
    assert out["venue_confirmed"]["fills_without_mid"] == 1


def test_kill_switch_recommendation_needs_confirmed_samples_then_follows_markout():
    toxic = [
        quote(f"q{i}", fills=[fill(f"f{i}", mid_after={1: D("0.47"), 10: D("0.40"), 60: D("0.4")})])
        for i in range(3)
    ]
    assert run(toxic)["kill_switch_recommendation"] == "INSUFFICIENT_CONFIRMED_FILLS"
    assert (
        run(toxic, min_fills_for_recommendation=3)["kill_switch_recommendation"] == "PAUSE_QUOTING"
    )
    calm = [quote(f"q{i}", fills=[fill(f"f{i}")]) for i in range(3)]
    assert (
        run(calm, min_fills_for_recommendation=3)["kill_switch_recommendation"] == "CONTINUE_SHADOW"
    )


def test_cost_stress_removes_the_edge():
    q = quote(fills=[fill()], settlement_payout=D("1"), holding_cost=D("0"))
    assert D(run([q])["realized"]["pnl_usdc"]) > 0
    stressed = quote(fills=[fill()], settlement_payout=D("1"), holding_cost=D("6"))
    assert D(run([stressed])["realized"]["pnl_usdc"]) < 0


def test_stationary_market_has_no_fills_and_no_economics():
    out = run([quote(final_state="resting")])
    assert out["venue_confirmed"]["fills"] == 0 and out["realized"]["pnl_usdc"] is None


@pytest.mark.parametrize(
    "bad",
    [
        quote(limit_price=D("1")),
        quote(shares=D("0")),
        quote(fills=[fill(shares="11")]),
        quote(fills=[fill(price="0")]),
    ],
)
def test_structurally_invalid_input_is_rejected(bad):
    with pytest.raises(me.MakerEconomicsError):
        run([bad])


def test_naive_as_of_is_rejected():
    with pytest.raises(me.MakerEconomicsError):
        me.analyze([], as_of=datetime(2026, 1, 1))


def test_report_is_deterministic_read_only_and_audited():
    q = quote(fills=[fill()])
    a, b = run([q], source_ids=("s1",)), run([q], source_ids=("s1",))
    assert a == b and a["report_hash"] == b["report_hash"]
    assert a["execution_eligible"] is False
    assert a["evidence_status"] == "implemented_detector_not_validated"
    assert a["condition_ids"] == ["c1"] and a["token_ids"] == ["yes"]
    assert a["as_of"].startswith("2026-11-25T12:00:00") and a["source_ids"] == ["s1"]


def test_fee_table_has_source_url_effective_date_and_maker_vs_taker():
    table = me.FEE_TABLE
    assert all(u.startswith("https://docs.polymarket.com/") for u in table["source_urls"])
    assert table["effective_from"] == "2026-11-20" and table["retrieved_at"] == "2026-11-25"
    cats = table["categories"]
    assert all(c["maker"] == "0" for c in cats.values())
    assert cats["crypto_btc_5m_15m_updown"]["taker"] == "0.05"
    assert (
        cats["geopolitics"]["rebate_eligible"] is False
        and cats["combos"]["rebate_eligible"] is False
    )


# ---- adapter over the real paper journal tables ----


def test_paper_adapter_reports_modeled_fills_as_counterfactual_only(research_db):
    db = research_db
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
    order = MakerOrder(
        id="o1",
        token_id="11",
        condition_id=CID,
        side="BUY",
        limit_price=D("0.40"),
        shares=D("10"),
        active_from=NOW,
        expires_at=NOW + timedelta(minutes=5),
        source_snapshot_id="snap",
        cumulative_filled=D("4"),
        state="PARTIAL",
    )
    db.add(
        PaperMakerOrder(
            id="o1",
            config_id=1,
            user_id=1,
            account_id=1,
            quote_key="k",
            source_snapshot_id="snap",
            metadata_snapshot_id="snap",
            payload_json=order.model_dump_json(),
            state="PARTIAL",
            shares=D("10"),
            limit_price=D("0.40"),
            cumulative_filled=D("4"),
        )
    )
    db.add(
        PaperMakerFill(
            id="f1",
            order_id="o1",
            event_id="e1",
            fill_sequence=0,
            realized_pnl_usdc="0",
            payload_json=json.dumps({"shares": "4", "price": "0.40", "fee_usdc": "0"}),
            observed_at=NOW,
        )
    )
    db.add(
        PaperMakerMark(
            fill_id="f1",
            horizon_seconds=30,
            payload_json=json.dumps({"markout_per_share": "-0.02"}),
        )
    )
    db.add(
        PaperMakerMark(
            fill_id="f1", horizon_seconds=5, payload_json=json.dumps({"markout_per_share": None})
        )
    )
    db.commit()
    out = economics_for_config(db, config)
    assert out["horizons_seconds"] == [5, 30, 60]
    assert out["venue_confirmed"]["fills"] == 0 and out["realized"]["pnl_usdc"] is None
    modeled = out["counterfactual_modeled"]
    assert modeled["fills"] == 1 and modeled["shares"] == "4"
    assert modeled["markouts"]["30"] == {"mean_per_share": "-0.02", "samples": 1}
    assert modeled["markouts"]["5"]["samples"] == 0  # unavailable mark is not zero
    assert modeled["effective_spread_fill_weighted"] is None


def test_economics_route_is_ownership_checked(research_db):
    from fastapi import HTTPException

    from app.api.routes.trading import market_maker as routes

    db = research_db
    db.add(
        MarketMakerConfig(
            id=1,
            user_id=1,
            account_id=1,
            enabled=True,
            condition_id=CID,
            token_id_yes="11",
            token_id_no="22",
        )
    )
    db.commit()
    mine = SimpleNamespace(id=1, owner_user_id=1, is_default=False)
    assert routes.maker_economics(1, account=mine, db=db)["schema_version"] == me.SKILL_VERSION
    other = SimpleNamespace(id=1, owner_user_id=2, is_default=False)
    with pytest.raises(HTTPException) as err:
        routes.maker_economics(1, account=other, db=db)
    assert err.value.status_code == 404
