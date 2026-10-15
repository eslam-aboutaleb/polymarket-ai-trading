/**
 * @module BillingSection
 * Subscription surface: current plan, usage against quota, and the
 * upgrade/downgrade actions (plan 09 Phase A).
 *
 * The tier matrix is read from `GET /api/billing/plans` rather than
 * duplicated here, so this component never decides what a plan includes —
 * it renders what the server says and names the consequences of a
 * downgrade. That is the same rule the backend follows: one matrix, one
 * place that writes the plan column.
 *
 * A downgrade is deliberately shown as a *loss list* before it happens.
 * Entitlements are additive-only server-side — a downgrade stops new
 * automated activity and revokes external trade scopes, and never deletes
 * position or order history — so the honest thing to promise a subscriber
 * is "these switches stop, nothing is destroyed", and the honest thing to
 * ask for is a confirmation.
 *
 * No react-query: the repo's settings sections use `useState` + a service
 * call + `useEffect`, and three small reads do not justify a cache layer.
 */

import { useCallback, useEffect, useState } from "react";
import { getApiErrorMessage } from "../utils/apiError";
import {
  CAPABILITY_LABELS,
  DOWNGRADE_WARNING_ORDER,
  billingService,
  Capability,
  PlanTier,
  PlansResponse,
  SubscriptionResponse,
} from "../services/billingService";

const PLAN_ORDER: PlanTier[] = ["free", "pro", "institutional"];

function formatDate(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleDateString();
}

function formatLimit(value: number | null, unlimited: boolean): string {
  if (unlimited || value === null) return "Unlimited";
  return String(value);
}

/** Fraction of the bar to fill, clamped so an over-budget row still renders. */
function fillFraction(used: number, limit: number | null, unlimited: boolean): number {
  if (unlimited || limit === null || limit <= 0) return 0;
  return Math.min(1, used / limit);
}

function UsageBar({
  label,
  used,
  limit,
  unlimited,
}: {
  label: string;
  used: number;
  limit: number | null;
  unlimited: boolean;
}) {
  const fraction = fillFraction(used, limit, unlimited);
  const exhausted = !unlimited && limit !== null && limit > 0 && used >= limit;
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between text-sm">
        <span className="text-muted">{label}</span>
        <span className={exhausted ? "text-[var(--danger,#f87171)]" : ""}>
          {used} / {formatLimit(limit, unlimited)}
        </span>
      </div>
      <div className="h-1.5 rounded-full bg-[var(--line)] overflow-hidden">
        <div
          className="h-full rounded-full bg-[var(--accent)]"
          style={{ width: `${Math.round(fraction * 100)}%` }}
          role="progressbar"
          aria-valuenow={used}
          aria-valuemin={0}
          aria-valuemax={limit ?? 0}
          aria-label={`${label} usage`}
        />
      </div>
    </div>
  );
}

export default function BillingSection() {
  const [subscription, setSubscription] = useState<SubscriptionResponse | null>(null);
  const [plans, setPlans] = useState<PlansResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<PlanTier | null>(null);
  const [email, setEmail] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  /** Plan the user has picked an upgrade for; its email prompt is inline. */
  const [pendingPlan, setPendingPlan] = useState<PlanTier | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [subscriptionRow, planRow] = await Promise.all([
        billingService.getSubscription(),
        billingService.getPlans(),
      ]);
      setSubscription(subscriptionRow);
      setPlans(planRow);
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // A completed Stripe Checkout returns to this tab with a marker; the
  // plan itself is written by the webhook, so a short re-read is all the
  // confirmation this needs.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("checkout") !== "success") return;
    setNotice("Checkout complete — refreshing your plan.");
    const timer = window.setTimeout(() => {
      load().catch(() => undefined);
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [load]);

  const startUpgrade = async (plan: PlanTier) => {
    setBusy(plan);
    setError(null);
    setNotice(null);
    try {
      const needsEmail = !(subscription?.email_on_file ?? false);
      if (needsEmail && !email.trim()) {
        setPendingPlan(plan);
        return;
      }
      const session = await billingService.checkoutSession(
        plan,
        needsEmail ? email.trim() : undefined,
      );
      window.location.assign(session.url);
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setBusy(null);
    }
  };

  const openPortal = async () => {
    setBusy(subscription?.subscription.plan ?? null);
    setError(null);
    setNotice(null);
    try {
      const session = await billingService.portalSession(
        `${window.location.origin}/settings?tab=billing`,
      );
      window.location.assign(session.url);
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setBusy(null);
    }
  };

  if (loading) {
    return (
      <div className="surface-panel p-6" role="status" aria-live="polite">
        <p className="text-muted">Loading your subscription…</p>
      </div>
    );
  }

  if (!subscription || !plans) {
    return (
      <div className="surface-panel p-6 space-y-4">
        <h2 className="text-xl font-semibold">Subscription</h2>
        <p className="text-sm text-muted">
          {error ?? "Your subscription details are unavailable right now."}
        </p>
        <button type="button" className="btn-primary" onClick={load}>
          Retry
        </button>
      </div>
    );
  }

  const currentPlan = subscription.usage.plan;
  const currentIndex = PLAN_ORDER.indexOf(currentPlan);
  const upgrades = plans.plans.filter(
    (row) => row.purchasable && PLAN_ORDER.indexOf(row.plan) > currentIndex,
  );
  const currentSummary = plans.plans.find((row) => row.plan === currentPlan);
  const lost: Capability[] = currentSummary
    ? DOWNGRADE_WARNING_ORDER.filter(
        (capability) =>
          !currentSummary.capabilities.includes(capability) &&
          capability !== "manual_trading" &&
          capability !== "market_browsing" &&
          capability !== "basic_analysis",
      )
    : [];
  const { usage } = subscription;

  return (
    <div className="space-y-6">
      {/* Current plan */}
      <div className="surface-panel p-6 space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-xl font-semibold">Subscription</h2>
            <p className="text-sm text-muted">
              {usage.plan_label} plan · status {usage.status}
            </p>
          </div>
          <span className="text-sm text-muted">
            Renews {formatDate(subscription.subscription.current_period_end)}
          </span>
        </div>

        {subscription.subscription.cancel_at_period_end && (
          <p className="text-sm text-[var(--accent)]">
            This subscription ends at the current period and will drop to Free. Nothing is deleted —
            the automated strategies simply stop starting new activity.
          </p>
        )}

        {!subscription.billing_configured && (
          <p className="text-sm text-muted">
            Billing is not configured on this deployment, so upgrades are unavailable. Every Free
            capability stays fully usable.
          </p>
        )}

        <div className="grid gap-4 sm:grid-cols-2">
          <UsageBar
            label="Accounts"
            used={usage.accounts.used}
            limit={usage.accounts.limit}
            unlimited={usage.accounts.unlimited}
          />
          <UsageBar
            label="Followed traders"
            used={usage.followed_traders.used}
            limit={usage.followed_traders.limit}
            unlimited={usage.followed_traders.unlimited}
          />
          {usage.quotas.map((quota) => (
            <UsageBar
              key={quota.capability}
              label={CAPABILITY_LABELS[quota.capability] ?? quota.capability}
              used={quota.used}
              limit={quota.limit}
              unlimited={quota.unlimited}
            />
          ))}
        </div>
      </div>

      {/* What the current plan includes */}
      <div className="surface-panel p-6 space-y-3">
        <h3 className="text-lg font-semibold">Included with {usage.plan_label}</h3>
        <ul className="flex flex-wrap gap-2">
          {usage.capabilities.map((capability) => (
            <li
              key={capability}
              className="rounded-full border border-[var(--line)] px-3 py-1 text-xs"
            >
              {CAPABILITY_LABELS[capability] ?? capability}
            </li>
          ))}
        </ul>
      </div>

      {/* Upgrade */}
      {upgrades.length > 0 && (
        <div className="surface-panel p-6 space-y-4">
          <h3 className="text-lg font-semibold">Upgrade</h3>
          {pendingPlan && (
            <div className="space-y-2">
              <label className="block text-sm text-muted" htmlFor="billing-email">
                Email for receipts
              </label>
              <input
                id="billing-email"
                className="input-field"
                type="email"
                placeholder="you@example.com"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
              <p className="text-xs text-muted">
                Stripe needs a valid address to issue receipts. The Free tier never asks for one.
              </p>
            </div>
          )}
          <table className="table-theme w-full text-sm">
            <thead>
              <tr>
                <th>Plan</th>
                <th>Adds</th>
                <th className="text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {upgrades.map((plan) => (
                <tr key={plan.plan}>
                  <td className="font-medium">{plan.label}</td>
                  <td>
                    {plan.capabilities
                      .filter((capability) => !usage.capabilities.includes(capability))
                      .map((capability) => CAPABILITY_LABELS[capability] ?? capability)
                      .join(", ") || "—"}
                  </td>
                  <td className="text-right">
                    <button
                      type="button"
                      className="btn-primary"
                      disabled={busy !== null || !subscription.billing_configured}
                      onClick={() => startUpgrade(plan.plan)}
                    >
                      {busy === plan.plan ? "Redirecting…" : `Upgrade to ${plan.label}`}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Downgrade */}
      {currentPlan !== "free" && (
        <div className="surface-panel p-6 space-y-4">
          <h3 className="text-lg font-semibold">Manage or downgrade</h3>
          {lost.length > 0 && (
            <div className="space-y-2">
              <p className="text-sm font-medium">What you lose on downgrade</p>
              <ul className="list-disc space-y-1 pl-5 text-sm text-muted">
                {lost.map((capability) => (
                  <li key={capability}>{CAPABILITY_LABELS[capability] ?? capability}</li>
                ))}
              </ul>
              <p className="text-xs text-muted">
                A downgrade stops these from starting anything new and revokes the external API
                trade scope. It never cancels orders, liquidates positions, or deletes your trade
                history, stop-losses or API keys — those stay exactly as they are, and you keep
                reaching the Free capabilities straight away.
              </p>
            </div>
          )}
          <p className="text-sm text-muted">
            Downgrades and cancellations are handled in the Stripe portal, which reports back here
            as a webhook so the plan you see is always the one being enforced.
          </p>
          <button
            type="button"
            className="btn-primary"
            disabled={busy !== null || !subscription.billing_configured}
            onClick={openPortal}
          >
            {busy === currentPlan ? "Redirecting…" : "Open billing portal"}
          </button>
        </div>
      )}

      {notice && <p className="text-sm text-[var(--accent)]">{notice}</p>}
      {error && (
        <div className="surface-panel p-4 text-sm">
          <p>{error}</p>
          <button type="button" className="btn-primary mt-3" onClick={load}>
            Retry
          </button>
        </div>
      )}
    </div>
  );
}
