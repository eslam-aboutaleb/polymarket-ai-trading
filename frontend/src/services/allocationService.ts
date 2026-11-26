/**
 * Advisory scenario allocation under `/api/v1/portfolio`. Proposals never authorize execution.
 *
 * @module services/allocationService
 */

import { apiClient } from "./apiClient";

export interface AllocationPolicyResult {
  name: string;
  capital_required: string;
  worst_case_loss: string;
  expected_return_lower_bound: string | null;
  oos_realized_pnl: string | null;
  oos_note: string;
}

export interface AllocationSizing {
  candidate_id: string;
  shares: string;
  cost: string;
  kelly_fraction_used: string;
  marginal_worst_loss: string;
}

export interface AllocationProposalView {
  sizes: AllocationSizing[];
  rejected: { candidate_id: string; reasons: string[] }[];
  capital_required: string;
  cash_available_before: string;
  cash_remaining: string;
  worst_case_loss: string;
  max_worst_case_loss: string;
  cluster_warnings: string[];
  comparison: AllocationPolicyResult[];
  decision: "advisory_only";
  execution_eligible: false;
  as_of: string;
}

export interface StoredScenario {
  scenario_id: string;
  created_at: string;
  input_hash: string;
  allocator_version: string;
  proposal: AllocationProposalView;
}

export const allocationService = {
  scenarios(limit = 5): Promise<StoredScenario[]> {
    return apiClient.get<StoredScenario[]>(`/api/v1/portfolio/scenarios?limit=${limit}`);
  },
};
