# LLM Chain Service

LangChain-based AI analysis service for Polymarket trading.

## Features

- gRPC service for market analysis
- Multi-provider support: OpenAI, Anthropic, Google, Groq, Ollama
- **Binance Skills Hub integration** — smart money signals, social hype, trending tokens, and token market data enriched into all AI analysis chains
- Configurable via environment variables

## Binance Skills Integration

The analysis chain automatically gathers Binance smart money data for crypto-related markets via the MCP research server. This enriches the following analysis methods:

| Method                        | What Binance Data Adds                                                                                         |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `_research_market()`          | Smart money signals + social hype + trending tokens appended to research context                               |
| `evaluate_inverse_position()` | Binance context in prompt; confidence cap relaxed when Binance data compensates for missing X/Twitter evidence |
| `evaluate_copy_trade()`       | Smart money context appended alongside web research                                                            |
| `scan_opportunity()`          | Binance data merged into `smart_money_context` parameter                                                       |
| `scan_event_opportunity()`    | Same as above for event-level scoring                                                                          |
| `analyze_sentiment()`         | Binance social hype & smart money signals as additional evidence source                                        |

### Configuration

```env
BINANCE_SKILLS_ENABLED=true  # default; set false to disable
```

No API key required — all Binance Skills Hub APIs are public.
