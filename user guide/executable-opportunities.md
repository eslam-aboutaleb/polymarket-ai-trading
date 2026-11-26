# Executable opportunities (research only)

Enable with `EXECUTABLE_OPPORTUNITIES_ENABLED=true`. Read-only: nothing here places an order.

`GET /api/opportunities` ranks stored AI forecasts by edge after the executable VWAP at your
size, fees and slippage, for both YES and NO. Stored verdicts have no uncertainty interval,
calibration status or rule hash, so rows are INDICATIVE. `POST /api/opportunities/evaluate`
takes an explicit forecast; it is VERIFIED only with a full interval, a positive conservative
edge, a calibrated forecast, a matching rule hash and full depth. Stale forecasts or books,
abstained forecasts and missing fee inputs are not ranked. Edges are never guaranteed profit.

Not built: persistence, `/{id}`, `/evidence`, `/simulate`, copy and arbitrage sources.
