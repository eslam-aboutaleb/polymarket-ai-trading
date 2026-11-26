import { useEffect, useRef, useState } from "react";
import {
  streamMarketDeepDive,
  type MarketDeepDiveResult,
} from "../../services/marketDeepDiveService";
import {
  CostPanel,
  EvidenceExplorer,
  ForecastPanel,
  MarketSummaryCard,
  ResearchConclusionCard,
  ResolutionRuleCard,
} from "./DeepDiveCards";

/** On-demand structured deep dive. The server resolves market data; nothing is sent from the URL. */
export default function MarketDeepDive({ conditionId }: { conditionId: string }) {
  const [running, setRunning] = useState(false);
  const [stage, setStage] = useState("");
  const [result, setResult] = useState<MarketDeepDiveResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);

  const cancel = () => {
    controller.current?.abort();
    controller.current = null;
    setRunning(false);
    setStage("");
  };

  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    cancel();
    setResult(null);
    setError(null);
  }, [conditionId]);

  const start = () => {
    controller.current?.abort();
    setRunning(true);
    setError(null);
    setResult(null);
    setStage("Starting…");
    const own = streamMarketDeepDive(
      { conditionId },
      {
        onStage: (text) => controller.current === own && setStage(text),
        onResult: (value) => controller.current === own && setResult(value),
        onDone: () => {
          if (controller.current !== own) return;
          setRunning(false);
          setStage("");
        },
        onError: (message) => {
          if (controller.current !== own) return;
          setRunning(false);
          setStage("");
          setError(message);
        },
      },
    );
    controller.current = own;
  };

  return (
    <section aria-label="Structured deep dive" className="space-y-6">
      <div className="surface-panel p-6">
        <h2 className="text-lg font-bold">Structured deep dive</h2>
        <p className="text-sm my-2">
          Cited evidence, validated forecast and depth-walked cost. Research only.
        </p>
        {running ? (
          <button className="btn-muted" onClick={cancel}>
            Cancel deep dive
          </button>
        ) : (
          <button className="btn-primary" onClick={start}>
            {result || error ? "Run deep dive again" : "Run structured deep dive"}
          </button>
        )}
        <p role="status" aria-live="polite" className="text-sm mt-2">
          {running ? stage : ""}
        </p>
        {error && (
          <p role="alert" className="mt-2">
            {error}
          </p>
        )}
      </div>
      {result && (
        <>
          <MarketSummaryCard result={result} />
          <ResolutionRuleCard result={result} />
          <EvidenceExplorer result={result} />
          <ForecastPanel forecast={result.forecast} />
          <CostPanel cost={result.price_and_cost} />
          <ResearchConclusionCard result={result} />
        </>
      )}
    </section>
  );
}
