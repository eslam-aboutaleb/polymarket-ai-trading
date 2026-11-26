# Forecast sizing gate (issue #159)

No skill is assumed until it is measured. An LLM probability may drive Kelly sizing for a
category only when the out-of-sample paired Brier delta against the matched market price has
a 95% event-cluster interval that excludes zero in the model's favour, on at least 30
independent events, and no drift flag is raised for the models in that category.

- `category_reports` scores each category (`PredictionScoreInput.group`) with the #22 scorer.
- `decide` returns `kelly`, `flat_tiny` (opt in, minimum notional only) or `none`.
- `fit_shrinkage_weight` fits `w` in `m + w(p - m)` on past matched rows, clamped to [0, 1];
  with no data it is 0 (use the market price).
- `size_with_gate` sizes on net edge: Kelly sees `price + fee_per_share` as the cost.
- A drift flag disables the category even when skill was previously shown.

Status: the module returns a decision only. It is not yet wired into `copy_trade_service` or
the manual-trade sizing route, which keeps live sizing unchanged until that wiring is
reviewed. `pre_trade_gate.py` and `live_trading_policy.py` are untouched.
