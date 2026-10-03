/**
 * Hook that tracks whether the session user has a trading key
 * stored (`GET /api/auth/trading-key`).
 *
 * Auto-trading strategies (stop-loss, inverse bot, latency arb)
 * require a trading key; consumers use `hasTradingKey` to gate
 * their enable toggles and prompt the user to connect one.
 * `hasTradingKey` is `null` while loading or when the status
 * could not be fetched.
 *
 * @module hooks/useTradingKeyStatus
 */

import { useCallback, useEffect, useState } from "react";
import { getTradingKeyStatus } from "../services/tradingKeyService";

export interface TradingKeyStatusState {
  hasTradingKey: boolean | null;
  loading: boolean;
  refresh: () => Promise<void>;
}

export function useTradingKeyStatus(): TradingKeyStatusState {
  const [hasTradingKey, setHasTradingKey] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const status = await getTradingKeyStatus();
      setHasTradingKey(status.has_trading_key);
    } catch {
      setHasTradingKey(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { hasTradingKey, loading, refresh };
}
