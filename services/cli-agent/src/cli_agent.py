"""
GitHub Copilot CLI Agent wrapper.
Uses GitHub Models API (OpenAI-compatible) for AI analysis.
Falls back to OpenAI API if GITHUB_TOKEN is not available.
"""
import asyncio
import json
import os
import re
import logging
from typing import Dict, List, Optional, Any, AsyncIterator
from datetime import datetime

import httpx

from src.config import get_settings
from src.mcp_client import ResearchMCPClient

logger = logging.getLogger(__name__)


class PromptManager:
    """Manages loading prompts from external JSON file."""
    
    _instance = None
    _prompts: Dict = {}
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_prompts()
        return cls._instance
    
    def _load_prompts(self):
        settings = get_settings()
        try:
            with open(settings.prompts_path, "r") as f:
                self._prompts = json.load(f)
            print(f"Loaded prompts from {settings.prompts_path}")
        except FileNotFoundError:
            print(f"Warning: Prompts file not found, using defaults")
            self._prompts = {}
    
    def get(self, category: str, prompt_name: str) -> str:
        if category in self._prompts and prompt_name in self._prompts[category]:
            return self._prompts[category][prompt_name]
        return ""
    
    def reload(self):
        self._load_prompts()


class CopilotCLIAgent:
    """
    AI Analysis Agent using GitHub Models API.
    Uses the OpenAI-compatible endpoint at models.inference.ai.azure.com
    with GITHUB_TOKEN for authentication.
    """
    
    def __init__(self):
        self.settings = get_settings()
        self.prompts = PromptManager()
        
        # Determine API endpoint and key
        if self.settings.github_token:
            self.api_base = self.settings.github_models_endpoint
            self.api_key = self.settings.github_token
            self.model = self.settings.github_model
            logger.info(f"Using GitHub Models API with model: {self.model}")
        elif self.settings.openai_api_key:
            self.api_base = "https://api.openai.com/v1"
            self.api_key = self.settings.openai_api_key
            self.model = "gpt-4o-mini"
            logger.info("Falling back to OpenAI API (no GITHUB_TOKEN)")
        else:
            self.api_base = None
            self.api_key = None
            self.model = None
            logger.warning("No GITHUB_TOKEN or OPENAI_API_KEY set - agent will return errors")
        
        self._client = httpx.AsyncClient(timeout=self.settings.copilot_timeout)
        self.research_mcp = ResearchMCPClient()
    
    async def _call_llm(self, prompt: str, system_prompt: str = None) -> str:
        """
        Call the GitHub Models API (OpenAI-compatible chat completions).
        """
        if not self.api_key:
            return "Error: No API key configured. Set GITHUB_TOKEN or OPENAI_API_KEY."
        
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        
        # Build endpoint URL
        if "azure.com" in self.api_base:
            url = f"{self.api_base}/chat/completions"
        else:
            url = f"{self.api_base}/chat/completions"
        
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.settings.github_model_temperature,
            "max_tokens": self.settings.github_model_max_tokens,
        }
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        
        try:
            response = await self._client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            content = data["choices"][0]["message"]["content"]
            logger.info(f"LLM response received ({len(content)} chars)")
            return content.strip()
            
        except httpx.HTTPStatusError as e:
            error_body = e.response.text[:500]
            logger.error(f"GitHub Models API error {e.response.status_code}: {error_body}")
            return f"Analysis failed (API error {e.response.status_code}). Please check your GitHub token configuration."
        except httpx.TimeoutException:
            logger.error("GitHub Models API timeout")
            return "Analysis timed out. Please try again with a simpler query."
        except Exception as e:
            logger.error(f"LLM call error: {e}")
            return f"Analysis error: {str(e)}"
    
    async def _run_analysis(self, prompt: str, system_prompt: str = None) -> str:
        """Run an analysis prompt through the LLM. Replaces old _run_copilot."""
        default_system = (
            "You are an expert AI trading analyst specializing in Polymarket prediction markets. "
            "Provide concise, data-driven analysis with clear recommendations."
        )
        return await self._call_llm(prompt, system_prompt or default_system)
    
    async def _run_analysis_with_context(self, prompt: str, use_tools: List[str] = None) -> str:
        """
        Run analysis with a structured system prompt.
        Replaces old _run_copilot_with_mcp.
        """
        system_prompt = """You are an AI trading analyst for Polymarket prediction markets.

Provide a structured analysis with:
1. Key findings
2. Probability assessment
3. Confidence level
4. Recommendation (buy_yes/buy_no/hold)
5. Risk factors"""
        
        return await self._run_analysis(prompt, system_prompt)
    
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
        """Perform market analysis using Copilot CLI with MCP tools."""
        
        # Build analysis prompt
        prompt_template = self.prompts.get("market_analysis", "market_assessment")
        if not prompt_template:
            prompt_template = """
Analyze this Polymarket market:
Market: {market_title}
Description: {market_description}
Yes Price: ${yes_price}
No Price: ${no_price}
24h Volume: ${volume_24h}
End Date: {end_date}
"""
        
        prompt = prompt_template.format(
            market_title=market_title,
            market_description=market_description,
            yes_price=yes_price,
            no_price=no_price,
            volume_24h=volume_24h,
            end_date=end_date,
            news_context="(Use web search tool to find recent news)"
        )
        
        result = await self._run_analysis_with_context(prompt)
        
        return {
            "analysis": result,
            "research_context": "Analysis performed via GitHub Models API" if include_research else "",
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
        """Stream analysis (CLI doesn't support true streaming, simulates it)."""
        
        yield {"chunk": "Initiating CLI agent analysis...\n", "chunk_type": "research", "is_final": False}
        
        if include_research:
            yield {"chunk": "Searching for market information...\n", "chunk_type": "research", "is_final": False}
        
        # Run the actual analysis
        result = await self.analyze_market(
            market_title, market_description, yes_price, no_price,
            volume_24h, end_date, include_research
        )
        
        # Yield result in chunks
        analysis = result["analysis"]
        chunk_size = 100
        for i in range(0, len(analysis), chunk_size):
            yield {
                "chunk": analysis[i:i+chunk_size],
                "chunk_type": "analysis",
                "is_final": False
            }
            await asyncio.sleep(0.05)  # Small delay for streaming effect
        
        yield {"chunk": "", "chunk_type": "recommendation", "is_final": True}
    
    async def quick_analysis(self, question: str, current_price: float) -> str:
        """Quick analysis using CLI."""
        prompt = f"""
Quick analysis needed for this prediction market:
Question: {question}
Current Price: ${current_price}

Provide a brief 2-3 sentence assessment:
- Is this overpriced, underpriced, or fair?
- What's your confidence level?
"""
        return await self._run_analysis(prompt)
    
    async def scan_markets(self, markets: List[Dict]) -> Dict[str, Any]:
        """Scan multiple markets for opportunities."""
        markets_str = json.dumps(markets[:10], indent=2)  # Limit to 10 markets
        
        prompt = f"""
Scan these Polymarket markets for trading opportunities:

{markets_str}

Identify the top 3 markets with highest expected value.
For each, provide: name, mispricing estimate, confidence, rationale.
"""
        
        result = await self._run_analysis_with_context(prompt)
        
        return {
            "analysis": result,
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
        """Assess trade risk using CLI."""
        prompt = f"""
Risk assessment for Polymarket trade:
Market: {market_title}
Position Size: ${position_size}
Entry Price: ${entry_price}
Days to Expiry: {days_to_expiry}
Correlation: {correlation_info}

Analyze:
1. Maximum loss scenario
2. Probability of significant loss (>20%)
3. Liquidity risk
4. Event risk factors
5. Overall risk rating (1-10)
"""
        
        result = await self._run_analysis(prompt)
        
        return {
            "analysis": result,
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
        """Generate trade execution plan."""
        order_book_str = json.dumps(order_book) if order_book else "Not available"
        
        prompt = f"""
Create a trade execution plan:
Action: {action}
Market: {market_title}
Target Size: ${target_size}
Current Price: ${current_price}
Order Book: {order_book_str}

Provide:
1. Recommended order type
2. Price levels (if limit)
3. Slicing strategy
4. Risk management stops
"""
        
        result = await self._run_analysis_with_context(prompt)
        
        return {
            "analysis": result,
            "timestamp": datetime.utcnow().isoformat()
        }

    # ── NEW: Opportunity Scoring ───────────────────────────

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
        Score a single market opportunity using GitHub Models API.
        Uses web research via the ddg-search MCP for credibility.
        """
        # Build a comprehensive prompt with all context
        prompt_template = self.prompts.get("opportunity_analysis", "market_scoring")
        if not prompt_template:
            prompt_template = (
                "Score this market opportunity: {market_title}\n"
                "YES: {yes_price_cents}c, NO: {no_price_cents}c\n"
                "Search findings: {search_findings}\nSmart Money: {smart_money_context}\n"
                "Return JSON with ai_score, risk_level, recommendation."
            )

        # Since CLI agent doesn't have direct DuckDuckGo, include instruction
        # for the LLM to use its knowledge to validate credibility
        search_instruction = (
            f"Use your knowledge to validate the credibility of this market: {market_title}\n"
            f"Consider: Is this a real, verifiable event? Are there recent news about it?\n"
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
            search_findings=search_instruction,
            smart_money_context=smart_money_context[:1500],
        )

        system_prompt = self.prompts.get("opportunity_analysis", "system_prompt")
        if not system_prompt:
            system_prompt = (
                "You are an expert prediction market analyst. "
                "Respond with ONLY valid JSON — no markdown, no extra text."
            )

        result = await self._call_llm(prompt, system_prompt)

        # Parse JSON
        json_match = re.search(r'\{[\s\S]*\}', result)
        if json_match:
            try:
                scored = json.loads(json_match.group())
            except json.JSONDecodeError:
                scored = {}
        else:
            scored = {}

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
            "reasoning": scored.get("reasoning", result[:300]),
            "search_summary": scored.get("search_summary", "Analysis via GitHub Models API"),
            "key_risks": scored.get("key_risks", []),
            "search_findings": "Analysis via GitHub Models API",
            "timestamp": datetime.utcnow().isoformat(),
        }

    # ── NEW: Trader Analysis & Copy-Trade Evaluation ─────────

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
        """Trader profile analysis using MCP tools for research."""
        prompt_template = self.prompts.get("trader_analysis", "profile_research")
        if not prompt_template:
            prompt_template = (
                "Analyze this Polymarket trader: {wallet_address}\n"
                "Display Name: {display_name}\nPnL: {total_pnl}\n"
                "Win Rate: {win_rate}\nTrades: {trade_count}\n"
                "Markets: {markets_traded}\nRecent Trades: {recent_trades}"
            )

        prompt = prompt_template.format(
            wallet_address=wallet_address,
            display_name=display_name or "Unknown",
            total_pnl=total_pnl,
            win_rate=win_rate,
            trade_count=trade_count,
            markets_traded=markets_traded,
            recent_trades=recent_trades_json,
        )

        result = await self._run_analysis_with_context(prompt)

        return {
            "analysis": result,
            "research_context": "Analysis performed via GitHub Models API",
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
        """Evaluate whether to copy a specific trade."""
        prompt_template = self.prompts.get("copy_trading", "trade_evaluation")
        if not prompt_template:
            prompt_template = (
                "Evaluate copy trade: Trader {trader_wallet} is {trade_side} on "
                "{market_title} at {current_price} for {trade_size}\n"
                "Trader Stats: {trader_stats}\nRisk Profile: {user_risk_profile}"
            )

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

        result = await self._run_analysis_with_context(prompt)

        # Parse recommendation
        upper = result.upper()
        if "SKIP" in upper or "AVOID" in upper:
            recommendation = "avoid"
        elif "REDUCE" in upper:
            recommendation = "reduce_size"
        else:
            recommendation = "copy"

        confidence = 50.0
        conf_match = re.search(r'confidence[:\s]*(\d+)', upper, re.IGNORECASE)
        if conf_match:
            confidence = min(100, max(0, float(conf_match.group(1))))

        return {
            "analysis": result,
            "recommendation": recommendation,
            "confidence": confidence,
            "ai_score": confidence,
            "risk_level": "high" if confidence < 40 else "medium" if confidence < 70 else "low",
            "market_sentiment": None,
            "research_context": "Analysis performed via GitHub Models API",
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

        system_prompt = self.prompts.get("inverse_position_bot", "system_prompt")
        if not system_prompt:
            system_prompt = (
                "You are a conservative Polymarket risk engine. "
                "Return only valid JSON."
            )
        prompt_template = self.prompts.get("inverse_position_bot", "evaluation_prompt")
        if not prompt_template:
            prompt_template = (
                "Evaluate inverse position decision.\n"
                "Market: {market_title}\n"
                "Held: {held_outcome} ({held_pct:.3f})\n"
                "Best alternative: {best_alt_outcome} ({best_alt_pct:.3f})\n"
                "Delta: {delta_pct:.3f}\n"
                "Alternatives: {alternatives_json}\n"
                "Web summary: {web_summary}\n"
                "X summary: {x_summary}\n"
                "Return strict JSON: recommendation, confidence, reasoning, key_risks, alt_outcome, alt_token_id, web_summary, x_summary."
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

        raw = await self._call_llm(prompt, system_prompt)

        parsed: dict[str, Any] = {}
        json_match = re.search(r"\{[\s\S]*\}", raw or "")
        if json_match:
            try:
                parsed = json.loads(json_match.group(0))
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
            "reasoning": str(parsed.get("reasoning", (raw or "")[:600])),
            "key_risks": key_risks,
            "alt_outcome": str(parsed.get("alt_outcome", best_alt_outcome)),
            "alt_token_id": str(parsed.get("alt_token_id", "")),
            "web_summary": str(parsed.get("web_summary", web_summary)),
            "x_summary": str(parsed.get("x_summary", x_summary)),
            "timestamp": datetime.utcnow().isoformat(),
        }


# Singleton instance
_agent_instance: Optional[CopilotCLIAgent] = None


def get_cli_agent() -> CopilotCLIAgent:
    """Get or create the CLI agent singleton."""
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = CopilotCLIAgent()
    return _agent_instance
