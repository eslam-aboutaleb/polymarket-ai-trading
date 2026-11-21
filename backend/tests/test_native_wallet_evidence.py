"""Real v2 HTTP parser: bounds, explicit funding, pagination and immutable raw receipts."""

import hashlib
import json
import time
from datetime import timedelta

import httpx
import pytest
from test_live_account_reconciliation import T0

from app.services.analytics.attribution_flows import external_flows
from app.services.analytics.native_wallet_evidence import wallet_evidence

WALLET = "0x" + "1" * 40


def test_v2_discovers_external_tokens_preserves_raw_and_explicitly_requests_funding():
    requests = []

    def provider(request):
        requests.append(request)
        assert request.url.host == "data-api.polymarket.com"
        assert "authorization" not in request.headers
        rows = []
        if request.url.path == "/v2/positions" and request.url.params["status"] == "OPEN":
            rows = [{"proxy_wallet": WALLET, "token_id": "999", "current_size": "0.000001"}]
        if request.url.path == "/v2/activity":
            assert request.url.params["exclude_deposits_withdrawals"] == "false"
            rows = [
                {
                    "proxy_wallet": WALLET,
                    "transaction_hash": "deposit",
                    "type": "DEPOSIT",
                    "timestamp": int(T0.timestamp()) + 1,
                    "usdc_size": "10.000001",
                }
            ]
        return httpx.Response(200, json={"data": rows, "pagination": {"next_cursor": None}})

    end = (T0 + timedelta(seconds=10)).isoformat()
    result = wallet_evidence(
        WALLET,
        start=T0.isoformat(),
        end=end,
        deadline=time.monotonic() + 10,
        transport=httpx.MockTransport(provider),
    )
    assert result["positions"] == {"999": "0.000001"}
    assert len(requests) == 4
    for receipt in result["receipts"]:
        assert hashlib.sha256(receipt["body"].encode()).hexdigest() == receipt["sha256"]
    report = external_flows(
        {"observed_at": T0.isoformat()}, {"observed_at": end, "wallet_evidence": result}, []
    )
    assert str(report["funding_cashflow"]) == "10.000001"
    assert report["other_income"] == 0 and report["unattributed_rebates"] == 0


@pytest.mark.parametrize(
    "fault", ["cursor", "v1", "owner", "too_large", "negative", "http", "truncated"]
)
def test_malformed_or_incomplete_provider_data_never_claims_complete_discovery(fault):
    def provider(request):
        body = {"data": [], "pagination": {"next_cursor": None}}
        if fault == "truncated":
            body["pagination"]["has_more"] = True
        elif fault == "cursor":
            body["pagination"]["next_cursor"] = "repeated-cursor"
        elif fault == "v1":
            body = []
        elif fault in {"owner", "negative"}:
            body["data"] = [
                {
                    "proxy_wallet": "0x" + "2" * 40 if fault == "owner" else WALLET,
                    "token_id": "11",
                    "current_size": "-1" if fault == "negative" else "1",
                }
            ]
        elif fault == "too_large":
            return httpx.Response(200, content=b" " * 262145)
        if fault == "http":
            return httpx.Response(429)
        return httpx.Response(200, content=json.dumps(body).encode())

    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        wallet_evidence(
            WALLET, deadline=time.monotonic() + 10, transport=httpx.MockTransport(provider)
        )


def test_ambiguous_activity_is_not_double_counted_or_credited_to_profit():
    item = {
        "transaction_hash": "same",
        "timestamp": int(T0.timestamp()) + 1,
        "type": "MAKER_REBATE",
        "usdc_size": "100",
    }
    ending = (T0 + timedelta(seconds=10)).isoformat()
    result = external_flows(
        {"observed_at": T0.isoformat()},
        {
            "observed_at": ending,
            "wallet_evidence": {
                "activity_from": T0.isoformat(),
                "activity_to": ending,
                "activity": [item, item],
            },
        },
        [],
    )
    assert result["unattributed_rebates"] is None
    assert "ACTIVITY_IDENTITY_AMBIGUOUS" in result["reasons"]
