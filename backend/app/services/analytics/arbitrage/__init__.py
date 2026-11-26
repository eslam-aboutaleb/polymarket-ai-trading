"""Read-only, payoff-verified arbitrage feasibility engine (issue #9).

Detection is separate from execution. Nothing in this package signs, submits,
cancels or sizes a real order; ``paper_only`` is always true and live
execution stays behind the existing pre-trade gate and live trading policy.
"""

ENGINE_VERSION = "arb-feasibility-v1"
