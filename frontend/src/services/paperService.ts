import { apiClient } from "./apiClient";

export interface PaperAccountValue {
  initialized: boolean;
  account_id: number;
  nav?: string | null;
  cash?: string;
  available_cash?: string;
  reserved_cash?: string;
  fees?: string;
  net_pnl?: string | null;
  fee_bps?: string | null;
  quality?: string;
  legacy_data_excluded?: boolean;
}

export const paperService = {
  account: () => apiClient.get<PaperAccountValue>("/api/paper/account"),
  configure: (fee: string, acknowledgeLegacy: boolean) =>
    apiClient.post<PaperAccountValue>("/api/paper/account", {
      fee_bps: fee,
      acknowledge_legacy: acknowledgeLegacy,
    }),
  snapshot: () => apiClient.post<Record<string, unknown>>("/api/paper/snapshot", {}),
  refreshMarket: (conditionId: string) =>
    apiClient.post("/api/paper/refresh-market", { condition_id: conditionId }),
};
