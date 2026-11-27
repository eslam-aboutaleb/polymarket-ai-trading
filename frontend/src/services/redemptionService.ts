import { apiClient } from "./apiClient";

export interface RedemptionIntent {
  id: number;
  intent_id: string;
  account_id: number;
  market_id: string;
  status: string;
  execution_mode: "preview" | "submit";
  preview_only: boolean;
  on_chain_submitted: boolean;
  broadcast_outcome_unknown: boolean;
  tx_hash: string | null;
  expected_payout: string;
  finalized_payout: string | null;
  snapshot_hash: string;
  receipt_hash: string | null;
  error: string | null;
  created_at: string;
}

const base = "/api/portfolio/redemption-intents";
export const redemptionService = {
  list: (): Promise<RedemptionIntent[]> => apiClient.get<RedemptionIntent[]>(base),
  preview: (condition_id: string, idempotency_key: string): Promise<RedemptionIntent> =>
    apiClient.post<RedemptionIntent>(base, {
      condition_id,
      idempotency_key,
      execution_mode: "preview",
    }),
  reconcile: (intentId: string): Promise<RedemptionIntent> =>
    apiClient.post<RedemptionIntent>(`${base}/${intentId}/reconcile`),
  evidence: (intentId: string): Promise<unknown> =>
    apiClient.get<unknown>(`${base}/${intentId}/evidence`),
};
