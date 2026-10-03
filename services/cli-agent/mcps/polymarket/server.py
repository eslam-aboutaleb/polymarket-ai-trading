"""
Polymarket MCP Server
Provides tools for interacting with Polymarket prediction markets.
Implements the MCP (Model Context Protocol) specification.
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any

# stdout is reserved for the MCP JSON-RPC stream; diagnostics must go to stderr.
logging.basicConfig(stream=sys.stderr)
logger = logging.getLogger(__name__)


# MCP protocol messages
def make_response(id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def make_error(id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


class PolymarketMCP:
    """
    MCP Server for Polymarket interactions.
    Provides tools for market data, positions, and trading.
    """

    def __init__(self):
        self.private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        self.chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
        self.client = None
        self._init_client()

    def _init_client(self):
        """Initialize the Polymarket CLOB client."""
        try:
            from py_clob_client.client import ClobClient

            if self.private_key:
                self.client = ClobClient(
                    host="https://clob.polymarket.com", chain_id=self.chain_id, key=self.private_key
                )
            else:
                # Read-only client without private key
                self.client = ClobClient(host="https://clob.polymarket.com", chain_id=self.chain_id)
        except ImportError:
            self.client = None
        except Exception:
            self.client = None

    def get_tools(self) -> list[dict]:
        """Return list of available tools."""
        return [
            {
                "name": "polymarket_get_markets",
                "description": (
                    "Get a list of active Polymarket prediction markets with "
                    "current prices and volume"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of markets to return (default: 20)",
                            "default": 20,
                        },
                        "active": {
                            "type": "boolean",
                            "description": "Only return active markets (default: true)",
                            "default": True,
                        },
                    },
                },
            },
            {
                "name": "polymarket_get_market",
                "description": (
                    "Get detailed information about a specific Polymarket market by ID or slug"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "market_id": {
                            "type": "string",
                            "description": "The market ID or condition ID",
                        }
                    },
                    "required": ["market_id"],
                },
            },
            {
                "name": "polymarket_get_orderbook",
                "description": "Get the order book for a specific market token",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "token_id": {
                            "type": "string",
                            "description": "The token ID (YES or NO token)",
                        }
                    },
                    "required": ["token_id"],
                },
            },
            {
                "name": "polymarket_get_positions",
                "description": "Get current trading positions for the authenticated wallet",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "polymarket_get_price_history",
                "description": "Get price history for a market token",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "token_id": {"type": "string", "description": "The token ID"},
                        "interval": {
                            "type": "string",
                            "description": "Time interval: 1h, 1d, 1w",
                            "default": "1d",
                        },
                    },
                    "required": ["token_id"],
                },
            },
            {
                "name": "polymarket_place_order",
                "description": (
                    "Place a limit order on a Polymarket market (requires authentication)"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "token_id": {"type": "string", "description": "The token ID to trade"},
                        "side": {
                            "type": "string",
                            "enum": ["BUY", "SELL"],
                            "description": "Order side",
                        },
                        "price": {"type": "number", "description": "Limit price (0-1)"},
                        "size": {"type": "number", "description": "Order size in USDC"},
                    },
                    "required": ["token_id", "side", "price", "size"],
                },
            },
        ]

    async def call_tool(self, name: str, arguments: dict) -> Any:
        """Execute a tool and return the result."""
        if not self.client:
            return {"error": "Polymarket client not initialized"}

        try:
            if name == "polymarket_get_markets":
                return await self._get_markets(arguments)
            if name == "polymarket_get_market":
                return await self._get_market(arguments)
            if name == "polymarket_get_orderbook":
                return await self._get_orderbook(arguments)
            if name == "polymarket_get_positions":
                return await self._get_positions()
            if name == "polymarket_get_price_history":
                return await self._get_price_history(arguments)
            if name == "polymarket_place_order":
                return await self._place_order(arguments)
            return {"error": f"Unknown tool: {name}"}
        except Exception as e:
            return {"error": str(e)}

    async def _get_markets(self, args: dict) -> dict:
        """Get list of markets."""
        try:
            limit = args.get("limit", 20)
            # Note: This is a simplified implementation
            # Real implementation would use the actual CLOB API
            markets = self.client.get_markets()
            return {
                "markets": markets[:limit] if isinstance(markets, list) else [],
                "count": min(len(markets) if isinstance(markets, list) else 0, limit),
            }
        except Exception as e:
            return {"error": f"Failed to get markets: {e}"}

    async def _get_market(self, args: dict) -> dict:
        """Get specific market details."""
        try:
            market_id = args["market_id"]
            market = self.client.get_market(market_id)
            return {"market": market}
        except Exception as e:
            return {"error": f"Failed to get market: {e}"}

    async def _get_orderbook(self, args: dict) -> dict:
        """Get order book for a token."""
        try:
            token_id = args["token_id"]
            orderbook = self.client.get_order_book(token_id)
            return {"orderbook": orderbook}
        except Exception as e:
            return {"error": f"Failed to get orderbook: {e}"}

    async def _get_positions(self) -> dict:
        """Get current positions."""
        try:
            if not self.private_key:
                return {"error": "Trading not enabled - no private key"}
            # This would need proper implementation with authenticated client
            return {"positions": [], "note": "Positions API requires full authentication"}
        except Exception as e:
            return {"error": f"Failed to get positions: {e}"}

    async def _get_price_history(self, args: dict) -> dict:
        """Get price history."""
        try:
            token_id = args["token_id"]
            interval = args.get("interval", "1d")
            # Simplified - real implementation would fetch historical data
            return {
                "token_id": token_id,
                "interval": interval,
                "history": [],
                "note": "Price history requires additional API calls",
            }
        except Exception as e:
            return {"error": f"Failed to get price history: {e}"}

    async def _place_order(self, args: dict) -> dict:
        """Place a limit order."""
        try:
            if not self.private_key:
                return {"error": "Trading not enabled - no private key configured"}

            # This would implement actual order placement
            return {
                "status": "simulated",
                "order": {
                    "token_id": args["token_id"],
                    "side": args["side"],
                    "price": args["price"],
                    "size": args["size"],
                },
                "note": "Order placement requires full implementation",
            }
        except Exception as e:
            return {"error": f"Failed to place order: {e}"}


async def handle_request(mcp: PolymarketMCP, request: dict) -> dict:
    """Handle incoming MCP request."""
    method = request.get("method", "")
    id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        return make_response(
            id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "polymarket-mcp", "version": "0.1.0"},
            },
        )

    if method == "tools/list":
        return make_response(id, {"tools": mcp.get_tools()})

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = await mcp.call_tool(tool_name, arguments)
        return make_response(
            id, {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
        )

    return make_error(id, -32601, f"Method not found: {method}")


async def main():
    """Main MCP server loop - communicates via stdio."""
    mcp = PolymarketMCP()

    # Read from stdin, write to stdout
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        try:
            line = await reader.readline()
            if not line:
                break

            request = json.loads(line.decode())
            await handle_request(mcp, request)

            # Write response to stdout

        except json.JSONDecodeError:
            # Logged rather than silently swallowed: the client sees no reply,
            # so an unlogged failure here is indistinguishable from a hang.
            # Must go to stderr — stdout carries the JSON-RPC stream.
            logger.warning("Discarded malformed JSON request: %r", line[:200])
        except Exception:
            logger.exception("MCP request handler raised")


if __name__ == "__main__":
    asyncio.run(main())
