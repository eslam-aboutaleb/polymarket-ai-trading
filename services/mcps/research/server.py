"""Open-source research MCP server (stdio JSON protocol).

Tools:
- web_search(query, recency_hours=24, max_results=8)
- x_posts_search(query, recency_hours=24, max_results=8)
"""
from __future__ import annotations

import json
import sys
from typing import Any

from ddgs import DDGS


def _timelimit_for_hours(recency_hours: int) -> str | None:
    if recency_hours <= 24:
        return "d"
    if recency_hours <= 24 * 7:
        return "w"
    if recency_hours <= 24 * 31:
        return "m"
    return "y"


def _normalize_result(item: dict[str, Any]) -> dict[str, str]:
    return {
        "title": str(item.get("title") or "").strip(),
        "url": str(item.get("href") or "").strip(),
        "snippet": str(item.get("body") or "").strip(),
    }


def web_search(
    query: str,
    recency_hours: int = 24,
    max_results: int = 8,
) -> list[dict[str, str]]:
    if not query.strip():
        return []

    timelimit = _timelimit_for_hours(max(1, recency_hours))
    results: list[dict[str, str]] = []
    with DDGS() as ddgs:
        for row in ddgs.text(
            keywords=query,
            max_results=max(1, min(20, int(max_results))),
            timelimit=timelimit,
        ):
            normalized = _normalize_result(row or {})
            if normalized["url"]:
                results.append(normalized)
    return results[: max(1, min(20, int(max_results)))]


def x_posts_search(
    query: str,
    recency_hours: int = 24,
    max_results: int = 8,
) -> list[dict[str, str]]:
    if not query.strip():
        return []

    primary_query = f"site:x.com {query}"
    primary = web_search(
        primary_query,
        recency_hours=recency_hours,
        max_results=max_results,
    )
    x_only = [
        r
        for r in primary
        if "x.com/" in r.get("url", "") or "twitter.com/" in r.get("url", "")
    ]
    if x_only:
        return x_only[: max(1, min(20, int(max_results)))]

    fallback_query = f"{query} x.com"
    fallback = web_search(
        fallback_query,
        recency_hours=recency_hours,
        max_results=max_results,
    )
    return [
        r
        for r in fallback
        if "x.com/" in r.get("url", "") or "twitter.com/" in r.get("url", "")
    ][: max(1, min(20, int(max_results)))]


def _run_one(payload: dict[str, Any]) -> dict[str, Any]:
    tool = str(payload.get("tool") or "").strip()
    args = payload.get("args") or {}
    if not isinstance(args, dict):
        args = {}

    if tool == "web_search":
        result = web_search(
            query=str(args.get("query") or ""),
            recency_hours=int(args.get("recency_hours", 24) or 24),
            max_results=int(args.get("max_results", 8) or 8),
        )
    elif tool == "x_posts_search":
        result = x_posts_search(
            query=str(args.get("query") or ""),
            recency_hours=int(args.get("recency_hours", 24) or 24),
            max_results=int(args.get("max_results", 8) or 8),
        )
    else:
        return {"ok": False, "error": f"Unsupported tool: {tool}"}
    return {"ok": True, "result": result}


def main() -> int:
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Request must be an object")
            response = _run_one(payload)
        except Exception as exc:  # pragma: no cover
            response = {"ok": False, "error": str(exc)}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
