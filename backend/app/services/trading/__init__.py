"""Trading execution domain: order lifecycle, bots and monitors.

Owns copy-trading, stop-loss/take-profit monitors, market maker, inverse bot,
latency arbitrage, pre-trade gating and position lifecycle. Money-path logic
lives here; anything a human or bot executes on Polymarket runs through this
domain.

Dependency rule: may import `market_data`, `users`, `intelligence`,
`analytics`, `platform`. Must not import `api`.
"""
