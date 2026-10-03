/**
 * Inline prompt shown when an auto-trading strategy is switched on
 * without a stored trading key.
 *
 * Renders a warning with a link to the trading-key section of the
 * Settings page, where the user can connect their key.
 *
 * @module components/TradingKeyPrompt
 */
import { Link } from "react-router-dom";

export default function TradingKeyPrompt({ strategy }: { strategy: string }) {
  return (
    <div className="p-3 rounded-lg border border-amber-500/30 bg-amber-500/10 text-sm text-amber-400">
      {strategy} requires a trading key to run automatically.{" "}
      <Link to="/settings#trading-key" className="underline font-medium">
        Connect your trading key in Settings
      </Link>{" "}
      to enable it.
    </div>
  );
}
