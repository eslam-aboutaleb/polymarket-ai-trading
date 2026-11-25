"""Event-driven research backtesting, shadow runs and strategy laboratory (#2).

Pure, deterministic and side-effect free. Nothing in this package imports a
signer, wallet, order gateway, network client or database session, so a
simulated or shadow run cannot place or sign an order. Results are modeled
research output, never exchange-confirmed execution.
"""
