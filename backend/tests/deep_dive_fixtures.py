"""Venue-HTTP and provider boundary fixtures for Market Deep Dive tests.

Only the public CLOB/Gamma HTTP transport and the LLM provider are replaced. The
real context builder, rule parser, book model, fee code, claim validation,
decision policy, forecast calibration and persistence run unchanged.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx

CID = "0x" + "ab" * 32
YES, NO = "11", "22"
RULES = (
    "This market resolves to Yes if Chainlink (BTC/USD) is at least $100,000.00 "
    "on 2026-12-31 23:59:00 UTC."
)
SOURCE_A = "https://news-a.example/btc-outlook"
SOURCE_B = "https://wire-b.example/markets/btc"
EXCERPT_A = "Analysts expect the price feed to stay near the threshold into year end."
EXCERPT_B = "Exchange outflows suggest holders are not selling ahead of the deadline."


def clob_market(**overrides):
    market = {
        "condition_id": CID,
        "question": "Will BTC be at least $100,000?",
        "description": RULES,
        "end_date_iso": "2026-12-31T23:59:00Z",
        "resolutionSource": "Chainlink",
        "active": True,
        "closed": False,
        "accepting_orders": True,
        "neg_risk": False,
        "minimum_tick_size": "0.01",
        "minimum_order_size": "1",
        # Deliberately reversed order: labels, not position, define the sides.
        "tokens": [
            {"outcome": "No", "token_id": NO, "price": "0.4"},
            {"outcome": "Yes", "token_id": YES, "price": "0.6"},
        ],
    }
    market.update(overrides)
    return market


def gamma_market(**overrides):
    market = {
        "conditionId": CID,
        "active": True,
        "closed": False,
        "enableOrderBook": True,
        "feesEnabled": False,
        "outcomes": ["Yes", "No"],
        "clobTokenIds": [YES, NO],
        "events": [{"id": "777"}],
    }
    market.update(overrides)
    return market


def raw_book(token, *, age_seconds=1, asks=None, bids=None):
    from py_clob_client_v2.utilities import (
        generate_orderbook_summary_hash,
        parse_raw_orderbook_summary,
    )

    at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    raw = {
        "market": CID,
        "asset_id": token,
        "timestamp": str(int(at.timestamp() * 1000)),
        "bids": bids or [{"price": "0.58", "size": "50"}],
        "asks": asks or [{"price": "0.60", "size": "20"}, {"price": "0.62", "size": "30"}],
        "tick_size": "0.01",
        "min_order_size": "1",
        "neg_risk": False,
        "last_trade_price": "0.6",
        "hash": "",
    }
    raw["hash"] = generate_orderbook_summary_hash(parse_raw_orderbook_summary(raw))
    return raw


class Venue:
    """Mutable public-venue fixture served through an httpx MockTransport."""

    def __init__(self):
        self.clob = clob_market()
        self.gamma = [gamma_market()]
        self.book_age = 1
        self.asks = None
        self.bids = None
        self.fail_gamma = False
        self.fail_books = False
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if request.url.path.endswith("/book"):
            if self.fail_books:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json=raw_book(
                    request.url.params["token_id"],
                    age_seconds=self.book_age,
                    asks=self.asks,
                    bids=self.bids,
                ),
            )
        if "gamma" in request.url.host:
            if self.fail_gamma:
                return httpx.Response(500)
            return httpx.Response(200, json=self.gamma)
        return httpx.Response(200, json=self.clob)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def research_context(*pairs):
    """The existing bounded, source-delimited search-excerpt format."""
    return "== Tavily Research ==\n" + "\n".join(
        f"• Headline\n  {excerpt}\n  Source: {url}" for url, excerpt in pairs
    )


def draft(*claims, **extra):
    body = {
        "summary": "Two independent sources describe a market near its threshold.",
        "key_insights": ["Price sits near the threshold."],
        "claims": list(claims),
        "risks": ["Oracle feed gaps near the cutoff."],
        "invalidation_conditions": ["A rule clarification changes the threshold."],
        "missing_information": ["Cutoff-day volatility."],
        **extra,
    }
    return json.dumps(body)


def claim(url, quote, *, direction="supports", pivotal=True, statement=None):
    return {
        "statement": statement or "A sourced statement about the market.",
        "direction": direction,
        "source_url": url,
        "quote": quote,
        "pivotal": pivotal,
        "uncertainty": None,
    }


GOOD_CLAIMS = (
    claim(SOURCE_A, EXCERPT_A, direction="supports"),
    claim(SOURCE_B, EXCERPT_B, direction="contradicts"),
)
GOOD_CONTEXT = research_context((SOURCE_A, EXCERPT_A), (SOURCE_B, EXCERPT_B))


class FakeClient:
    """Provider boundary: the user-selected analysis client."""

    def __init__(self, analysis="", context="", metadata=None, error=None, delay=0.0):
        self.analysis, self.context, self.error, self.delay = analysis, context, error, delay
        self.metadata = (
            {"research_contract": "market-research-v1", "provider": "fake", "model": "fake-1"}
            if metadata is None
            else metadata
        )
        self.closed = False
        self.calls = []

    async def analyze_market(self, **kwargs):
        import asyncio

        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return {
            "analysis": self.analysis,
            "research_context": self.context,
            "metadata": self.metadata,
        }

    async def close(self):
        self.closed = True
