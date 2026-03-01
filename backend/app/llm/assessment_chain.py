"""
LangChain assessment chains for AI trade analysis.
Uses external prompts from prompts.json for all LLM interactions.
"""
import json
import os
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime

from langchain_openai import ChatOpenAI
from langchain.prompts import ChatPromptTemplate, SystemMessagePromptTemplate, HumanMessagePromptTemplate
from langchain.schema import HumanMessage, SystemMessage
from langchain.tools import Tool
from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain_community.tools import DuckDuckGoSearchRun
from langchain.callbacks.manager import CallbackManager
from langchain.callbacks.streaming_stdout import StreamingStdOutCallbackHandler

from pydantic import BaseModel, Field
from app.config import get_settings
from app.utils.time import utc_now

logger = logging.getLogger(__name__)


class MarketAssessment(BaseModel):
    """Structured output for market assessment."""
    probability: float = Field(..., description="Probability assessment 0-100")
    confidence: str = Field(..., description="Confidence level: low/medium/high")
    recommendation: str = Field(..., description="buy_yes, buy_no, or hold")
    position_size_pct: float = Field(..., description="Suggested position as % of max")
    reasoning: str = Field(..., description="Key factors supporting analysis")
    risk_factors: List[str] = Field(..., description="Risk factors to monitor")


class ResearchQuery(BaseModel):
    """Structured output for research queries."""
    queries: List[str] = Field(..., description="Search queries to execute")


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
        prompts_path = os.environ.get("PROMPTS_PATH", "prompts/prompts.json")
        try:
            with open(prompts_path, "r") as f:
                self._prompts = json.load(f)
        except FileNotFoundError:
            logger.warning("Prompts file not found at %s", prompts_path)
            self._prompts = {}
    
    def get(self, category: str, prompt_name: str) -> str:
        """Get a prompt by category and name."""
        if category in self._prompts and prompt_name in self._prompts[category]:
            return self._prompts[category][prompt_name]
        raise KeyError(f"Prompt not found: {category}.{prompt_name}")
    
    def reload(self):
        """Reload prompts from file."""
        self._load_prompts()


class TradingAnalysisChain:
    """
    LangChain-based trading analysis with web search capabilities.
    Uses external prompts and supports MCP tool integration.
    """
    
    def __init__(self, streaming: bool = False):
        settings = get_settings()
        self.prompts = PromptManager()
        
        # Initialize LLM
        callbacks = [StreamingStdOutCallbackHandler()] if streaming else None
        self.llm = ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            temperature=0.3,  # Lower temperature for more consistent analysis
            callbacks=callbacks
        )
        
        # Initialize search tool for research
        self.search_tool = DuckDuckGoSearchRun()
        
        # Initialize tools list
        self.tools = [
            Tool(
                name="web_search",
                description="Search the web for recent news, data, and information about prediction markets, politics, sports, or any topic relevant to market analysis.",
                func=self.search_tool.run
            ),
        ]
        
        # Create agent for research tasks
        self._setup_research_agent()
    
    def _setup_research_agent(self):
        """Set up the research agent with tools."""
        system_prompt = self.prompts.get("market_analysis", "system_prompt")
        
        prompt = ChatPromptTemplate.from_messages([
            SystemMessagePromptTemplate.from_template(system_prompt + "\n\nYou have access to web search to find current information."),
            HumanMessagePromptTemplate.from_template("{input}"),
            ("placeholder", "{agent_scratchpad}")
        ])
        
        agent = create_openai_tools_agent(self.llm, self.tools, prompt)
        self.research_agent = AgentExecutor(
            agent=agent,
            tools=self.tools,
            verbose=True,
            max_iterations=5,
            handle_parsing_errors=True
        )
    
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
        """
        Perform comprehensive market analysis with optional web research.
        
        Args:
            market_title: Title of the prediction market
            market_description: Full market description
            yes_price: Current YES token price
            no_price: Current NO token price
            volume_24h: 24-hour trading volume
            end_date: Market resolution date
            include_research: Whether to include web research
        
        Returns:
            Dict containing analysis results
        """
        news_context = ""
        
        # Optionally perform web research first
        if include_research:
            news_context = await self._research_market(market_title, market_description)
        
        # Format the analysis prompt
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
        
        # Get system prompt
        system_prompt = self.prompts.get("market_analysis", "system_prompt")
        
        # Run analysis
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=analysis_prompt)
        ]
        
        response = await self.llm.ainvoke(messages)
        
        return {
            "market_title": market_title,
            "analysis": response.content,
            "research_context": news_context,
            "analyzed_at": utc_now().isoformat(),
            "prices": {
                "yes": yes_price,
                "no": no_price
            }
        }
    
    async def _research_market(self, market_title: str, description: str) -> str:
        """
        Use web search to gather current information about a market.
        """
        try:
            # Generate search queries
            query_prompt = self.prompts.get("research", "news_search_query")
            query_request = query_prompt.format(
                market_question=market_title,
                category="general",
                time_horizon="near-term"
            )
            
            # Get search queries from LLM
            messages = [
                SystemMessage(content="Generate search queries for market research. Return only the queries, one per line."),
                HumanMessage(content=query_request)
            ]
            query_response = await self.llm.ainvoke(messages)
            queries = query_response.content.strip().split("\n")[:3]  # Limit to 3 queries
            
            # Execute searches
            results = []
            for query in queries:
                query = query.strip().strip("1234567890.-) ")
                if query:
                    try:
                        result = self.search_tool.run(query)
                        results.append(f"Query: {query}\nResults: {result}\n")
                    except Exception as e:
                        logger.warning("Search error for query '%s': %s", query, e)
            
            return "\n---\n".join(results) if results else "No search results available."
        except Exception as e:
            logger.error("Research error: %s", e, exc_info=True)
            return "Research unavailable."
    
    async def quick_analysis(self, question: str, current_price: float) -> str:
        """
        Perform quick 2-3 sentence analysis of a market.
        """
        prompt_template = self.prompts.get("market_analysis", "quick_analysis")
        prompt = prompt_template.format(
            question=question,
            current_price=current_price
        )
        
        messages = [HumanMessage(content=prompt)]
        response = await self.llm.ainvoke(messages)
        return response.content
    
    async def scan_markets(self, markets: List[Dict]) -> Dict[str, Any]:
        """
        Scan multiple markets for opportunities.
        """
        prompt_template = self.prompts.get("market_analysis", "multi_market_scan")
        prompt = prompt_template.format(
            markets_json=json.dumps(markets, indent=2)
        )
        
        system_prompt = self.prompts.get("market_analysis", "system_prompt")
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=prompt)
        ]
        
        response = await self.llm.ainvoke(messages)
        return {
            "scan_results": response.content,
            "markets_scanned": len(markets),
            "scanned_at": utc_now().isoformat()
        }
    
    async def assess_risk(
        self,
        market_title: str,
        position_size: float,
        entry_price: float,
        days_to_expiry: int,
        correlation_info: str = "No correlation data"
    ) -> Dict[str, Any]:
        """
        Assess risk for a potential trade.
        """
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
            "market": market_title,
            "risk_assessment": response.content,
            "assessed_at": utc_now().isoformat()
        }
    
    async def generate_trade_plan(
        self,
        action: str,
        market_title: str,
        target_size: float,
        current_price: float,
        order_book: Dict = None
    ) -> Dict[str, Any]:
        """
        Generate a trade execution plan.
        """
        prompt_template = self.prompts.get("trading", "trade_execution_plan")
        prompt = prompt_template.format(
            action=action,
            market_title=market_title,
            target_size=target_size,
            current_price=current_price,
            order_book=json.dumps(order_book) if order_book else "Order book data unavailable"
        )
        
        messages = [HumanMessage(content=prompt)]
        response = await self.llm.ainvoke(messages)
        
        return {
            "action": action,
            "market": market_title,
            "execution_plan": response.content,
            "generated_at": utc_now().isoformat()
        }


# Singleton instance
_chain_instance: Optional[TradingAnalysisChain] = None


def get_trading_chain() -> TradingAnalysisChain:
    """Get or create the trading analysis chain singleton."""
    global _chain_instance
    if _chain_instance is None:
        _chain_instance = TradingAnalysisChain()
    return _chain_instance
