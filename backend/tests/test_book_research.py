from datetime import timedelta
from unittest.mock import patch

from book_research_fixtures import CID, NOW
from book_research_fixtures import research_db as _research_db
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.markets.microstructure import router
from app.models import Account
from app.services.market_data.book_research import (
    ResearchQuery,
    load_analysis,
    replay_research,
    save_research,
)
from app.services.market_data.venue_observations import append_event
from app.utils.database import get_db

research_db = _research_db


def query(**changes):
    return ResearchQuery(
        **{
            "condition_id": CID,
            "as_of": NOW,
            "sizes": ["20"],
            "fee_bps": "100",
            "fee_version": "test-flat",
            **changes,
        }
    )


def test_native_http_checksum_depth_sorting_and_frozen_replay(research_db):
    db = research_db
    with (
        patch("py_clob_client.client.ClobClient.create_order", side_effect=AssertionError("sign")),
        patch("py_clob_client.client.ClobClient.post_order", side_effect=AssertionError("post")),
        patch(
            "py_clob_client_v2.client.ClobClient.create_order", side_effect=AssertionError("sign")
        ),
        patch("py_clob_client_v2.client.ClobClient.post_order", side_effect=AssertionError("post")),
    ):
        result, _ = load_analysis(db, query())
        assert result["books"]["YES"]["grade"] == "VERIFIED_BOOK", result
        assert result["estimates"][0]["vwap"] == "0.55"
        assert result["estimates"][0]["net_cash"] == "11.1100"
        assert result["estimates"][0]["measured_price_impact"] is None
        saved = save_research(db, query(), db.get(Account, 1))
        db.commit()
        from app.models.analytics.book_research_run import BookResearchRun

        row = db.get(BookResearchRun, saved["id"])
        assert replay_research(db, row) == saved
        append_event(
            db,
            {
                "event_type": "tick_size_change",
                "asset_id": "11",
                "market": CID,
                "timestamp": str(int((NOW + timedelta(seconds=1)).timestamp() * 1000)),
            },
            received_at=NOW + timedelta(seconds=1),
            connection_id="one",
        )
        db.commit()
        assert replay_research(db, row) == saved
        assert (
            load_analysis(db, query(as_of=NOW + timedelta(seconds=1)))[0]["estimates"][0]["status"]
            == "abstain"
        )


def test_private_recipes_cannot_cross_accounts(research_db):
    db = research_db
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    with TestClient(app) as client:
        response = client.post("/api/microstructure/runs", json=query().model_dump(mode="json"))
        assert response.status_code == 200, response.text
        identity = response.json()["id"]
        assert client.get(f"/api/microstructure/runs/{identity}").status_code == 200
        assert (
            client.get(
                f"/api/microstructure/runs/{identity}", headers={"X-Account-Id": "2"}
            ).status_code
            == 404
        )
        for account in ["3", "999"]:
            assert (
                client.get(
                    f"/api/microstructure/runs/{identity}", headers={"X-Account-Id": account}
                ).status_code
                == 403
            )
        assert (
            client.get(
                f"/api/microstructure/runs/{identity}", headers={"X-Account-Id": "bad"}
            ).status_code
            == 400
        )


def test_source_gap_unknown_sequence_and_no_history_abstain(research_db):
    db = research_db
    result, _ = load_analysis(db, query(as_of=NOW - timedelta(seconds=1)))
    assert result["reason"] == "NO_ASOF_MARKET_METADATA"
    append_event(
        db,
        {
            "event_type": "price_change",
            "asset_id": "11",
            "market": CID,
            "timestamp": str(int((NOW + timedelta(seconds=1)).timestamp() * 1000)),
            "price_changes": [{"asset_id": "11", "price": "0.50", "size": "0", "side": "SELL"}],
        },
        received_at=NOW + timedelta(seconds=1),
        connection_id="one",
    )
    result, _ = load_analysis(db, query(as_of=NOW + timedelta(seconds=1)))
    assert result["books"]["YES"]["grade"] == "PARTIAL"
    assert result["books"]["YES"]["needs_resnapshot"]
    assert result["estimates"][0]["vwap"] is None


def test_inspected_capture_ids_do_not_drift_and_tampering_rejects(research_db):
    from app.models import BookResearchRun, VenueObservation

    db = research_db
    result, _ = load_analysis(db, query())
    exact = query(capture_ids=result["capture_ids"])
    # Later commit, same receipt cutoff: a query could change, selected IDs cannot.
    from book_research_fixtures import raw_book

    raw = raw_book("11")
    raw["hash"] = "invalid"
    append_event(db, {**raw, "event_type": "book"}, received_at=NOW, connection_id="other")
    db.commit()
    saved = save_research(db, exact, db.get(Account, 1))
    assert saved["result"] == result
    db.commit()
    row = db.get(BookResearchRun, saved["id"])
    assert replay_research(db, row) == saved
    source = db.get(VenueObservation, result["capture_ids"][0])
    source.payload_json = "{}"
    db.commit()
    import pytest

    with pytest.raises(ValueError):
        replay_research(db, row)


def test_orchestration_context_is_server_owned_and_read_only(research_db, monkeypatch):
    from app.api.routes.analysis.orchestration import _with_book_evidence
    from app.config import get_settings

    payload = {"condition_id": CID, "captured_microstructure": {"grade": "fake"}}
    result = _with_book_evidence(research_db, payload)
    assert result["captured_microstructure"]["books"]["YES"]["grade"] == "VERIFIED_BOOK"
    monkeypatch.setattr(get_settings(), "binary_book_research_enabled", False)
    assert "captured_microstructure" not in _with_book_evidence(research_db, payload)


def test_resnapshot_jobs_are_opt_in_bounded_and_cancellable(research_db, monkeypatch):
    import asyncio

    from app.services.market_data import book_resnapshot
    from app.services.market_data.clob_ws_manager import ClobWsManager

    calls = []

    async def refresh(condition):
        calls.append(condition)
        await asyncio.Event().wait()

    monkeypatch.setattr(book_resnapshot, "_resnapshot", refresh)

    async def run():
        manager = ClobWsManager()
        await manager.subscribe(["11"])
        event = {"event_type": "price_change", "market": CID, "asset_id": "11"}
        for _ in range(10):
            book_resnapshot.schedule_resnapshot(manager, event)
        await asyncio.sleep(0)
        assert calls == [CID]
        await manager.stop()
        assert not manager._resnapshot_tasks

    asyncio.run(run())


def test_shadow_first_level_baseline_is_not_a_size_aware_execution(research_db, monkeypatch):
    import asyncio

    import httpx
    from book_research_fixtures import raw_book

    from app.services.trading.market_maker_service import _fetch_order_book

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw_book("11"))),
            **kwargs,
        ),
    )
    baseline = asyncio.run(_fetch_order_book("11"))
    result, _ = load_analysis(research_db, query())
    assert str(baseline["best_ask"]) == "0.6"
    assert result["top_of_book"]["YES"]["best_ask"] == "0.50"
    assert result["estimates"][0]["vwap"] == "0.55"
    assert result["estimates"][0]["measured_price_impact"] is None


def test_existing_settled_execution_comparison_is_read_only_and_not_causal_impact(research_db):
    from app.models import BookResearchRun, LiveOrder, UserTrade
    from app.services.analytics.book_execution_benchmark import compare_execution

    db = research_db
    saved = save_research(db, query(sizes=["5"]), db.get(Account, 1))
    trade = UserTrade(
        user_id=1,
        account_id=1,
        market_id=CID,
        token_id="11",
        action="buy",
        amount=2.6,
        filled_price=0.52,
        filled_size=5,
        price=0.52,
        status="executed",
        order_hash="fixture-order",
        executed_at=NOW + timedelta(seconds=2),
    )
    db.add(trade)
    db.flush()
    order = LiveOrder(
        user_id=1,
        account_id=1,
        trade_id=trade.id,
        idempotency_key="fixture",
        fingerprint="f" * 64,
        strategy="manual",
        token_id="11",
        side="BUY",
        price="0.52",
        shares=5,
        notional="2.6",
        reserved="2.6",
        state="FILLED",
        order_hash="fixture-order",
        response={"settled": True, "size_matched": "5", "filled_notional": 2.6},
        created_at=NOW + timedelta(seconds=1),
    )
    db.add(order)
    db.commit()
    result = compare_execution(
        db, db.get(BookResearchRun, saved["id"]), trade.id, db.get(Account, 1)
    )
    assert result["observed_vwap_difference"] == "0.02"
    assert result["measured_price_impact"] is None and result["comparable_sample_count"] == 1
    assert db.query(LiveOrder).count() == 1 and order.state == "FILLED"
    import pytest

    with pytest.raises(LookupError):
        compare_execution(db, db.get(BookResearchRun, saved["id"]), trade.id, db.get(Account, 2))
    trade.executed_at = NOW + timedelta(seconds=61)
    assert (
        compare_execution(db, db.get(BookResearchRun, saved["id"]), trade.id, db.get(Account, 1))[
            "reason"
        ]
        == "EXECUTION_OUTSIDE_CAPTURED_QUOTE_WINDOW"
    )
    trade.executed_at = NOW + timedelta(seconds=2)
    order.response = {"settled": False}
    assert (
        compare_execution(db, db.get(BookResearchRun, saved["id"]), trade.id, db.get(Account, 1))[
            "status"
        ]
        == "abstain"
    )


def test_native_rest_decimal_lexemes_are_not_rounded():
    import asyncio

    import httpx

    from app.services.market_data.replay_capture import PublicVenueCapture

    async def read():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=b'{"price":0.400000000000000001}')
            )
        ) as client:
            return await PublicVenueCapture(client).get("https://clob.polymarket.com/book")

    assert asyncio.run(read())["price"] == "0.400000000000000001"
