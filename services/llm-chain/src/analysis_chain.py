"""
LangChain-based trading analysis service.
Provides AI-powered market analysis using configurable LLM providers.
"""

import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_community.tools import DuckDuckGoSearchRun
from langchain_core.callbacks import StreamingStdOutCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import Tool

from src.config import get_settings
from src.llm_factory import (
    LLMRequestConfig,
    create_llm,
    create_llm_for_request,
    create_llm_for_task,
    get_provider_info,
)
from src.mcp_client import ResearchMCPClient

logger = logging.getLogger(__name__)

# Auto-discovery fan-out caps. Each stage is bounded so a single invocation
# cannot fan out into an unbounded number of LLM calls.
EVENTS_TO_SCREEN = 5
MARKETS_TO_CONSIDER = 40
MARKETS_TO_SELECT = 10
MARKETS_TO_FORECAST = 5

# Lazy imports for optional providers
_TavilyClient = None
_NewsApiClient = None


def _parse_copy_recommendation(content: str) -> str:
    """Extract the copy-trade verdict from free-form model output.

    The prompt asks for a numbered ``Recommendation: COPY / REDUCE_SIZE / SKIP``
    line, so that field is preferred. Matching bare substrings anywhere in the
    response is unreliable: a market titled "Will Skips McGee ..." flips the
    verdict, and prose that merely mentions "skip" while recommending COPY does
    the same.

    Fails closed to ``"avoid"`` when no verdict is recognisable -- an
    unparseable answer must never authorise a trade.
    """
    text = content or ""

    match = re.search(
        r"recommendation\s*[:\-]\s*\**\s*(copy|reduce[_ ]?size|skip|avoid)\b",
        text,
        re.IGNORECASE,
    )
    if match:
        verdict = match.group(1).lower().replace(" ", "_")
        if verdict == "reduce_size":
            return "reduce_size"
        if verdict == "copy":
            return "copy"
        return "avoid"

    # No explicit field: fall back to a whole-word search for the verdict verbs.
    for pattern, verdict in (
        (r"\breduce[_ ]?size\b", "reduce_size"),
        (r"\b(skip|avoid)\b", "avoid"),
        (r"\bcopy\b", "copy"),
    ):
        if re.search(pattern, text, re.IGNORECASE):
            return verdict

    logger.warning("Copy-trade verdict not recognisable; failing closed to 'avoid'.")
    return "avoid"


def _get_tavily_client():
    """Create Tavily client if API key is set."""
    global _TavilyClient
    settings = get_settings()
    if not settings.tavily_enabled or not settings.tavily_api_key:
        return None
    if _TavilyClient is None:
        try:
            from tavily import TavilyClient

            _TavilyClient = TavilyClient
        except ImportError:
            logger.warning("tavily-python not installed")
            return None
    return _TavilyClient(api_key=settings.tavily_api_key)


def _get_newsapi_client():
    """Create NewsAPI client if API key is set."""
    global _NewsApiClient
    settings = get_settings()
    if not settings.newsapi_enabled or not settings.newsapi_api_key:
        return None
    if _NewsApiClient is None:
        try:
            from newsapi import NewsApiClient

            _NewsApiClient = NewsApiClient
        except ImportError:
            logger.warning("newsapi-python not installed")
            return None
    return _NewsApiClient(api_key=settings.newsapi_api_key)


class PromptManager:
    """Manages loading and accessing prompts from external JSON file."""

    _instance = None
    _prompts: dict = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_prompts()
        return cls._instance

    def _load_prompts(self):
        """Load prompts from external JSON file."""
        settings = get_settings()
        prompts_path = settings.prompts_path
        try:
            with Path(prompts_path).open() as f:
                self._prompts = json.load(f)
            logger.info("Loaded prompts from %s", prompts_path)
        except FileNotFoundError:
            logger.warning("Prompts file not found at %s, using defaults", prompts_path)
            self._prompts = self._default_prompts()

    def _default_prompts(self) -> dict:
        return {
            "market_analysis": {
                "system_prompt": (
                    "You are an expert prediction market analyst. "
                    "Provide actionable trading insights with probability assessments."
                ),
                "market_assessment": (
                    "Analyze this market: {market_title}\n"
                    "Description: {market_description}\n"
                    "Yes: ${yes_price}, No: ${no_price}\n"
                    "Volume: ${volume_24h}\nEnd: {end_date}\n\nContext:\n{news_context}"
                ),
                "quick_analysis": "Quick analysis: {question} at ${current_price}",
                "multi_market_scan": "Scan markets:\n{markets_json}",
            },
            "risk_management": {
                "risk_assessment": (
                    "Risk for {market_title}: Size ${position_size}, "
                    "Entry ${entry_price}, {days_to_expiry} days"
                )
            },
            "trading": {
                "trade_execution_plan": (
                    "Plan for {action} on {market_title}: Target ${target_size} at ${current_price}"
                )
            },
        }

    def get(self, category: str, prompt_name: str) -> str:
        """Get a prompt by category and name."""
        if category in self._prompts and prompt_name in self._prompts[category]:
            return self._prompts[category][prompt_name]
        # Fall back to defaults
        defaults = self._default_prompts()
        if category in defaults and prompt_name in defaults[category]:
            return defaults[category][prompt_name]
        raise KeyError(f"Prompt not found: {category}.{prompt_name}")

    def reload(self):
        """Reload prompts from file."""
        self._load_prompts()


class TradingAnalysisChain:
    """
    LangChain-based trading analysis with web search capabilities.
    Implements the analysis logic for the gRPC service.
    Supports multiple LLM providers via configuration.
    """

    def __init__(self, streaming: bool = False, llm_instance=None):
        """
        Initialize the trading analysis chain.

        Args:
            streaming: Enable streaming responses
            llm_instance: Optional pre-configured LLM instance (for per-request overrides)
        """
        self.prompts = PromptManager()
        self._streaming = streaming
        self._task_llm_cache: dict[str, Any] = {}

        # Use provided LLM or create one using factory (provider-agnostic)
        if llm_instance is not None:
            self.llm = llm_instance
            self._request_llm_explicit = True
            logger.info("Using provided LLM instance for this request")
        else:
            callbacks = [StreamingStdOutCallbackHandler()] if streaming else None
            self.llm = create_llm(streaming=streaming, callbacks=callbacks)
            self._request_llm_explicit = False

            # Log provider info
            provider_info = get_provider_info()
            logger.info(
                "Initialized LLM: %s / %s", provider_info["provider"], provider_info["model"]
            )

        # Initialize search tool
        self.search_tool = DuckDuckGoSearchRun()

        # Initialize tools list
        self.tools = [
            Tool(
                name="web_search",
                description=(
                    "Search the web for recent news and information about prediction markets."
                ),
                func=self.search_tool.run,
            ),
        ]
        self.research_mcp = ResearchMCPClient()

    def _get_llm(self, llm_override=None):
        """Get the LLM to use - either override or default."""
        return llm_override if llm_override is not None else self.llm

    def _get_task_llm(self, task: str, llm_override=None):
        """Resolve the LLM for a task with the documented precedence.

        A per-call ``llm_override`` wins. Otherwise an LLM supplied
        when this chain was created (a request-level override) wins.
        Otherwise the task's tier-resolved LLM is created and cached.
        """
        if llm_override is not None:
            return llm_override
        if self._request_llm_explicit:
            return self.llm
        if task not in self._task_llm_cache:
            callbacks = [StreamingStdOutCallbackHandler()] if self._streaming else None
            self._task_llm_cache[task] = create_llm_for_task(
                task, streaming=self._streaming, callbacks=callbacks
            )
        return self._task_llm_cache[task]

    async def analyze_market(
        self,
        market_title: str,
        market_description: str,
        yes_price: float,
        no_price: float,
        volume_24h: float,
        end_date: str,
        include_research: bool = True,
        llm_override=None,
    ) -> dict[str, Any]:
        """Perform comprehensive market analysis with optional web research."""
        llm = self._get_task_llm("market_assessment", llm_override)
        news_context = ""

        if include_research:
            news_context = await self._research_market(market_title, market_description)

        prompt_template = self.prompts.get("market_analysis", "market_assessment")
        analysis_prompt = prompt_template.format(
            market_title=market_title,
            market_description=market_description,
            yes_price=yes_price,
            no_price=no_price,
            volume_24h=volume_24h,
            end_date=end_date,
            news_context=news_context or "No additional research performed.",
        )

        system_prompt = self.prompts.get("market_analysis", "system_prompt")

        messages = [SystemMessage(content=system_prompt), HumanMessage(content=analysis_prompt)]

        response = await llm.ainvoke(messages)

        return {
            "analysis": response.content,
            "research_context": news_context,
            "timestamp": datetime.utcnow().isoformat(),
        }

    async def analyze_market_stream(
        self,
        market_title: str,
        market_description: str,
        yes_price: float,
        no_price: float,
        volume_24h: float,
        end_date: str,
        include_research: bool = True,
        llm_override=None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream market analysis for real-time updates."""
        # First yield research phase
        if include_research:
            yield {"chunk": "Researching market...", "chunk_type": "research", "is_final": False}
            news_context = await self._research_market(market_title, market_description)
            yield {
                "chunk": f"Research complete.\n\n{news_context[:500]}...",
                "chunk_type": "research",
                "is_final": False,
            }
        else:
            news_context = ""

        # Then stream analysis
        yield {"chunk": "\n\nAnalyzing market...\n", "chunk_type": "analysis", "is_final": False}

        prompt_template = self.prompts.get("market_analysis", "market_assessment")
        analysis_prompt = prompt_template.format(
            market_title=market_title,
            market_description=market_description,
            yes_price=yes_price,
            no_price=no_price,
            volume_24h=volume_24h,
            end_date=end_date,
            news_context=news_context or "No research.",
        )

        system_prompt = self.prompts.get("market_analysis", "system_prompt")
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=analysis_prompt)]

        llm = self._get_task_llm("market_assessment", llm_override)
        # Stream the response
        async for chunk in llm.astream(messages):
            yield {"chunk": chunk.content, "chunk_type": "analysis", "is_final": False}

        yield {"chunk": "", "chunk_type": "recommendation", "is_final": True}

    async def _research_market(self, market_title: str, description: str) -> str:
        """
        Use multi-source search to gather current information.
        Priority: Tavily (premium) > DuckDuckGo (free fallback).
        Also fetches NewsAPI headlines and Binance market data when available.
        """
        all_results = []

        # 1. Try Tavily premium search first (better relevance)
        tavily_results = await self._research_with_tavily(market_title)
        if tavily_results:
            all_results.append(f"== Tavily Research ==\n{tavily_results}")

        # 2. DuckDuckGo fallback / supplement
        try:
            # The market description carries the specifics (dates, entities,
            # resolution criteria) that the title alone does not.
            topic = f"{market_title} {description}".strip() if description else market_title
            queries = [f"{topic} latest news", f"{topic} prediction analysis"]

            results = []
            for query in queries[:2]:
                try:
                    result = self.search_tool.run(query)
                    results.append(f"Query: {query}\nResults: {result}\n")
                except Exception as e:
                    logger.debug(f"DuckDuckGo search error for '{query}': {e}")

            if results:
                all_results.append("== Web Search ==\n" + "\n---\n".join(results))
        except Exception as e:
            logger.debug(f"DuckDuckGo research error: {e}")

        # 3. NewsAPI headlines
        news_context = await self._get_news_context(market_title)
        if news_context:
            all_results.append(f"== Recent News Headlines ==\n{news_context}")

        # 4. Binance Smart Money & Market Data
        binance_context = await self._get_binance_context(market_title)
        if binance_context:
            all_results.append(binance_context)

        return "\n\n".join(all_results) if all_results else "No search results available."

    async def _get_binance_context(self, market_title: str) -> str:
        """Fetch Binance smart money signals and market data for crypto-related markets."""
        try:
            from binance_skills_client import BinanceSkillsClient

            client = BinanceSkillsClient()
            return await client.get_research_context(market_title)
        except ImportError:
            pass

        # Fallback: use the MCP server tool
        try:
            research = await self.research_mcp._gather_binance_context(market_title)
            formatted = research.get("formatted", "")
            return formatted if formatted else ""
        except Exception as e:
            logger.debug(f"Binance context fetch failed: {e}")
            return ""

    async def _research_with_tavily(self, query: str) -> str:
        """Use Tavily premium search if available."""
        client = _get_tavily_client()
        if client is None:
            return ""
        try:
            response = client.search(
                query=f"{query} prediction market analysis",
                max_results=5,
                search_depth="advanced",
            )
            results = []
            for item in response.get("results", []):
                title = item.get("title", "")
                content = item.get("content", "")
                url = item.get("url", "")
                results.append(f"• {title}\n  {content[:300]}\n  Source: {url}")
            return "\n".join(results) if results else ""
        except Exception as e:
            logger.warning(f"Tavily search failed: {e}")
            return ""

    async def _get_news_context(self, query: str) -> str:
        """Fetch recent news headlines via NewsAPI if available."""
        client = _get_newsapi_client()
        if client is None:
            return ""
        try:
            response = client.get_everything(
                q=query,
                language="en",
                sort_by="relevancy",
                page_size=5,
            )
            articles = response.get("articles", [])
            if not articles:
                return ""
            lines = []
            for a in articles[:5]:
                title = a.get("title", "")
                desc = a.get("description", "")
                source = (a.get("source") or {}).get("name", "")
                pub_at = a.get("publishedAt", "")
                lines.append(f"• [{source}] {title} ({pub_at[:10]})\n  {desc[:200]}")
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"NewsAPI fetch failed: {e}")
            return ""

    async def quick_analysis(self, question: str, current_price: float) -> str:
        """Quick 2-3 sentence analysis."""
        prompt_template = self.prompts.get("market_analysis", "quick_analysis")
        prompt = prompt_template.format(question=question, current_price=current_price)

        messages = [HumanMessage(content=prompt)]
        llm = self._get_task_llm("quick_analysis")
        response = await llm.ainvoke(messages)
        return response.content

    async def scan_markets(self, markets: list[dict]) -> dict[str, Any]:
        """Scan multiple markets for opportunities."""
        prompt_template = self.prompts.get("market_analysis", "multi_market_scan")
        prompt = prompt_template.format(markets_json=json.dumps(markets, indent=2))

        system_prompt = self.prompts.get("market_analysis", "system_prompt")
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=prompt)]

        llm = self._get_task_llm("multi_market_scan")
        response = await llm.ainvoke(messages)
        return {
            "analysis": response.content,
            "markets_scanned": len(markets),
            "timestamp": datetime.utcnow().isoformat(),
        }

    async def assess_risk(
        self,
        market_title: str,
        position_size: float,
        entry_price: float,
        days_to_expiry: int,
        correlation_info: str = "No correlation data",
    ) -> dict[str, Any]:
        """Assess risk for a potential trade."""
        prompt_template = self.prompts.get("risk_management", "risk_assessment")
        prompt = prompt_template.format(
            market_title=market_title,
            position_size=position_size,
            entry_price=entry_price,
            days_to_expiry=days_to_expiry,
            correlation_info=correlation_info,
        )

        messages = [HumanMessage(content=prompt)]
        llm = self._get_task_llm("risk_assessment")
        response = await llm.ainvoke(messages)

        return {"analysis": response.content, "timestamp": datetime.utcnow().isoformat()}

    async def generate_trade_plan(
        self,
        action: str,
        market_title: str,
        target_size: float,
        current_price: float,
        order_book: dict = None,
    ) -> dict[str, Any]:
        """Generate a trade execution plan."""
        prompt_template = self.prompts.get("trading", "trade_execution_plan")
        prompt = prompt_template.format(
            action=action,
            market_title=market_title,
            target_size=target_size,
            current_price=current_price,
            order_book=json.dumps(order_book) if order_book else "N/A",
        )

        messages = [HumanMessage(content=prompt)]
        llm = self._get_task_llm("trade_execution_plan")
        response = await llm.ainvoke(messages)

        return {"analysis": response.content, "timestamp": datetime.utcnow().isoformat()}

    # ── NEW: Trader Analysis & Copy-Trade Evaluation ─────────

    async def scan_opportunity(
        self,
        market_title: str,
        market_description: str,
        yes_price: float,
        no_price: float,
        volume_24h: float,
        liquidity: float,
        end_date: str,
        pnl_potential: float,
        smart_money_context: str,
        condition_id: str = "",
    ) -> dict[str, Any]:
        """
        Score a single market opportunity: web-research for credibility,
        analyse smart money positioning, return structured JSON scoring.
        Uses DuckDuckGo search for credibility validation.
        """
        # 1. Web search for credibility validation
        search_findings = ""
        try:
            queries = [
                f"{market_title} latest news credibility",
                f"{market_title} prediction market analysis",
            ]
            results = []
            for q in queries:
                try:
                    result = self.search_tool.run(q)
                    results.append(f"Query: {q}\nResults: {result}\n")
                except Exception as e:
                    logger.warning("Opportunity search error for %r: %s", q, e)
            search_findings = "\n---\n".join(results) if results else "No search results available."
        except Exception as e:
            search_findings = f"Search unavailable: {e}"

        # 1b. Binance smart-money context for crypto-related markets
        binance_context = ""
        try:
            binance_context = await self._get_binance_context(market_title)
        except Exception as e:
            logger.debug(f"Binance context for opportunity scan failed: {e}")

        # Merge Binance data into smart_money_context if available
        combined_smart_money = smart_money_context or ""
        if binance_context:
            combined_smart_money += f"\n\n--- Binance Smart Money Data ---\n{binance_context}"

        # 2. Build the prompt from the opportunity_analysis template
        prompt_template = self.prompts.get("opportunity_analysis", "market_scoring")
        if not prompt_template:
            # Fallback prompt
            prompt_template = (
                "Score this market opportunity: {market_title}\n"
                "YES: {yes_price_cents}c, NO: {no_price_cents}c\n"
                "Search: {search_findings}\nSmart Money: {smart_money_context}\n"
                "Return JSON with ai_score, risk_level, recommendation."
            )

        prompt = prompt_template.format(
            market_title=market_title,
            market_description=market_description or market_title,
            yes_price_cents=f"{yes_price * 100:.1f}",
            no_price_cents=f"{no_price * 100:.1f}",
            volume_24h=f"{volume_24h:,.0f}",
            liquidity=f"{liquidity:,.0f}",
            end_date=end_date,
            pnl_potential=f"{pnl_potential:.2f}",
            search_findings=search_findings[:2000],
            smart_money_context=combined_smart_money[:2000],
        )

        system_prompt = self.prompts.get("opportunity_analysis", "system_prompt")
        if not system_prompt:
            system_prompt = (
                "You are an expert prediction market analyst. "
                "Respond with ONLY valid JSON — no markdown, no extra text."
            )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=prompt),
        ]

        llm = self._get_task_llm("scan_opportunity")
        response = await llm.ainvoke(messages)
        raw = response.content.strip()

        # Parse JSON from LLM response
        import re

        json_match = re.search(r"\{[\s\S]*\}", raw)
        if json_match:
            try:
                scored = json.loads(json_match.group())
            except json.JSONDecodeError:
                scored = {}
        else:
            scored = {}

        # Ensure all required fields with defaults
        return {
            "condition_id": condition_id,
            "market_title": market_title,
            "ai_score": int(scored.get("ai_score", 50)),
            "risk_level": scored.get("risk_level", "medium"),
            "pnl_potential": float(scored.get("pnl_potential", pnl_potential)),
            "credibility_score": int(scored.get("credibility_score", 50)),
            "smart_money_signal": scored.get("smart_money_signal", "neutral"),
            "smart_money_summary": scored.get("smart_money_summary", "No smart money data"),
            "recommendation": scored.get("recommendation", "hold"),
            "recommended_side": scored.get(
                "recommended_side", "YES" if yes_price < no_price else "NO"
            ),
            "reasoning": scored.get("reasoning", raw[:300]),
            "search_summary": scored.get("search_summary", search_findings[:200]),
            "key_risks": scored.get("key_risks", []),
            "search_findings": search_findings[:500],
            "timestamp": datetime.utcnow().isoformat(),
        }

    async def scan_event_opportunity(
        self,
        event_title: str,
        sub_markets: list[dict[str, Any]],
        event_volume: str = "0",
        event_liquidity: str = "0",
        smart_money_context: str = "",
    ) -> dict[str, Any]:
        """
        Score an entire event group (multiple related sub-markets) with ONE
        LLM call instead of scoring each sub-market independently.
        Returns a single scoring dict that includes 'recommended_option'.
        """
        # 1. Web search — ONE search for the whole event
        search_findings = ""
        try:
            queries = [
                f"{event_title} latest news prediction",
                f"{event_title} odds analysis forecast",
            ]
            results = []
            for q in queries:
                try:
                    result = self.search_tool.run(q)
                    results.append(f"Query: {q}\nResults: {result}\n")
                except Exception as e:
                    logger.warning("Event search error for %r: %s", q, e)
            search_findings = "\n---\n".join(results) if results else "No search results available."
        except Exception as e:
            search_findings = f"Search unavailable: {e}"

        # 1b. Binance smart-money context for crypto-related events
        binance_context = ""
        try:
            binance_context = await self._get_binance_context(event_title)
        except Exception as e:
            logger.debug(f"Binance context for event scan failed: {e}")

        combined_smart_money = smart_money_context or ""
        if binance_context:
            combined_smart_money += f"\n\n--- Binance Smart Money Data ---\n{binance_context}"

        # 2. Build sub-markets table (markdown-style)
        table_lines = [
            "| Option | YES Price | NO Price | 24h Volume | Liquidity |",
            "|--------|-----------|----------|------------|-----------|",
        ]
        for sm in sub_markets:
            label = sm.get("label", sm.get("question", "Unknown"))
            yes_p = sm.get("yes_price", 0)
            no_p = sm.get("no_price", 0)
            vol = sm.get("volume_24h", 0)
            liq = sm.get("liquidity", 0)
            table_lines.append(
                f"| {label} | {yes_p * 100:.1f}¢ | {no_p * 100:.1f}¢ | ${vol:,.0f} | ${liq:,.0f} |"
            )
        sub_markets_table = "\n".join(table_lines)

        # 3. Build prompt from event_scoring template
        prompt_template = self.prompts.get("opportunity_analysis", "event_scoring")
        if not prompt_template:
            prompt_template = (
                "Score this event group: {event_title}\n"
                "Sub-markets:\n{sub_markets_table}\n"
                "Search: {search_findings}\nSmart Money: {smart_money_context}\n"
                "Return JSON with ai_score, risk_level, recommendation, recommended_option."
            )

        prompt = prompt_template.format(
            event_title=event_title,
            sub_markets_table=sub_markets_table,
            event_volume=event_volume,
            event_liquidity=event_liquidity,
            search_findings=search_findings[:2000],
            smart_money_context=combined_smart_money[:2000],
        )

        system_prompt = self.prompts.get("opportunity_analysis", "system_prompt")
        if not system_prompt:
            system_prompt = (
                "You are an expert prediction market analyst. "
                "Respond with ONLY valid JSON — no markdown, no extra text."
            )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=prompt),
        ]

        llm = self._get_task_llm("event_quick_analysis")
        response = await llm.ainvoke(messages)
        raw = response.content.strip()

        # Parse JSON
        import re

        json_match = re.search(r"\{[\s\S]*\}", raw)
        if json_match:
            try:
                scored = json.loads(json_match.group())
            except json.JSONDecodeError:
                scored = {}
        else:
            scored = {}

        return {
            "event_title": event_title,
            "ai_score": int(scored.get("ai_score", 50)),
            "risk_level": scored.get("risk_level", "medium"),
            "pnl_potential": float(scored.get("pnl_potential", 0.5)),
            "credibility_score": int(scored.get("credibility_score", 50)),
            "smart_money_signal": scored.get("smart_money_signal", "neutral"),
            "smart_money_summary": scored.get("smart_money_summary", "No smart money data"),
            "recommendation": scored.get("recommendation", "hold"),
            "recommended_side": scored.get("recommended_side", "YES"),
            "recommended_option": scored.get("recommended_option", ""),
            "reasoning": scored.get("reasoning", raw[:300]),
            "search_summary": scored.get("search_summary", search_findings[:200]),
            "key_risks": scored.get("key_risks", []),
            "search_findings": search_findings[:500],
            "timestamp": datetime.utcnow().isoformat(),
        }

    async def analyze_trader(
        self,
        wallet_address: str,
        display_name: str = "",
        total_pnl: float = 0.0,
        win_rate: float = 0.0,
        trade_count: int = 0,
        markets_traded: int = 0,
        recent_trades_json: str = "[]",
    ) -> dict[str, Any]:
        """Full trader profile analysis with internet research."""
        # Web research on the trader
        research_context = ""
        try:
            queries = [
                f"polymarket trader {wallet_address}",
                f"polymarket whale {display_name or wallet_address[:10]} prediction market",
            ]
            results = []
            for q in queries:
                try:
                    result = self.search_tool.run(q)
                    results.append(f"Query: {q}\n{result}")
                except Exception as e:
                    logger.warning("Trader search error for %r: %s", q, e)
            research_context = "\n---\n".join(results) if results else "No search results."
        except Exception as e:
            research_context = f"Research unavailable: {e}"

        prompt_template = self.prompts.get("trader_analysis", "profile_research")
        prompt = prompt_template.format(
            wallet_address=wallet_address,
            display_name=display_name or "Unknown",
            total_pnl=total_pnl,
            win_rate=win_rate,
            trade_count=trade_count,
            markets_traded=markets_traded,
            recent_trades=recent_trades_json,
        )

        system_prompt = self.prompts.get("trader_analysis", "system_prompt")
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"{prompt}\n\nInternet Research:\n{research_context}"),
        ]
        llm = self._get_task_llm("trader_profile")
        response = await llm.ainvoke(messages)

        return {
            "analysis": response.content,
            "research_context": research_context,
            "timestamp": datetime.utcnow().isoformat(),
        }

    async def evaluate_copy_trade(
        self,
        trader_wallet: str,
        trader_stats: str,
        market_id: str = "",
        market_title: str = "",
        trade_side: str = "BUY",
        trade_size: float = 0.0,
        current_price: float = 0.0,
        user_risk_profile: str = "max_position_daily_loss",
    ) -> dict[str, Any]:
        """Evaluate whether to copy a specific trade – returns recommendation."""
        # Quick web search on the market
        market_research = ""
        if market_title or market_id:
            try:
                query = f"{market_title or market_id} polymarket prediction"
                market_research = self.search_tool.run(query)
            except Exception:
                market_research = "No market research available."

        # Gather Binance smart money context for crypto-related markets
        binance_context = ""
        try:
            binance_context = await self._get_binance_context(market_title or market_id)
        except Exception as e:
            logger.debug(f"Binance context for copy-trade failed: {e}")

        prompt_template = self.prompts.get("copy_trading", "trade_evaluation")
        prompt = prompt_template.format(
            trader_wallet=trader_wallet,
            trader_stats=trader_stats,
            market_title=market_title or market_id,
            market_id=market_id,
            trade_side=trade_side,
            trade_size=trade_size,
            current_price=current_price,
            user_risk_profile=user_risk_profile,
        )

        extra_context = f"\n\nMarket Research:\n{market_research}"
        if binance_context:
            extra_context += f"\n\nBinance Smart Money & Market Data:\n{binance_context}"

        # The copy-trade evaluation is a prose task: use the
        # copy_trading system prompt, not the trader_analysis one
        # (which demands JSON-only output and contradicts the
        # prose trade_evaluation task prompt).
        system_prompt = self.prompts.get("copy_trading", "system_prompt")
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"{prompt}{extra_context}"),
        ]
        llm = self._get_task_llm("copy_trade_eval")
        response = await llm.ainvoke(messages)

        # Parse recommendation from LLM output
        recommendation = _parse_copy_recommendation(response.content)

        # Try to extract confidence
        confidence = 50.0
        conf_match = re.search(r"confidence[:\s]*(\d+)", response.content, re.IGNORECASE)
        if conf_match:
            confidence = min(100, max(0, float(conf_match.group(1))))

        return {
            "analysis": response.content,
            "recommendation": recommendation,
            "confidence": confidence,
            "ai_score": confidence,
            "risk_level": "high" if confidence < 40 else "medium" if confidence < 70 else "low",
            "market_sentiment": None,
            "research_context": market_research,
            "timestamp": datetime.utcnow().isoformat(),
        }

    async def evaluate_inverse_position(
        self,
        condition_id: str,
        market_title: str,
        held_outcome: str,
        held_pct: float,
        best_alt_outcome: str,
        best_alt_pct: float,
        delta_pct: float,
        alternatives_json: str = "[]",
        include_research: bool = True,
    ) -> dict[str, Any]:
        """Evaluate whether an open position should be reversed."""
        try:
            alternatives = json.loads(alternatives_json or "[]")
            if not isinstance(alternatives, list):
                alternatives = []
        except Exception:
            alternatives = []

        web_summary = ""
        x_summary = ""
        binance_summary = ""
        x_results: list[dict[str, str]] = []
        if include_research:
            research = await self.research_mcp.gather_market_research(
                condition_id=condition_id,
                market_title=market_title,
                held_outcome=held_outcome,
                best_alt_outcome=best_alt_outcome,
                recency_hours=24,
                max_results=8,
            )
            web_summary = research.get("web_summary", "")
            x_summary = research.get("x_summary", "")
            x_results = research.get("x_results", []) or []
            binance_summary = research.get("binance_summary", "")

        prompt_template = self.prompts.get("inverse_position_bot", "evaluation_prompt")
        if not prompt_template:
            prompt_template = (
                "Evaluate whether to reverse this position.\n"
                "Market: {market_title}\n"
                "Held outcome: {held_outcome} ({held_pct:.3f})\n"
                "Best alternative: {best_alt_outcome} ({best_alt_pct:.3f})\n"
                "Delta: {delta_pct:.3f}\n"
                "Alternatives: {alternatives_json}\n"
                "Web: {web_summary}\n"
                "X: {x_summary}\n"
                "Binance Smart Money: {binance_summary}\n"
                "Return JSON with: recommendation, confidence, reasoning, key_risks, "
                "alt_outcome, alt_token_id, web_summary, x_summary."
            )

        system_prompt = self.prompts.get("inverse_position_bot", "system_prompt")
        if not system_prompt:
            system_prompt = (
                "You are a conservative Polymarket risk engine. Respond with only valid JSON."
            )

        # Both the packaged template and the inline fallback above declare every
        # placeholder, so this render cannot raise KeyError.
        prompt = prompt_template.format(
            market_title=market_title,
            held_outcome=held_outcome,
            held_pct=held_pct,
            best_alt_outcome=best_alt_outcome,
            best_alt_pct=best_alt_pct,
            delta_pct=delta_pct,
            alternatives_json=json.dumps(alternatives, ensure_ascii=True),
            web_summary=web_summary or "No web evidence found in last 24h.",
            x_summary=x_summary or "No X evidence found in last 24h.",
            binance_summary=binance_summary or "No Binance smart money data available.",
        )

        llm = self._get_task_llm("inverse_position_eval")
        response = await llm.ainvoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=prompt),
            ]
        )
        raw = str(response.content or "").strip()

        parsed: dict[str, Any] = {}
        match = re.search(r"\{[\s\S]*\}", raw)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                parsed = {}

        recommendation = str(parsed.get("recommendation", "hold")).lower()
        try:
            confidence = float(parsed.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        # Cap confidence if no X evidence, but Binance data can partially compensate
        if not x_results and not binance_summary:
            confidence = min(confidence, 70.0)
        elif not x_results and binance_summary:
            confidence = min(confidence, 80.0)

        key_risks = parsed.get("key_risks", [])
        if not isinstance(key_risks, list):
            key_risks = []

        return {
            "analysis": raw,
            "recommendation": recommendation,
            "confidence": confidence,
            "reasoning": str(parsed.get("reasoning", raw[:600])),
            "key_risks": key_risks,
            "alt_outcome": str(parsed.get("alt_outcome", best_alt_outcome)),
            "alt_token_id": str(parsed.get("alt_token_id", "")),
            "web_summary": str(parsed.get("web_summary", web_summary)),
            "x_summary": str(parsed.get("x_summary", x_summary)),
            "timestamp": datetime.utcnow().isoformat(),
        }

    # ── NEW: Sentiment Analysis ────────────────────────────────

    async def analyze_sentiment(
        self,
        query: str,
        include_news: bool = True,
        include_social: bool = True,
        outcome: str = "",
        llm_override=None,
    ) -> dict[str, Any]:
        """
        Analyse the public sentiment around a market / topic.
        Gathers web + news + social signals, then asks the LLM to produce
        a structured sentiment score and breakdown.

        ``outcome`` names the specific outcome the sentiment is scored *for*
        (e.g. "YES"); the score is directional with respect to it. When empty,
        sentiment is scored against the market question as a whole.

        Returns dict with: sentiment_score (0-1), label, summary, sources, etc.
        """
        llm = self._get_task_llm("sentiment_analysis", llm_override)
        evidence_parts: list[str] = []

        # 1. Web search (always)
        try:
            web_results = self.search_tool.run(f"{query} sentiment opinion")
            evidence_parts.append(f"== Web Search ==\n{web_results}")
        except Exception as e:
            logger.debug(f"Sentiment web search failed: {e}")

        # 2. Tavily deep search
        tavily_text = await self._research_with_tavily(f"{query} public opinion sentiment")
        if tavily_text:
            evidence_parts.append(f"== Deep Search ==\n{tavily_text}")

        # 3. News headlines
        if include_news:
            news_text = await self._get_news_context(query)
            if news_text:
                evidence_parts.append(f"== Recent News ==\n{news_text}")

        # 4. X / Twitter signals
        if include_social:
            try:
                # Must be awaited: without it this bound a coroutine, which is
                # always truthy, so the subscript below raised TypeError and the
                # social evidence block was silently dead in every request.
                x_results = await self.research_mcp._call_tool(
                    "x_posts_search",
                    {"query": query, "recency_hours": 48, "max_results": 10},
                )
                if x_results:
                    x_text = "\n".join(
                        f"• {r.get('title', '')} — {r.get('snippet', '')}" for r in x_results[:10]
                    )
                    evidence_parts.append(f"== Social/X Posts ==\n{x_text}")
            except Exception:
                logger.warning(
                    "X posts search failed; continuing without social evidence",
                    exc_info=True,
                )

        # 5. Binance social hype & smart money signals for crypto markets
        try:
            binance_context = await self._get_binance_context(query)
            if binance_context:
                evidence_parts.append(f"== Binance Smart Money & Social Hype ==\n{binance_context}")
        except Exception as e:
            logger.debug(f"Binance sentiment context failed: {e}")

        evidence = "\n\n".join(evidence_parts) if evidence_parts else "No evidence gathered."

        # 5. LLM sentiment analysis
        prompt_template = self.prompts.get("research", "sentiment_analysis")
        prompt = prompt_template.format(
            question=query,
            outcome=outcome or "the market as a whole",
            content=evidence,
        )
        messages = [HumanMessage(content=prompt)]
        response = await llm.ainvoke(messages)
        raw = response.content.strip()

        # Parse JSON
        json_match = re.search(r"\{[\s\S]*\}", raw)
        if json_match:
            try:
                scored = json.loads(json_match.group())
            except json.JSONDecodeError:
                scored = {}
        else:
            scored = {}

        try:
            sentiment_score = float(scored.get("sentiment_score", 0.5))
        except (TypeError, ValueError):
            sentiment_score = 0.5

        return {
            "sentiment_score": round(sentiment_score, 3),
            "label": str(scored.get("label", "neutral")),
            "summary": str(scored.get("summary", raw[:500])),
            "bullish_factors": scored.get("bullish_factors", []),
            "bearish_factors": scored.get("bearish_factors", []),
            "source_count": len(evidence_parts),
            "evidence_snippet": evidence[:1000],
            "timestamp": datetime.utcnow().isoformat(),
        }

    # ── NEW: Autonomous Trade Discovery ────────────────────────

    async def discover_best_trade(
        self,
        markets: list[dict[str, Any]],
        budget: float = 100.0,
        risk_tolerance: str = "medium",
        llm_override=None,
    ) -> dict[str, Any]:
        """
        Autonomous trade discovery pipeline inspired by Polymarket/agents.
        Steps:
          1. Filter events (LLM picks top event categories)
          2. Filter markets (LLM picks top markets from those events)
          3. Superforecast each shortlisted market (calibrated probability)
          4. Pick the single best trade (highest edge)

        Args:
            markets: List of active market dicts from Polymarket API
            budget: Available budget in USDC
            risk_tolerance: "low", "medium", "high"
            llm_override: Optional custom LLM

        Returns:
            Dict with best_trade recommendation, reasoning, all_candidates, etc.
        """
        llm = self._get_task_llm("discover_best_trade", llm_override)

        # ── Step 1: Group into events and filter ──
        logger.info(f"Auto-discovery: starting with {len(markets)} markets, budget=${budget}")

        # Group markets by event/slug prefix
        events: dict[str, list[dict]] = {}
        for m in markets:
            slug = str(m.get("slug", "") or m.get("event_slug", "") or "other")
            event_key = slug.split("-")[0] if "-" in slug else slug
            events.setdefault(event_key, []).append(m)

        events_summary = json.dumps(
            [
                {
                    "event": k,
                    "num_markets": len(v),
                    "total_volume": sum(float(m.get("volume_24h", 0) or 0) for m in v),
                }
                for k, v in list(events.items())[:50]
            ],
            indent=2,
        )

        filter_events_prompt = self.prompts.get("auto_discovery", "filter_events")
        prompt = filter_events_prompt.format(
            events_json=events_summary,
            top_n=EVENTS_TO_SCREEN,
        )
        filter_sys = self.prompts.get("auto_discovery", "system_prompt")
        response = await llm.ainvoke(
            [
                SystemMessage(content=filter_sys),
                HumanMessage(content=prompt),
            ]
        )

        # Parse selected event keys from LLM response
        selected_event_keys: list[str] = []
        # A malformed model response simply means "no explicit selection".
        with contextlib.suppress(Exception):
            arr_match = re.search(r"\[[\s\S]*?\]", response.content)
            if arr_match:
                selected_event_keys = json.loads(arr_match.group())
        if not selected_event_keys:
            # Fallback: keep top 5 by volume
            sorted_events = sorted(
                events.items(),
                key=lambda kv: sum(float(m.get("volume_24h", 0) or 0) for m in kv[1]),
                reverse=True,
            )
            selected_event_keys = [k for k, _ in sorted_events[:EVENTS_TO_SCREEN]]

        # Gather markets from selected events
        candidate_markets = []
        for key in selected_event_keys:
            candidate_markets.extend(events.get(key, []))

        logger.info(
            f"Auto-discovery: {len(selected_event_keys)} events, "
            f"{len(candidate_markets)} candidate markets"
        )

        # ── Step 2: Filter to top N markets ──
        if len(candidate_markets) > 20:
            markets_summary = json.dumps(
                [
                    {
                        "question": m.get("question", "")[:100],
                        "yes_price": float(m.get("outcomePrices", [0.5])[0])
                        if m.get("outcomePrices")
                        else 0.5,
                        "volume_24h": float(m.get("volume_24h", 0) or 0),
                        "liquidity": float(m.get("liquidity", 0) or 0),
                    }
                    for m in candidate_markets[:MARKETS_TO_CONSIDER]
                ],
                indent=2,
            )
            filter_markets_prompt = self.prompts.get("auto_discovery", "filter_markets")
            prompt = filter_markets_prompt.format(
                markets_json=markets_summary,
                top_n=MARKETS_TO_SELECT,
            )
            response = await llm.ainvoke(
                [
                    SystemMessage(content=filter_sys),
                    HumanMessage(content=prompt),
                ]
            )
            # A malformed selection just falls through to the volume ordering.
            with contextlib.suppress(Exception):
                arr_match = re.search(r"\[[\s\S]*?\]", response.content)
                if arr_match:
                    selected = json.loads(arr_match.group())
                    if selected and isinstance(selected[0], dict):
                        # Object array as requested by the template:
                        # [{"id": ..., "question": ..., "edge_estimate": ...}]
                        # Match candidates by question (the only field the
                        # upstream summary actually exposes) and by id when
                        # the model echoes a condition id.
                        selected_questions = {
                            str(item.get("question", "")).strip().lower()
                            for item in selected
                            if isinstance(item, dict)
                        }
                        selected_questions.discard("")
                        selected_ids = {
                            str(item.get("id", "")).strip().lower()
                            for item in selected
                            if isinstance(item, dict)
                        }
                        selected_ids.discard("")
                        if selected_questions or selected_ids:
                            matched = []
                            for m in candidate_markets:
                                question = str(m.get("question", "")).strip().lower()
                                condition_id = (
                                    str(m.get("condition_id") or m.get("conditionId") or "")
                                    .strip()
                                    .lower()
                                )
                                if (
                                    question in selected_questions
                                    or (condition_id and condition_id in selected_ids)
                                    or any(
                                        q and len(q) >= 10 and (q in question or question in q)
                                        for q in selected_questions
                                    )
                                ):
                                    matched.append(m)
                            if matched:
                                candidate_markets = matched
                    elif selected and isinstance(selected[0], int):
                        candidate_markets = [
                            candidate_markets[i] for i in selected if i < len(candidate_markets)
                        ]
                    elif selected and isinstance(selected[0], str):
                        selected_set = {s.lower() for s in selected}
                        candidate_markets = [
                            m
                            for m in candidate_markets
                            if m.get("question", "").lower() in selected_set
                        ]
            candidate_markets = candidate_markets[:MARKETS_TO_SELECT]

        # ── Step 3: Superforecast each candidate ──
        forecasts = []
        for m in candidate_markets[:MARKETS_TO_FORECAST]:
            question = m.get("question", "")
            prices = m.get("outcomePrices", [])
            try:
                market_price = float(prices[0]) if prices else 0.5
            except (ValueError, TypeError):
                market_price = 0.5

            # Quick web research for each
            research_text = ""
            # Web research is best-effort; the forecast proceeds without it.
            with contextlib.suppress(Exception):
                research_text = self.search_tool.run(f"{question} latest news")[:500]

            sf_prompt = self.prompts.get("auto_discovery", "superforecast")
            prompt = sf_prompt.format(
                question=question,
                description=str(m.get("description", "") or "No description available."),
                outcome_prices=(
                    ", ".join(f"{float(p):.2f}" for p in prices if p is not None) or "unavailable"
                ),
                outcomes=(", ".join(str(o) for o in (m.get("outcomes") or [])) or "YES / NO"),
                research_context=research_text or "No research available.",
                sentiment_context="No sentiment analysis was run for this market.",
            )
            response = await llm.ainvoke(
                [
                    SystemMessage(content=filter_sys),
                    HumanMessage(content=prompt),
                ]
            )

            parsed: dict = {}
            json_match = re.search(r"\{[\s\S]*?\}", response.content)
            if json_match:
                with contextlib.suppress(json.JSONDecodeError):
                    parsed = json.loads(json_match.group())

            try:
                p_yes = float(parsed.get("p_yes", market_price))
            except (TypeError, ValueError):
                p_yes = market_price
            try:
                confidence = float(parsed.get("confidence", 50))
            except (TypeError, ValueError):
                confidence = 50

            edge = abs(p_yes - market_price)
            side = "YES" if p_yes > market_price else "NO"

            forecasts.append(
                {
                    "question": question,
                    "market_price": round(market_price, 4),
                    "liquidity": round(float(m.get("liquidity", 0) or 0), 2),
                    "spread": m.get("spread"),
                    "p_yes": round(p_yes, 4),
                    "edge": round(edge, 4),
                    "side": side,
                    "confidence": confidence,
                    "reasoning": str(parsed.get("reasoning", response.content[:200])),
                    "condition_id": str(m.get("condition_id", "") or m.get("conditionId", "")),
                }
            )

        # ── Step 4: Pick the single best trade ──
        forecasts.sort(key=lambda f: f["edge"] * (f["confidence"] / 100), reverse=True)

        best_trade_prompt = self.prompts.get("auto_discovery", "best_trade")
        best_forecast = forecasts[0] if forecasts else {}
        best_price = float(best_forecast.get("market_price", 0.5))
        prompt = best_trade_prompt.format(
            prediction_json=json.dumps(forecasts[:MARKETS_TO_FORECAST], indent=2),
            question=str(best_forecast.get("question", "")),
            yes_price=f"{best_price:.2f}",
            no_price=f"{1.0 - best_price:.2f}",
            available_balance=f"{float(budget):.2f}",
            spread=str(best_forecast.get("spread") or "unavailable"),
            liquidity=f"{float(best_forecast.get('liquidity', 0) or 0):.2f}",
        )
        response = await llm.ainvoke(
            [
                SystemMessage(content=filter_sys),
                HumanMessage(content=prompt),
            ]
        )

        # Parse final recommendation
        final: dict = {}
        json_match = re.search(r"\{[\s\S]*?\}", response.content)
        if json_match:
            with contextlib.suppress(json.JSONDecodeError):
                final = json.loads(json_match.group())

        best = forecasts[0] if forecasts else {}

        return {
            "best_trade": {
                "question": str(final.get("question", best.get("question", ""))),
                "side": str(final.get("side", best.get("side", "YES"))),
                "size": float(final.get("size", min(budget * 0.1, 50))),
                "edge": float(final.get("edge", best.get("edge", 0))),
                "p_yes": float(final.get("p_yes", best.get("p_yes", 0.5))),
                "confidence": float(final.get("confidence", best.get("confidence", 50))),
                "condition_id": str(final.get("condition_id", best.get("condition_id", ""))),
                "reasoning": str(final.get("reasoning", response.content[:500])),
            },
            "all_candidates": forecasts[:MARKETS_TO_FORECAST],
            "events_evaluated": len(selected_event_keys),
            "markets_evaluated": len(candidate_markets),
            "budget": budget,
            "risk_tolerance": risk_tolerance,
            "timestamp": datetime.utcnow().isoformat(),
        }

    # ── NEW: News Article Generation ─────────────────────────

    async def generate_news_article(
        self,
        market_question: str,
        market_description: str,
        condition_id: str = "",
        yes_price: float = 0.5,
        no_price: float = 0.5,
        volume_24h: float = 0,
        end_date: str = "",
        trader_stats_json: str = "{}",
        smart_money_json: str = "{}",
        llm_override=None,
    ) -> dict[str, Any]:
        """
        Generate an AI-powered news article for a prediction market.

        Gathers multi-source research (Tavily, DuckDuckGo, NewsAPI, Binance,
        X/Twitter), combines with trader positioning data and smart money
        analysis, then prompts the LLM to produce a structured news article.
        """
        llm = self._get_task_llm("news_article", llm_override)

        # 1. Multi-source research (web + news + Binance)
        news_context = await self._research_market(market_question, market_description)

        # 2. Social / X-Twitter commentary
        social_data = ""
        try:
            x_results = await self.research_mcp._call_tool(
                "x_posts_search",
                {"query": market_question, "recency_hours": 48, "max_results": 10},
            )
            if x_results and isinstance(x_results, list):
                social_data = "\n".join(
                    f"• {r.get('title', '')} — {r.get('snippet', r.get('body', ''))}"
                    for r in x_results[:10]
                )
            elif isinstance(x_results, str):
                social_data = x_results
        except Exception as exc:
            logger.debug(f"X/Twitter search for news failed: {exc}")

        if not social_data:
            social_data = "No social media data available."

        # 3. Build prompt
        try:
            prompt_template = self.prompts.get("news_generation", "generate_article")
        except KeyError:
            prompt_template = (
                "Generate a news article about: {market_question}\n"
                "Prices: YES {yes_price}¢ / NO {no_price}¢\n"
                "Trader data: {trader_stats}\nSmart money: {smart_money_data}\n"
                "Social: {social_data}\nNews: {news_context}\n"
                "Return JSON with headline, summary, body, sentiment, confidence, "
                "key_insights, trader_behavior_summary, market_outlook, tags."
            )

        analysis_prompt = prompt_template.format(
            market_question=market_question,
            market_description=market_description or "No description available.",
            yes_price=f"{yes_price * 100:.1f}",
            no_price=f"{no_price * 100:.1f}",
            volume_24h=f"{volume_24h:,.0f}",
            end_date=end_date or "Not specified",
            trader_stats=trader_stats_json,
            smart_money_data=smart_money_json,
            social_data=social_data,
            news_context=news_context or "No external news available.",
        )

        try:
            system_prompt = self.prompts.get("news_generation", "system_prompt")
        except KeyError:
            system_prompt = (
                "You are an elite financial journalist covering Polymarket. "
                "Respond with ONLY a valid JSON object."
            )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=analysis_prompt),
        ]

        response = await llm.ainvoke(messages)
        raw = response.content

        # 4. Parse structured JSON from response
        article = self._parse_news_json(raw)
        article["market_question"] = market_question
        article["condition_id"] = condition_id
        article["generated_at"] = datetime.utcnow().isoformat()

        return article

    @staticmethod
    def _parse_news_json(raw_text: str) -> dict[str, Any]:
        """Best-effort extraction of JSON from the LLM response."""
        # Strip markdown fences
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Try to find embedded JSON object
            match = re.search(r"\{[\s\S]*\}", cleaned)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass

        # Fallback: return raw text as the body
        return {
            "headline": "Market Update",
            "summary": cleaned[:300],
            "body": cleaned,
            "sentiment": "neutral",
            "confidence": 0.5,
            "key_insights": [],
            "trader_behavior_summary": "",
            "market_outlook": "",
            "tags": [],
        }


# Singleton instance
_chain_instance: TradingAnalysisChain | None = None


def get_trading_chain() -> TradingAnalysisChain:
    """Get or create the trading analysis chain singleton."""
    global _chain_instance
    if _chain_instance is None:
        _chain_instance = TradingAnalysisChain()
    return _chain_instance


def get_chain_for_request(request_config: LLMRequestConfig | None) -> TradingAnalysisChain:
    """
    Get a trading chain configured for a specific request.

    If request_config specifies a custom provider/model, creates a new chain
    with that configuration. Otherwise returns the default singleton chain.

    Args:
        request_config: Per-request LLM configuration (can be None)

    Returns:
        Configured TradingAnalysisChain
    """
    # If no custom config, use default singleton
    if request_config is None:
        return get_trading_chain()

    # Check if any override is specified
    if (
        request_config.provider is None
        and request_config.model is None
        and request_config.temperature is None
        and request_config.max_tokens is None
    ):
        return get_trading_chain()

    # Create a custom LLM for this request
    llm = create_llm_for_request(request_config)

    # Return a new chain with the custom LLM
    return TradingAnalysisChain(llm_instance=llm)
