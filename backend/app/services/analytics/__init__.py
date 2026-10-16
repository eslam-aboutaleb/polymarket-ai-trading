"""Analytics domain: quant strategies, backtesting and reporting.

Owns arbitrage detection, strategy simulation/backtesting, execution
analytics, P&L reconciliation, leaderboard/trader quality scoring and
portfolio snapshots.

Dependency rule: may import `market_data`, `intelligence`, `platform`. Should
not import `trading` (read its outputs through repositories/events instead).
"""
