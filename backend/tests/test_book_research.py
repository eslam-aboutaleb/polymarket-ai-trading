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
