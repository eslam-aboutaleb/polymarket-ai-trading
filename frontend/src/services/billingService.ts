/**
 * @module billingService
 * Client for the subscription / entitlement surface (plan 09 Phase A).
 *
 * The server owns the tier matrix: `GET /api/billing/plans` returns which
 * capabilities each plan grants, so this client never hard-codes "pro
 * includes copy trading" — a plan change is a backend edit, not a UI
 * rebuild. What this module does own is the shape of the data and the
 * three actions a subscriber can take (upgrade, manage, read current
 * state).
 *
 * The email rule lives here too: `User.email` is optional in the product,
 * so a free account has none, and the checkout call is the only place one
 * is required. That is why `checkoutSession` takes an optional email and
 * the server answers 400 `email_required` when it has nothing valid to
 * give Stripe.
 */

import { apiClient } from "./apiClient";

export type PlanTier = "free" | "pro" | "institutional";
export type SubscriptionStatus =
  "none" | "active" | "trialing" | "past_due" | "canceled" | "incomplete";

export type Capability =
  | "manual_trading"
  | "market_browsing"
  | "basic_analysis"
  | "backtesting"
  | "orchestration"
  | "opportunity_scan"
  | "news_generation"
  | "ai_analysis"
  | "alerts"
  | "copy_trading"
  | "market_maker"
  | "inverse_bot"
  | "latency_arb"
  | "sub_accounts"
  | "external_api_read"
  | "external_api_trade"
  | "compliance_export"
  | "priority_support";

export interface QuotaUsage {
  capability: Capability;
  used: number;
  /** `null` when the plan has no ceiling for this surface. */
  limit: number | null;
  unlimited: boolean;
}

export interface AccountUsage {
  used: number;
  limit: number | null;
  unlimited: boolean;
}

export interface UsageReport {
  plan: PlanTier;
  plan_label: string;
  status: SubscriptionStatus;
  accounts: AccountUsage;
  followed_traders: AccountUsage;
  quotas: QuotaUsage[];
  /** Every capability the current plan grants. */
  capabilities: Capability[];
}

export interface SubscriptionRecord {
  plan: PlanTier;
  status: SubscriptionStatus;
  provider: string;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  created_at: string | null;
}

export interface PlanSummary {
  plan: PlanTier;
  label: string;
  capabilities: Capability[];
  limits: {
    max_accounts: number | null;
    max_followed_traders: number | null;
    quotas: Record<string, number | null>;
  };
  purchasable: boolean;
}

export interface PlansResponse {
  plans: PlanSummary[];
  current_plan: PlanTier;
  email_on_file: boolean;
  billing_configured: boolean;
}

export interface SubscriptionResponse {
  subscription: SubscriptionRecord;
  usage: UsageReport;
  email_on_file: boolean;
  billing_configured: boolean;
}

export interface CheckoutSessionResponse {
  url: string;
  session_id: string;
  plan: PlanTier;
}

const BASE = "/api/billing";

export const billingService = {
  /** Current plan, status and usage against every quota. */
  async getSubscription(): Promise<SubscriptionResponse> {
    return apiClient.get<SubscriptionResponse>(`${BASE}/subscription`);
  },

  /** The tier matrix plus the caller's position in it. */
  async getPlans(): Promise<PlansResponse> {
    return apiClient.get<PlansResponse>(`${BASE}/plans`);
  },

  /**
   * Start a Stripe Checkout for an upgrade. The plan is written by the
   * completion *webhook*, never by this call, so abandoning the checkout
   * changes nothing.
   *
   * @param plan the tier to buy; `free` is not purchasable
   * @param email required when the account has no valid one on file
   */
  async checkoutSession(plan: PlanTier, email?: string): Promise<CheckoutSessionResponse> {
    return apiClient.post<CheckoutSessionResponse>(`${BASE}/checkout-session`, {
      plan,
      ...(email ? { email } : {}),
    });
  },

  /**
   * Stripe's hosted portal: downgrade, cancel, or update the payment
   * method. The resulting plan change arrives as a webhook, so what the
   * portal shows and what the server enforces cannot diverge.
   */
  async portalSession(returnUrl?: string): Promise<{ url: string }> {
    return apiClient.post<{ url: string }>(
      `${BASE}/portal-session`,
      returnUrl ? { return_url: returnUrl } : {},
    );
  },
};

/**
 * Capabilities the downgrade warning has to name, in the order they
 * matter to somebody deciding. Sourced from the matrix at render time,
 * so this is only the copy, never the rule.
 */
export const DOWNGRADE_WARNING_ORDER: Capability[] = [
  "external_api_trade",
  "compliance_export",
  "copy_trading",
  "market_maker",
  "inverse_bot",
  "latency_arb",
  "sub_accounts",
  "orchestration",
  "opportunity_scan",
  "news_generation",
];

export const CAPABILITY_LABELS: Record<Capability, string> = {
  manual_trading: "Manual trading",
  market_browsing: "Market browsing",
  basic_analysis: "Basic analysis",
  backtesting: "Backtesting",
  orchestration: "AI orchestration",
  opportunity_scan: "Opportunity scans",
  news_generation: "News generation",
  ai_analysis: "AI analysis",
  alerts: "Alerts",
  copy_trading: "Copy trading",
  market_maker: "Market maker",
  inverse_bot: "Inverse bot",
  latency_arb: "Latency arbitrage",
  sub_accounts: "Sub-accounts",
  external_api_read: "External API reads",
  external_api_trade: "External API trading",
  compliance_export: "Compliance export",
  priority_support: "Priority support",
};
