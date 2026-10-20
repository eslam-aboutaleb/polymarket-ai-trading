import { useEffect, useState } from "react";
import { emergencyService, EmergencyStopResponse } from "../services/emergencyService";

/** Poll durable state so a reload or another worker's progress stays visible. */
export default function EmergencyHaltStatus() {
  const [report, setReport] = useState<EmergencyStopResponse | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const next = await emergencyService.latestHalt();
        if (!disposed) {
          setReport(next);
          setUnavailable(false);
        }
      } catch {
        if (!disposed) setUnavailable(true);
      } finally {
        if (!disposed) timer = setTimeout(refresh, 3000);
      }
    };
    void refresh();
    return () => {
      disposed = true;
      clearTimeout(timer);
    };
  }, []);

  const count = (value: number | null) => value ?? "Unverified";
  return (
    <div role="status" aria-live="polite" className="text-sm space-y-1">
      <p>
        Live orders and automated exits are disabled pending safety verification. Existing positions
        may remain exposed.
      </p>
      {unavailable && <p>Halt status unavailable. Previous results may be stale.</p>}
      {report && (
        <>
          <p>Trading halted · {report.operation_status.split("_").join(" ")}</p>
          <p>
            Cancellation requests: {report.cancel_requests_submitted}. Confirmed cancelled:{" "}
            {report.orders_confirmed_cancelled}. Still open: {count(report.orders_still_open)}.
          </p>
          <p>
            Exit requests: {report.close_requests_submitted}. Confirmed closed:{" "}
            {report.positions_confirmed_closed}. Unresolved positions:{" "}
            {count(report.unresolved_positions)}.
          </p>
          <p>
            Position exits require verification. Positions remain visible and may still be exposed.
          </p>
          <p>Operation: {report.operation_id}</p>
        </>
      )}
    </div>
  );
}
