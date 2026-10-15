/**
 * Step-by-step timeline for a running orchestration plan.
 *
 * Each step shows its status, the backend that executed it, its duration and
 * (when it failed) the typed error the service returned. A `partial` badge
 * appears as soon as any step has failed, because the synthesis that follows
 * is a degraded one — the step outputs are still factual, the merged prose
 * is not.
 *
 * @module components/OrchestratorTimeline
 */

import type { OrchestrationStep } from "../services/orchestrationService";

interface OrchestratorTimelineProps {
  steps: OrchestrationStep[];
  /** True while the plan is still running (the last step may be pending). */
  running?: boolean;
  /** True when the plan finished with at least one failed step. */
  partial?: boolean;
  /** Step names the planner will run, in order (shown as pending while idle). */
  plannedSteps?: string[];
}

const BACKEND_LABEL: Record<string, string> = {
  "llm-chain": "llm-chain",
  "cli-agent": "cli-agent",
};

function statusChip(step: OrchestrationStep): { label: string; className: string } {
  if (step.ok) return { label: "done", className: "chip chip-success" };
  if (step.error?.startsWith("skipped"))
    return { label: "skipped", className: "chip chip-warning" };
  return { label: "failed", className: "chip chip-danger" };
}

function excerpt(step: OrchestrationStep): string {
  if (!step.ok) return step.error || "Step failed";
  const analysis = step.output?.analysis;
  const text = typeof analysis === "string" ? analysis : JSON.stringify(step.output ?? {});
  const flattened = text.replace(/\s+/g, " ").trim();
  return flattened.length > 220 ? `${flattened.slice(0, 220)}…` : flattened;
}

export default function OrchestratorTimeline({
  steps,
  running = false,
  partial = false,
  plannedSteps = [],
}: OrchestratorTimelineProps) {
  const pending = running
    ? plannedSteps.filter((name) => !steps.some((step) => step.step === name))
    : [];

  if (steps.length === 0 && pending.length === 0) {
    return (
      <p className="text-sm text-soft">
        No steps yet — pick an intent and run the plan to see each subtask.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {partial && (
        <div className="flex items-center gap-2">
          <span className="chip chip-warning">partial</span>
          <span className="text-xs text-soft">
            One or more steps failed — the synthesis below is degraded, not complete.
          </span>
        </div>
      )}

      <ol className="space-y-2">
        {steps.map((step, index) => {
          const status = statusChip(step);
          return (
            <li
              key={`${step.step}-${index}`}
              className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3"
            >
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="text-xs font-mono text-soft">{index + 1}.</span>
                  <span className="font-semibold text-sm truncate">{step.step}</span>
                  <span className={status.className}>{status.label}</span>
                  {step.backend && (
                    <span className="chip chip-accent">
                      {BACKEND_LABEL[step.backend] || step.backend}
                    </span>
                  )}
                </div>
                <span className="text-xs text-soft mono">{step.duration_ms}ms</span>
              </div>
              <p className="text-xs text-soft mt-2 whitespace-pre-wrap break-words">
                {excerpt(step)}
              </p>
            </li>
          );
        })}

        {pending.map((name, index) => (
          <li
            key={`pending-${name}-${index}`}
            className="rounded border border-dashed border-[var(--line)] p-3"
          >
            <div className="flex items-center gap-2">
              <span className="text-xs font-mono text-soft">{steps.length + index + 1}.</span>
              <span className="font-semibold text-sm truncate">{name}</span>
              <span className="chip">{running ? "running" : "pending"}</span>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
