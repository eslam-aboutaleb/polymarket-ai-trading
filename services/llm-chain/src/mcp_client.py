"""Backwards-compatible re-export of the shared research MCP client.

The implementation moved to :mod:`polymarket_mcp` (see ``services/shared/``) so
that ``llm-chain`` and ``cli-agent`` share one copy instead of maintaining
near-identical duplicates that had already drifted apart.

This module remains so existing call sites — ``from src.mcp_client import
ResearchMCPClient`` — keep working unchanged.
"""

from polymarket_mcp import ResearchMCPClient

__all__ = ["ResearchMCPClient"]
