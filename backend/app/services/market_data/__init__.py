"""Market data layer: external venue/exchange integrations.

Owns every outbound market-data client (Polymarket CLOB/gamma/web3, Binance,
CTF on-chain events) and the venue adapter registry consumer. This is the only
domain allowed to talk to prediction-market and crypto data sources; other
domains receive normalized data through it.

Dependency rule: may import `venues`, `platform`, `intelligence` (for LLM
enrichment). Nothing in this domain writes user-owned business state.
"""
