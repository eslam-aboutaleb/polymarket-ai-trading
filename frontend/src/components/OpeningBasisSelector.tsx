import { useEffect, useState } from "react";
import { executionAnalyticsService } from "../services/executionAnalyticsService";
import { getApiErrorMessage } from "../utils/apiError";

export default function OpeningBasisSelector({
  openingId,
  onSelect,
}: {
  openingId: string;
  onSelect: (id: string) => void;
}) {
  const [rows, setRows] = useState<{ report_id: string; created_at: string }[]>([]);
  const [selected, setSelected] = useState("");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    setSelected("");
    onSelect("");
    setRows([]);
    setError(null);
    if (openingId)
      void executionAnalyticsService
        .getOpeningBasisReports(openingId)
        .then((reports) => {
          if (active) setRows(reports);
        })
        .catch((cause) => {
          if (active) setError(getApiErrorMessage(cause));
        });
    return () => {
      active = false;
    };
  }, [openingId, onSelect]);
  return (
    <div>
      <label>
        Opening basis from saved evidence
        <select
          aria-label="Opening basis from saved evidence"
          value={selected}
          className="block bg-surface border border-border"
          onChange={(event) => {
            setSelected(event.currentTarget.value);
            onSelect(event.currentTarget.value);
          }}
        >
          <option value="">Reconstruct from recorded fills</option>
          {rows.map((row) => (
            <option key={row.report_id} value={row.report_id}>
              {new Date(row.created_at).toLocaleString()} — {row.report_id.slice(0, 12)}
            </option>
          ))}
        </select>
      </label>
      {error && (
        <p role="alert" className="text-red-400">
          {error}
        </p>
      )}
    </div>
  );
}
