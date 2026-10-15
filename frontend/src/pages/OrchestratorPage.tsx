/**
 * Orchestrator page: run a multi-step analysis plan and watch it execute.
 *
 * The intent picker selects a planner (market deep dive, opportunity sweep,
 * trader diligence) and the payload form supplies the fields that planner
 * reads. Running the plan streams each step over SSE into the timeline; the
 * merged synthesis renders below it, with a `partial` badge whenever any step
 * failed and the answer is degraded.
 *
 * @module pages/OrchestratorPage
 */

import { useEffect, useMemo, useRef, useState } from "react";
import OrchestratorTimeline from "../components/OrchestratorTimeline";
import { sanitizeAIError } from "../utils/apiError";
import {
  orchestrationService,
  type OrchestrationIntent,
  type OrchestrationStep,
} from "../services/orchestrationService";

interface IntentSpec {
  id: OrchestrationIntent;
  label: string;
  hint: string;
  fields: { name: string; label: string; type: "text" | "number"; default: string }[];
  /** Step names the service planner will run, in order (shown as pending). */
  plannedSteps: string[];
  /** Compose the payload dict from the form values. */
  build: (values: Record<string, string>) => Record<string, unknown>;
}

const INTENTS: IntentSpec[] = [
  {
    id: "market_deep_dive",
    label: "Market Deep Dive",
    hint: "Analyze the market with web research, assess the risk, then build a trade plan.",
    plannedSteps: ["analyze_market", "assess_risk", "trade_plan"],
    fields: [
      { name: "market_title", label: "Market title", type: "text", default: "" },
      { name: "yes_price", label: "YES price", type: "number", default: "0.5" },
      { name: "no_price", label: "NO price", type: "number", default: "0.5" },
      { name: "volume_24h", label: "24h volume (USD)", type: "number", default: "0" },
      { name: "position_size", label: "Position size (USD)", type: "number", default: "100" },
      { name: "days_to_expiry", label: "Days to expiry", type: "number", default: "30" },
      { name: "action", label: "Action (buy_yes / buy_no)", type: "text", default: "buy_yes" },
    ],
    build: (values) => ({
      market_title: values.market_title || "Untitled market",
      yes_price: Number(values.yes_price) || 0.5,
      no_price: Number(values.no_price) || 0.5,
      volume_24h: Number(values.volume_24h) || 0,
      position_size: Number(values.position_size) || 100,
      days_to_expiry: Number(values.days_to_expiry) || 30,
      action: values.action || "buy_yes",
    }),
  },
  {
    id: "opportunity_sweep",
    label: "Opportunity Sweep",
    hint: "Scan the markets you paste below, deep-dive the three busiest, then assess the top one.",
    plannedSteps: ["scan_markets", "deep_dive_1", "deep_dive_2", "deep_dive_3", "assess_risk"],
    fields: [
      {
        name: "markets",
        label: "Markets (JSON array: title, yes_price, no_price, volume_24h)",
        type: "text",
        default:
          '[{"title":"Will BTC hit $150k?","yes_price":0.62,"no_price":0.38,"volume_24h":250000}]',
      },
    ],
    build: (values) => {
      let markets: unknown = [];
      try {
        markets = JSON.parse(values.markets || "[]");
      } catch {
        markets = [];
      }
      return { markets: Array.isArray(markets) ? markets : [] };
    },
  },
  {
    id: "trader_diligence",
    label: "Trader Diligence",
    hint: "Profile the trader, evaluate copying the trade, then assess the risk of doing it.",
    plannedSteps: ["analyze_trader", "evaluate_copy_trade", "assess_risk"],
    fields: [
      { name: "wallet_address", label: "Trader wallet", type: "text", default: "" },
      { name: "market_title", label: "Market title", type: "text", default: "" },
      { name: "win_rate", label: "Win rate (0-1)", type: "number", default: "0.6" },
      { name: "trade_size", label: "Trade size (USD)", type: "number", default: "25" },
      { name: "current_price", label: "Entry price", type: "number", default: "0.5" },
    ],
    build: (values) => ({
      wallet_address: values.wallet_address || "0x0000000000000000000000000000000000000000",
      market_title: values.market_title || "Untitled market",
      win_rate: Number(values.win_rate) || 0,
      trade_size: Number(values.trade_size) || 0,
      current_price: Number(values.current_price) || 0.5,
    }),
  },
];

export default function OrchestratorPage() {
  const [intentId, setIntentId] = useState<OrchestrationIntent>("market_deep_dive");
  const [values, setValues] = useState<Record<string, string>>({});
  const [steps, setSteps] = useState<OrchestrationStep[]>([]);
  const [plannedSteps, setPlannedSteps] = useState<string[]>([]);
  const [synthesis, setSynthesis] = useState("");
  const [partial, setPartial] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const intent = useMemo(
    () => INTENTS.find((item) => item.id === intentId) || INTENTS[0],
    [intentId],
  );

  // The planner's step names, so the timeline can show what is still queued.
  useEffect(() => setPlannedSteps(intent.plannedSteps), [intent]);

  const formValues = useMemo(() => {
    const seeded: Record<string, string> = {};
    for (const field of intent.fields) seeded[field.name] = field.default;
    return { ...seeded, ...values };
  }, [intent, values]);

  const run = () => {
    abortRef.current?.abort();
    setSteps([]);
    setSynthesis("");
    setPartial(false);
    setError(null);
    setRunning(true);

    const controller = orchestrationService.streamOrchestrate(
      { intent: intent.id, payload: intent.build(formValues) },
      {
        onStep: (step) => setSteps((prev) => [...prev, step]),
        onRecommendation: (text, isPartial) => {
          setSynthesis(text);
          setPartial(isPartial);
        },
        onDone: () => setRunning(false),
        onError: (message) => {
          setError(sanitizeAIError(message));
          setRunning(false);
        },
      },
    );
    abortRef.current = controller;
  };

  const abort = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setRunning(false);
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">AI Orchestrator</h1>
        <p className="text-soft text-sm mt-1">
          One request, several analysis steps, one merged verdict. Each step runs on the backend
          that owns it — GitHub Models providers are served by the CLI agent, everything else by the
          LLM chain.
        </p>
      </div>

      <section className="surface-panel p-5 space-y-4">
        <h2 className="text-lg font-semibold">1. Pick an intent</h2>
        <div className="flex flex-wrap gap-2">
          {INTENTS.map((item) => (
            <button
              key={item.id}
              type="button"
              className={item.id === intentId ? "btn-primary text-xs" : "btn-muted text-xs"}
              onClick={() => setIntentId(item.id)}
              disabled={running}
            >
              {item.label}
            </button>
          ))}
        </div>
        <p className="text-xs text-soft">{intent.hint}</p>

        <h2 className="text-lg font-semibold">2. Fill the payload</h2>
        <div className="grid gap-3 md:grid-cols-2">
          {intent.fields.map((field) => (
            <label key={field.name} className="text-xs text-soft space-y-1">
              <span className="block">{field.label}</span>
              <input
                className="w-full rounded border border-[var(--line)] bg-[var(--bg-soft)] p-2 text-sm"
                type={field.type}
                value={formValues[field.name] ?? ""}
                onChange={(event) =>
                  setValues((prev) => ({ ...prev, [field.name]: event.target.value }))
                }
                disabled={running}
              />
            </label>
          ))}
        </div>

        <div className="flex flex-wrap gap-2">
          <button type="button" className="btn-primary text-xs" onClick={run} disabled={running}>
            {running ? "Running…" : "Run plan"}
          </button>
          {running && (
            <button type="button" className="btn-muted text-xs" onClick={abort}>
              Cancel
            </button>
          )}
        </div>
        {error && <div className="alert-error p-3 rounded text-sm">{error}</div>}
      </section>

      <section className="surface-panel p-5 space-y-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h2 className="text-lg font-semibold">3. Steps</h2>
          {partial && <span className="chip chip-warning">partial result</span>}
        </div>
        <OrchestratorTimeline
          steps={steps}
          running={running}
          partial={partial}
          plannedSteps={plannedSteps}
        />
      </section>

      {synthesis && (
        <section className="surface-panel p-5 space-y-2">
          <h2 className="text-lg font-semibold">Synthesis</h2>
          <pre className="text-xs whitespace-pre-wrap break-words mono">{synthesis}</pre>
        </section>
      )}
    </div>
  );
}
