"""
LangChain-based trading analysis service.
Provides AI-powered market analysis using configurable LLM providers.
"""
import json
import os
import re
from typing import Dict, List, Optional, Any, AsyncIterator
from datetime import datetime

from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate, HumanMessagePromptTemplate
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import Tool
from langchain_community.tools import DuckDuckGoSearchRun
from langchain_core.callbacks import StreamingStdOutCallbackHandler

from src.config import get_settings
from src.llm_factory import create_llm, get_provider_info
from src.mcp_client import ResearchMCPClient


class PromptManager:
    """Manages loading and accessing prompts from external JSON file."""
    
    _instance = None
    _prompts: Dict = {}
    
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
            with open(prompts_path, "r") as f:
                self._prompts = json.load(f)
            print(f"Loaded prompts from {prompts_path}")
        except FileNotFoundError:
            print(f"Warning: Prompts file not found at {prompts_path}, using defaults")
            self._prompts = self._default_prompts()
    
    def _default_prompts(self) -> Dict:
        return {
            "market_analysis": {
                "system_prompt": "You are an expert prediction market analyst. Provide actionable trading insights with probability assessments.",
                "market_assessment": "Analyze this market: {market_title}\nDescription: {market_description}\nYes: ${yes_price}, No: ${no_price}\nVolume: ${volume_24h}\nEnd: {end_date}\n\nContext:\n{news_context}",
                "quick_analysis": "Quick analysis: {question} at ${current_price}",
                "multi_market_scan": "Scan markets:\n{markets_json}"
            },
            "risk_management": {
                "risk_assessment": "Risk for {market_title}: Size ${position_size}, Entry ${entry_price}, {days_to_expiry} days"
            },
            "trading": {
                "trade_execution_plan": "Plan for {action} on {market_title}: Target ${target_size} at ${current_price}"
            }
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
    
    def __init__(self, streaming: bool = False):
        settings = get_settings()
        self.prompts = PromptManager()
        
        # Initialize LLM using factory (provider-agnostic)
        callbacks = [StreamingStdOutCallbackHandler()] if streaming else None
        self.llm = create_llm(streaming=streaming, callbacks=callbacks)
        
        # Log provider info
        provider_info = get_provider_info()
        print(f"Initialized LLM: {provider_info['provider']} / {provider_info['model']}")
        
        # Initialize search tool
        self.search_tool = DuckDuckGoSearchRun()
        
        # Initialize tools list
        self.tools = [
            Tool(
                name="web_search",
                description="Search the web for recent news and information about prediction markets.",
                func=self.search_tool.run
            ),
        ]
        self.research_mcp = ResearchMCPClient()
    
    async def analyze_market(
        self,
        market_title: str,
        market_description: str,
        yes_price: float,
        no_price: float,
        volume_24h: float,
        end_date: str,
        include_research: bool = True
    ) -> Dict[str, Any]:
        """Perform comprehensive market analysis with optional web research."""
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
            news_context=news_context or "No additional research performed."
        )
        
        system_prompt = self.prompts.get("market_analysis", "system_prompt")
        
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=analysis_prompt)
        ]
        
        response = await self.llm.ainvoke(messages)
        
        return {
            "analysis": response.content,
            "research_context": news_context,
            "timestamp": datetime.utcnow().isoformat()
        }
    
    async def analyze_market_stream(
        self,
        market_title: str,
        market_description: str,
        yes_price: float,
        no_price: float,
        volume_24h: float,
        end_date: str,
        include_research: bool = True
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream market analysis for real-time updates."""
        # First yield research phase
        if include_research:
            yield {"chunk": "Researching market...", "chunk_type": "research", "is_final": False}
            news_context = await self._research_market(market_title, market_description)
            yield {"chunk": f"Research complete.\n\n{news_context[:500]}...", "chunk_type": "research", "is_final": False}
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
            news_context=news_context or "No research."
        )
        
        system_prompt = self.prompts.get("market_analysis", "system_prompt")
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=analysis_prompt)
        ]
        
        # Stream the response
        async for chunk in self.llm.astream(messages):
            yield {"chunk": chunk.content, "chunk_type": "analysis", "is_final": False}
        
        yield {"chunk": "", "chunk_type": "recommendation", "is_final": True}
    
    async def _research_market(self, market_title: str, description: str) -> str:
        """Use web search to gather current information."""
        try:
            queries = [
                f"{market_title} latest news",
                f"{market_title} prediction analysis"
            ]
            
            results = []
            for query in queries[:2]:
                try:
                    result = self.search_tool.run(query)
                    results.append(f"Query: {query}\nResults: {result}\n")
                except Exception as e:
                    print(f"Search error for '{query}': {e}")
            
            return "\n---\n".join(results) if results else "No search results available."
        except Exception as e:
            print(f"Research error: {e}")
            return "Research unavailable."
    
    async def quick_analysis(self, question: str, current_price: float) -> str:
        """Quick 2-3 sentence analysis."""
        prompt_template = self.prompts.get("market_analysis", "quick_analysis")
        prompt = prompt_template.format(question=question, current_price=current_price)
        
        messages = [HumanMessage(content=prompt)]
        response = await self.llm.ainvoke(messages)
        return response.content
    
    async def scan_markets(self, markets: List[Dict]) -> Dict[str, Any]:
        """Scan multiple markets for opportunities."""
        prompt_template = self.prompts.get("market_analysis", "multi_market_scan")
        prompt = prompt_template.format(markets_json=json.dumps(markets, indent=2))
        
        system_prompt = self.prompts.get("market_analysis", "system_prompt")
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=prompt)
        ]
        
        response = await self.llm.ainvoke(messages)
        return {
            "analysis": response.content,
            "markets_scanned": len(markets),
            "timestamp": datetime.utcnow().isoformat()
        }
    
    async def assess_risk(
        self,
        market_title: str,
        position_size: float,
        entry_price: float,
        days_to_expiry: int,
        correlation_info: str = "No correlation data"
    ) -> Dict[str, Any]:
        """Assess risk for a potential trade."""
        prompt_template = self.prompts.get("risk_management", "risk_assessment")
        prompt = prompt_template.format(
            market_title=market_title,
            position_size=position_size,
            entry_price=entry_price,
            days_to_expiry=days_to_expiry,
            correlation_info=correlation_info
        )
        
        messages = [HumanMessage(content=prompt)]
        response = await self.llm.ainvoke(messages)
        
        return {
            "analysis": response.content,
            "timestamp": datetime.utcnow().isoformat()
        }
    
    async def generate_trade_plan(
        self,
        action: str,
        market_title: str,
        target_size: float,
        current_price: float,
        order_book: Dict = None
    ) -> Dict[str, Any]:
        """Generate a trade execution plan."""
        prompt_template = self.prompts.get("trading", "trade_execution_plan")
        prompt = prompt_template.format(
            action=action,
            market_title=market_title,
            target_size=target_size,
            current_price=current_price,
            order_book=json.dumps(order_book) if order_book else "N/A"
        )
        
        messages = [HumanMessage(content=prompt)]
        response = await self.llm.ainvoke(messages)
        
        return {
            "analysis": response.content,
            "timestamp": datetime.utcnow().isoformat()
        }

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
    ) -> Dict[str, Any]:
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
                    print(f"Opportunity search error for '{q}': {e}")
            search_findings = "\n---\n".join(results) if results else "No search results available."
        except Exception as e:
            search_findings = f"Search unavailable: {e}"

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
            smart_money_context=smart_money_context[:1500],
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

        response = await self.llm.ainvoke(messages)
        raw = response.content.strip()

        # Parse JSON from LLM response
        import re
        json_match = re.search(r'\{[\s\S]*\}', raw)
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
            "recommended_side": scored.get("recommended_side", "YES" if yes_price < no_price else "NO"),
            "reasoning": scored.get("reasoning", raw[:300]),
            "search_summary": scored.get("search_summary", search_findings[:200]),
            "key_risks": scored.get("key_risks", []),
            "search_findings": search_findings[:500],
            "timestamp": datetime.utcnow().isoformat(),
        }

    async def scan_event_opportunity(
        self,
        event_title: str,
        sub_markets: List[Dict[str, Any]],
        event_volume: str = "0",
        event_liquidity: str = "0",
        smart_money_context: str = "",
    ) -> Dict[str, Any]:
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
                    print(f"Event search error for '{q}': {e}")
            search_findings = "\n---\n".join(results) if results else "No search results available."
        except Exception as e:
            search_findings = f"Search unavailable: {e}"

        # 2. Build sub-markets table (markdown-style)
        table_lines = ["| Option | YES Price | NO Price | 24h Volume | Liquidity |",
                       "|--------|-----------|----------|------------|-----------|"]
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
            smart_money_context=smart_money_context[:1500],
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

        response = await self.llm.ainvoke(messages)
        raw = response.content.strip()

        # Parse JSON
        import re
        json_match = re.search(r'\{[\s\S]*\}', raw)
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
    ) -> Dict[str, Any]:
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
                    print(f"Trader search error for '{q}': {e}")
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
        response = await self.llm.ainvoke(messages)

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
    ) -> Dict[str, Any]:
        """Evaluate whether to copy a specific trade – returns recommendation."""
        # Quick web search on the market
        market_research = ""
        if market_title or market_id:
            try:
                query = f"{market_title or market_id} polymarket prediction"
                market_research = self.search_tool.run(query)
            except Exception:
                market_research = "No market research available."

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

        system_prompt = self.prompts.get("trader_analysis", "system_prompt")
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"{prompt}\n\nMarket Research:\n{market_research}"),
        ]
        response = await self.llm.ainvoke(messages)

        # Parse recommendation from LLM output
        content = response.content.upper()
        if "SKIP" in content or "AVOID" in content:
            recommendation = "avoid"
        elif "REDUCE" in content:
            recommendation = "reduce_size"
        else:
            recommendation = "copy"

        # Try to extract confidence
        confidence = 50.0
        import re
        conf_match = re.search(r'confidence[:\s]*(\d+)', content, re.IGNORECASE)
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
    ) -> Dict[str, Any]:
        """Evaluate whether an open position should be reversed."""
        try:
            alternatives = json.loads(alternatives_json or "[]")
            if not isinstance(alternatives, list):
                alternatives = []
        except Exception:
            alternatives = []

        web_summary = ""
        x_summary = ""
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
                "Return JSON with: recommendation, confidence, reasoning, key_risks, alt_outcome, alt_token_id, web_summary, x_summary."
            )

        system_prompt = self.prompts.get("inverse_position_bot", "system_prompt")
        if not system_prompt:
            system_prompt = (
                "You are a conservative Polymarket risk engine. "
                "Respond with only valid JSON."
            )

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
        )

        response = await self.llm.ainvoke(
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
        if not x_results:
            confidence = min(confidence, 70.0)

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


# Singleton instance
_chain_instance: Optional[TradingAnalysisChain] = None


def get_trading_chain() -> TradingAnalysisChain:
    """Get or create the trading analysis chain singleton."""
    global _chain_instance
    if _chain_instance is None:
        _chain_instance = TradingAnalysisChain()
    return _chain_instance
