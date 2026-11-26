import { MODE_INFO, type TradingMode } from "../../lib/evidence";

/** Research, Shadow, Paper or Live disabled, always as text plus color. */
export default function ModeBadge({ mode }: { mode: TradingMode }) {
  const info = MODE_INFO[mode];
  return (
    <span
      data-testid="mode-badge"
      data-mode={mode}
      title={info.description}
      className={`inline-flex items-center rounded border px-2 py-0.5 text-xs ${info.cls}`}
    >
      {info.label}
    </span>
  );
}
