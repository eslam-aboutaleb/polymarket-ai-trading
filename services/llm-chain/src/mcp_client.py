"""Minimal stdio MCP client for research tools."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


class ResearchMCPClient:
    """Calls the open-source research MCP server over stdio."""

    _cache: dict[tuple[str, int], dict[str, Any]] = {}
    _ttl_seconds = 15 * 60

    def __init__(self):
        self.server_path = self._resolve_server_path()

    def _resolve_server_path(self) -> str:
        env_path = os.getenv("RESEARCH_MCP_SERVER_PATH", "").strip()
        candidates = [
            env_path,
            "/app/services/mcps/research/server.py",
            str(Path(__file__).resolve().parents[2] / "mcps" / "research" / "server.py"),
            str(Path.cwd() / "services" / "mcps" / "research" / "server.py"),
        ]
        for c in candidates:
            if c and Path(c).exists():
                return c
        return ""

    async def gather_market_research(
        self,
        condition_id: str,
        market_title: str,
        held_outcome: str,
        best_alt_outcome: str,
        recency_hours: int = 24,
        max_results: int = 8,
    ) -> dict[str, Any]:
        bucket = int(time.time() // self._ttl_seconds)
        key = (condition_id or market_title, bucket)
        cached = self._cache.get(key)
        if cached:
            return cached

        query = f'{market_title} {held_outcome} {best_alt_outcome}'.strip()
        web_results = await self._call_tool(
            "web_search",
            {
                "query": query,
                "recency_hours": recency_hours,
                "max_results": max_results,
            },
        )
        x_results = await self._call_tool(
            "x_posts_search",
            {
                "query": query,
                "recency_hours": recency_hours,
                "max_results": max_results,
            },
        )

        result = {
            "web_results": web_results,
            "x_results": x_results,
            "web_summary": self._summarize(web_results),
            "x_summary": self._summarize(x_results),
        }
        self._cache[key] = result
        return result

    async def _call_tool(self, tool: str, args: dict[str, Any]) -> list[dict[str, str]]:
        if not self.server_path:
            return []
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                self.server_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            payload = json.dumps({"tool": tool, "args": args}) + "\n"
            stdout, _ = await proc.communicate(payload.encode("utf-8"))
            if proc.returncode != 0:
                return []
            line = stdout.decode("utf-8").strip().splitlines()
            if not line:
                return []
            parsed = json.loads(line[-1])
            if not parsed.get("ok"):
                return []
            result = parsed.get("result") or []
            if isinstance(result, list):
                return [r for r in result if isinstance(r, dict)]
            return []
        except Exception:
            return []

    @staticmethod
    def _summarize(results: list[dict[str, str]]) -> str:
        if not results:
            return ""
        lines: list[str] = []
        for row in results[:4]:
            title = str(row.get("title") or "").strip()
            snippet = str(row.get("snippet") or "").strip()
            url = str(row.get("url") or "").strip()
            part = " - ".join(p for p in [title, snippet] if p)
            if url:
                part = f"{part} ({url})" if part else url
            if part:
                lines.append(part)
        return "\n".join(lines)
