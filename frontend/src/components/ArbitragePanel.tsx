/**
 * Arbitrage scanner cards and paper-only feasibility detail.
 *
 * Scanner rows are indicative price gaps: every card says so, a wide spread is shown as a
 * liquidity signal (never arbitrage), and the feasibility view shows executable size, the cost
 * waterfall, one-leg exposure and resolution warnings. Nothing here can place an order.
 *
 * @module components/ArbitragePanel
 */
import { useCallback, useEffect, useState } from "react";
import { ArbitrageOpportunity } from "../services/emergencyService";
import { ArbCandidate, arbitrageService } from "../services/arbitrageService";
import { getApiErrorMessage } from "../utils/apiError";

interface ArbitragePanelProps {
  signals: ArbitrageOpportunity[];
}

const COMPLEMENT_LABEL = "Indicative price gap (not guaranteed profit)";
const SPREAD_LABEL = "Wide spread (liquidity signal, not arbitrage)";

function Badges({ badges }: { badges: string[] }) {
  return (
    <span className="flex flex-wrap gap-1">
      {badges.map((badge) => (
        <span
          key={badge}
          className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400 font-mono"
        >
          {badge}
        </span>
      ))}
    </span>
  );
}

function FeasibilityDetail({ candidate }: { candidate: ArbCandidate }) {
  return (
    <div aria-label="Feasibility detail" role="group" className="mt-3 space-y-2 text-xs">
      <p>
        State: <span className="font-mono font-semibold">{candidate.state}</span>
        {candidate.paper_only ? " · paper only" : ""}
      </p>
      <p>
        Executable size: <span className="font-mono">{candidate.size}</span> of requested{" "}
        <span className="font-mono">{candidate.requested_size}</span> (max displayed{" "}
        <span className="font-mono">{candidate.max_executable_size}</span>, max profitable{" "}
        <span className="font-mono">{candidate.max_profitable_size ?? "none"}</span>)
      </p>
      {candidate.reasons.length > 0 && (
        <p className="text-amber-400">Reasons: {candidate.reasons.join(", ")}</p>
      )}
      {candidate.equivalence && candidate.equivalence.status !== "VERIFIED_EQUIVALENT" && (
        <p className="text-amber-400">
          Resolution mismatch or unverified: {candidate.equivalence.reasons.join(", ")}
        </p>
      )}
      {candidate.cost_waterfall.length > 0 && (
        <table className="table-theme w-full" aria-label="Cost waterfall">
          <thead>
            <tr className="text-left text-muted/60">
              <th>Item</th>
              <th className="text-right">USDC</th>
            </tr>
          </thead>
          <tbody>
            {candidate.cost_waterfall.map((row) => (
              <tr key={row.item}>
                <td>{row.item}</td>
                <td className="text-right font-mono">{row.amount}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {candidate.partial_fill_risk && (
        <p>
          One-leg failure exposure is bounded at{" "}
          <span className="font-mono">{candidate.partial_fill_risk.max_loss_bound}</span> USDC (legs
          are separate orders, not atomic).
        </p>
      )}
      {candidate.notices.map((notice) => (
        <p key={notice} className="text-soft">
          {notice}
        </p>
      ))}
    </div>
  );
}

export default function ArbitragePanel({ signals }: ArbitragePanelProps) {
  const [candidates, setCandidates] = useState<ArbCandidate[]>([]);
  const [size, setSize] = useState("10");
  const [feeBps, setFeeBps] = useState("");
  const [feeVersion, setFeeVersion] = useState("");
  const [gas, setGas] = useState("");
  const [settlement, setSettlement] = useState("");
  const [checked, setChecked] = useState<Record<string, ArbCandidate>>({});
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await arbitrageService.listCandidates();
      setCandidates(res.candidates || []);
    } catch {
      setCandidates([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, signals]);

  const check = async (candidate: ArbCandidate) => {
    setError(null);
    try {
      const res = await arbitrageService.feasibility(candidate.candidate_id, {
        size,
        ...(feeBps ? { fee_bps: feeBps } : {}),
        ...(feeVersion ? { fee_version: feeVersion } : {}),
        costs: {
          ...(gas ? { gas_usdc: gas } : {}),
          ...(settlement ? { settlement_usdc: settlement } : {}),
        },
      });
      setChecked((prev) => ({ ...prev, [candidate.candidate_id]: res.candidate }));
    } catch (err) {
      setError(getApiErrorMessage(err, "Feasibility check failed"));
    }
  };

  return (
    <section aria-label="Arbitrage scanner" className="space-y-3">
      <h3 className="text-sm font-semibold">Arbitrage scanner (research only)</h3>
      {signals.map((row, index) => {
        const spread = row.type === "spread";
        const label = row.label ?? (spread ? SPREAD_LABEL : COMPLEMENT_LABEL);
        const gap = row.gross_price_gap_pct_points ?? row.profit_pct;
        return (
          <article
            key={`${row.market_id}-${row.type}-${index}`}
            className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3 text-xs"
          >
            <p className="font-semibold">{row.market_title}</p>
            <p className="text-amber-400">{label}</p>
            {!spread && gap != null && (
              <p className="font-mono">Gross gap {gap} pts (indicative, before fees and depth)</p>
            )}
            {spread && row.spread_pct != null && (
              <p className="font-mono">Quoted spread {row.spread_pct}%</p>
            )}
            <Badges
              badges={
                row.badges && row.badges.length > 0
                  ? row.badges
                  : ["INDICATIVE_ONLY", "NOT_EXECUTABLE", "UNKNOWN_FEES"]
              }
            />
          </article>
        );
      })}
      {candidates.length > 0 && (
        <div className="rounded border border-[var(--line)] p-3 text-xs space-y-2">
          <p className="font-semibold">Feasibility check (frozen book receipts, paper only)</p>
          <div className="flex flex-wrap gap-2">
            {[
              ["Size (shares)", size, setSize],
              ["Fee (bps)", feeBps, setFeeBps],
              ["Fee version", feeVersion, setFeeVersion],
              ["Gas (USDC)", gas, setGas],
              ["Settlement cost (USDC)", settlement, setSettlement],
            ].map(([name, value, setter]) => (
              <label key={name as string} className="flex flex-col gap-1">
                {name as string}
                <input
                  className="bg-[var(--bg-soft)] border border-[var(--line)] rounded px-2 py-1 w-32"
                  value={value as string}
                  onChange={(e) => (setter as (v: string) => void)(e.target.value)}
                />
              </label>
            ))}
          </div>
          {candidates.map((candidate) => (
            <div key={candidate.candidate_id} className="border-t border-[var(--line)] pt-2">
              <p>
                {candidate.market_title ?? candidate.condition_ids[0]} ·{" "}
                <span className="font-mono">{candidate.state}</span>
              </p>
              <button
                type="button"
                className="btn-muted text-xs mt-1"
                onClick={() => void check(candidate)}
              >
                Check feasibility
              </button>
              {checked[candidate.candidate_id] && (
                <FeasibilityDetail candidate={checked[candidate.candidate_id]} />
              )}
            </div>
          ))}
          {error && <p className="text-red-400">{error}</p>}
        </div>
      )}
    </section>
  );
}
