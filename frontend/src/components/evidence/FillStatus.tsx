import { fillInfo } from "../../lib/evidence";

export interface FillStatusProps {
  /** Raw exchange or API status string. Unknown values never read as filled. */
  status: string | null | undefined;
  filledSize?: number | null;
  orderedSize?: number | null;
}

/** Order lifecycle label. Accepted, acknowledged and partial are never "done". */
export default function FillStatus({ status, filledSize, orderedSize }: FillStatusProps) {
  const info = fillInfo(status);
  const showSize = filledSize != null && orderedSize != null;
  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="fill-status"
      data-fill-state={info.state}
      data-completed={info.completed ? "true" : "false"}
      className="text-xs"
    >
      <span className="font-medium">{info.label}</span>
      {showSize && (
        <span className="ml-2 text-[var(--pm-text-soft)]">
          {filledSize} of {orderedSize} shares confirmed
        </span>
      )}
      <p className="text-[var(--pm-text-soft)]">{info.description}</p>
    </div>
  );
}
