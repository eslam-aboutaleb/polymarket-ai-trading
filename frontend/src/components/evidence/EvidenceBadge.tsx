import {
  assessEvidence,
  BLOCKER_TEXT,
  MISSING_TEXT,
  type OpportunityEvidence,
} from "../../lib/evidence";

export interface EvidenceBadgeProps {
  evidence?: OpportunityEvidence | null;
  /** Injectable clock so renders are deterministic. */
  now?: number;
}

const STATE_CLASS = {
  blocked: "border-red-500/40 bg-red-500/10 text-red-300",
  indicative: "border-yellow-500/40 bg-yellow-500/10 text-yellow-300",
  verified: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
} as const;

const STATE_LABEL = {
  blocked: "Not actionable",
  indicative: "Indicative, evidence incomplete",
  verified: "Positive net edge (estimate, not earnings)",
} as const;

/**
 * Shows whether the evidence behind an opportunity is complete. The label is
 * text, not only color, and the reasons are readable by screen readers.
 */
export default function EvidenceBadge({ evidence, now }: EvidenceBadgeProps) {
  const a = assessEvidence(evidence, now);
  const reasons = [
    ...a.blockers.map((b) => BLOCKER_TEXT[b]),
    ...a.missing.map((m) => `Missing: ${MISSING_TEXT[m]}`),
  ];
  return (
    <div className="text-xs" data-testid="evidence-badge" data-evidence-state={a.state}>
      <span
        className={`inline-flex items-center rounded border px-2 py-0.5 ${STATE_CLASS[a.state]}`}
      >
        {STATE_LABEL[a.state]}
      </span>
      {reasons.length > 0 && (
        <ul className="mt-1 list-disc pl-4 text-[var(--pm-text-soft)]" aria-label="Evidence gaps">
          {reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
