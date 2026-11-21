"""Bounded read-only Data API v2 provenance; indexer observations are not chain finality.

Contract: https://docs.polymarket.com/migrate/data-api-v1-to-v2
Wallet fields: https://docs.polymarket.com/trading/wallet-activity
"""

import hashlib
import json
import re
import time
from datetime import datetime
from decimal import Decimal

import httpx

from app.services.market_data.polymarket_service import POLYMARKET_DATA_API

MAX_BODY = 262144


def _pages(client, path, params, deadline):
    cursor, seen, rows, receipts = None, set(), [], []
    for _ in range(10):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("WALLET_OBSERVATION_DEADLINE_EXCEEDED")
        query = {**params, "limit": 100}
        if cursor is not None:
            query["cursor"] = cursor
        with client.stream(
            "GET", POLYMARKET_DATA_API + path, params=query, timeout=min(5, remaining)
        ) as response:
            response.raise_for_status()
            body = bytearray()
            for part in response.iter_bytes():
                body.extend(part)
                if len(body) > MAX_BODY or time.monotonic() >= deadline:
                    raise ValueError("WALLET_EVIDENCE_BOUND_EXCEEDED")
        value = json.loads(body, parse_float=Decimal)
        if not isinstance(value, dict) or not isinstance(value.get("data"), list):
            raise ValueError("WALLET_V2_ENVELOPE_UNVERIFIED")
        page = value.get("pagination")
        if not isinstance(page, dict) or "next_cursor" not in page:
            raise ValueError("WALLET_PAGINATION_UNVERIFIED")
        if len(value["data"]) > 100:
            raise ValueError("WALLET_EVIDENCE_BOUND_EXCEEDED")
        rows.extend(value["data"])
        receipts.append(
            {
                "sha256": hashlib.sha256(body).hexdigest(),
                "body": body.decode(),
                "path": path,
                "query": query,
            }
        )
        if len(rows) > 500:
            raise ValueError("WALLET_EVIDENCE_BOUND_EXCEEDED")
        cursor = page["next_cursor"]
        if "has_more" in page and page["has_more"] is not (cursor is not None):
            raise ValueError("WALLET_PAGINATION_UNVERIFIED")
        if cursor is None:
            return rows, receipts
        if not isinstance(cursor, str) or len(cursor) > 4096 or cursor in seen:
            raise ValueError("WALLET_PAGINATION_UNVERIFIED")
        seen.add(cursor)
    raise ValueError("WALLET_EVIDENCE_BOUND_EXCEEDED")


def wallet_evidence(wallet, *, start=None, end=None, deadline, transport=None):
    """Call only configured native hosts, without signing keys or authentication headers."""
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", wallet):
        raise ValueError("WALLET_IDENTITY_INVALID")
    tokens, receipts, activity = {}, [], []
    with httpx.Client(transport=transport, follow_redirects=False, trust_env=False) as client:
        for status in ("OPEN", "REDEEMABLE", "REDEEMABLE_LOST"):
            rows, raw = _pages(
                client, "/v2/positions", {"user": wallet, "status": status}, deadline
            )
            receipts.extend(raw)
            for row in rows:
                token, quantity = row.get("token_id"), Decimal(str(row.get("current_size")))
                if (
                    str(row.get("proxy_wallet", "")).lower() != wallet.lower()
                    or not isinstance(token, str)
                    or not token.isdecimal()
                    or len(token) > 78
                    or not quantity.is_finite()
                    or quantity < 0
                ):
                    raise ValueError("WALLET_POSITION_IDENTITY_UNVERIFIED")
                if token in tokens and Decimal(tokens[token]) != quantity:
                    raise ValueError("WALLET_POSITION_CHANGED_DURING_OBSERVATION")
                tokens[token] = str(quantity)
            if len(tokens) > 20:
                raise ValueError("KNOWN_TOKEN_OBSERVATION_BOUND_EXCEEDED")
        if start is not None:
            params = {
                "user": wallet,
                "start": int(datetime.fromisoformat(start).timestamp()),
                "end": int(datetime.fromisoformat(end).timestamp()),
                "exclude_deposits_withdrawals": "false",
            }
            activity, raw = _pages(client, "/v2/activity", params, deadline)
            receipts.extend(raw)
            if any(str(row.get("proxy_wallet", "")).lower() != wallet.lower() for row in activity):
                raise ValueError("WALLET_ACTIVITY_IDENTITY_UNVERIFIED")
    # Keep raw source bodies/checksums for reproduction, never use provider P&L fields.
    return {
        "positions": tokens,
        "activity": activity,
        "activity_from": start,
        "activity_to": end,
        "receipts": receipts,
        "chain_finality": "unverified",
        "provider": "polymarket-data-api-v2",
    }
