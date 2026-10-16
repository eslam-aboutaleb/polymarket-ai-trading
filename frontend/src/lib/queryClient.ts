/**
 * The single react-query client for the app.
 *
 * Defaults are tuned for this dashboard: 30s of freshness so re-mounted panels
 * do not re-hit the API, one retry (money-adjacent reads get a second chance
 * without stalling the UI), and no refetch on window focus because several
 * panels already poll on their own cadence.
 *
 * @module lib/queryClient
 */

import { QueryClient } from "react-query";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});
