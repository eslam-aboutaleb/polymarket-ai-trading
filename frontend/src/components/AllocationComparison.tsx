/**
 * Latest saved allocation proposal vs equal-risk and no-trade baselines.
 * Advisory only: nothing here places an order or changes a risk limit. Out-of-sample P&L is
 * shown only when frozen outcomes were supplied; otherwise the cell says so.
 *
 * @module components/AllocationComparison
 */

import { useEffect, useState } from "react";
import { allocationService, StoredScenario } from "../services/allocationService";

const LABELS: Record<string, string> = {
  scenario_allocator: "Scenario allocator",
  equal_risk_equal_cash: "Equal-risk (equal cash)",
  no_trade: "No trade",
};

const money = (value: string | null) =>
  value === null ? "not available" : `$${Number(value).toFixed(2)}`;

export default function AllocationComparison() {
  const [scenario, setScenario] = useState<StoredScenario | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "disabled" | "error">("loading");

  useEffect(() => {
    let live = true;
    allocationService
      .scenarios(1)
      .then((rows) => {
        if (!live) return;
        setScenario(rows[0] ?? null);
        setState("ready");
      })
      .catch((err: { status?: number; response?: { status?: number } }) => {
        if (!live) return;
        const status = err?.status ?? err?.response?.status;
        setState(status === 503 ? "disabled" : "error");
      });
    return () => {
      live = false;
    };
  }, []);

  if (state === "disabled") return null;
  return (
    <section
      className="card"
      aria-label="Allocation comparison"
      data-testid="allocation-comparison"
    >
      <h3 className="text-lg font-bold text-white">Allocation vs equal-risk</h3>
      <p className="text-soft text-sm">
        Advisory simulation. It cannot place orders or raise risk limits.
      </p>
      {state === "loading" && <p className="text-soft text-sm">Loading…</p>}
      {state === "error" && <p className="status-bad text-sm">Could not load saved scenarios.</p>}
      {state === "ready" && !scenario && (
        <p className="text-soft text-sm">No saved allocation simulation yet.</p>
      )}
      {scenario && (
        <>
          <table className="w-full text-sm mt-3">
            <caption className="sr-only">Policy comparison</caption>
            <thead>
              <tr className="text-left text-soft">
                <th scope="col">Policy</th>
                <th scope="col">Capital</th>
                <th scope="col">Worst-case loss</th>
                <th scope="col">Out-of-sample P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {scenario.proposal.comparison.map((row) => (
                <tr key={row.name}>
                  <td>{LABELS[row.name] ?? row.name}</td>
                  <td>{money(row.capital_required)}</td>
                  <td>{money(row.worst_case_loss)}</td>
                  <td title={row.oos_note}>{money(row.oos_realized_pnl)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {scenario.proposal.rejected.length > 0 && (
            <ul className="mt-3 text-sm text-soft">
              {scenario.proposal.rejected.map((r) => (
                <li key={r.candidate_id}>
                  {r.candidate_id}: rejected ({r.reasons.join(", ")})
                </li>
              ))}
            </ul>
          )}
          <p className="text-soft text-xs mt-2">
            Worst-case loss is a bound over feasible event states, not a forecast. As of{" "}
            {scenario.proposal.as_of}.
          </p>
        </>
      )}
    </section>
  );
}
