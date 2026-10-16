/**
 * Wallet portfolio summary (balances + positions) via react-query.
 *
 * The price stream and the polling fallback keep the cached summary warm by
 * writing live prices into the same query key, so one `usePortfolio` observer
 * sees both the fetched summary and the tick-driven updates.
 *
 * @module hooks/usePortfolio
 */

import { useQuery } from "react-query";
import { portfolioService, PortfolioSummary } from "../services/portfolioService";

/** Query key of the portfolio summary (also used by live price updates). */
export const portfolioSummaryQueryKey = ["portfolio", "summary"] as const;

export interface UsePortfolioOptions {
  /** Set false to suspend the fetch (e.g. before the session is known). */
  enabled?: boolean;
}

export function usePortfolio(options: UsePortfolioOptions = {}) {
  const { enabled = true } = options;
  return useQuery<PortfolioSummary>(portfolioSummaryQueryKey, () => portfolioService.getSummary(), {
    enabled,
  });
}
