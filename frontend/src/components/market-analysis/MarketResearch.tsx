import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { marketContextService, type MarketContext } from "../../services/marketContextService";
import { getApiErrorMessage } from "../../utils/apiError";
import ResearchBrief from "./ResearchBrief";
import ObservationHistory from "./ObservationHistory";

export default function MarketResearch({ conditionId }: { conditionId: string }) {
  const [context, setContext] = useState<MarketContext | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setContext(null);
    setError(null);
    marketContextService.get(conditionId).then(
      (result) => {
        if (!cancelled) setContext(result);
      },
      (err: unknown) => {
        if (!cancelled) setError(getApiErrorMessage(err, "Market source unavailable"));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [conditionId, attempt]);

  if (error)
    return (
      <section className="surface-panel p-6" aria-label="Market research">
        <h1 className="text-xl font-bold">Market research unavailable</h1>
        <p role="alert" className="my-4">
          {error}
        </p>
        <p>
          Prices and rules cannot be verified from this link. Open a market from Opportunities or
          retry.
        </p>
        <button className="btn-muted mt-4" onClick={() => setAttempt((value) => value + 1)}>
          Retry market lookup
        </button>
      </section>
    );
  if (!context) return <p role="status">Loading market source…</p>;

  const yes = context.outcomes.find((outcome) => outcome.outcome === "YES")?.indicative_price;
  const no = context.outcomes.find((outcome) => outcome.outcome === "NO")?.indicative_price;
  const legacyParams = new URLSearchParams({
    market_title: context.question,
    market_description: context.rules.text,
    yes_price: String(yes),
    no_price: String(no),
    end_date: context.rules.market_end_date || "",
  });

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <section className="surface-panel p-6">
        <h1 className="text-xl font-bold mb-3">{context.question}</h1>
        <p className="text-sm mb-3">Research only · Indicative prices, not executable quotes</p>
        {yes != null && no != null && context.rules.market_end_date && (
          <Link className="underline inline-block mb-3" to={`/analysis?${legacyParams.toString()}`}>
            Continue to legacy AI analysis
          </Link>
        )}
        <div className="flex flex-wrap gap-3">
          {context.outcomes.map((outcome) => (
            <span className="chip" key={outcome.token_id}>
              {outcome.outcome}{" "}
              {outcome.indicative_price === null
                ? "unavailable"
                : `${Math.round(outcome.indicative_price * 100)}¢`}
            </span>
          ))}
        </div>
        <p className="text-sm mt-3">
          Source retrieved: {new Date(context.observed_at).toLocaleString()}. Quote time unknown.
        </p>
        <p className="text-sm">
          Market state:{" "}
          {context.closed === true
            ? "Closed"
            : context.active === true && context.closed === false
              ? "Active"
              : "Unknown or inactive"}
        </p>
      </section>
      <section className="surface-panel p-6" aria-labelledby="resolution-heading">
        <h2 id="resolution-heading" className="text-lg font-bold">
          What determines the outcome?
        </h2>
        <p className="my-3">Original venue rules · Interpretation unverified</p>
        <blockquote className="whitespace-pre-wrap border-l-2 pl-4">
          {context.rules.text || "Resolution rules unavailable. Do not infer them from the title."}
        </blockquote>
        <p className="mt-4">Market end date: {context.rules.market_end_date || "Unavailable"}</p>
        <p>Settlement cutoff and oracle: not independently verified.</p>
        <a
          className="underline inline-block mt-3"
          href={context.source_url}
          target="_blank"
          rel="noopener noreferrer"
        >
          Review original market source
        </a>
      </section>
      <section className="surface-panel p-6" aria-labelledby="missing-heading">
        <h2 id="missing-heading" className="text-lg font-bold">
          What still needs checking?
        </h2>
        <ul className="list-disc pl-5 mt-3 space-y-2">
          {context.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
        <details className="mt-4">
          <summary className="cursor-pointer">Source identity</summary>
          <dl className="text-xs break-all space-y-2 mt-2">
            <dt>Condition</dt>
            <dd>{context.condition_id}</dd>
            <dt>Rule and token mapping hash</dt>
            <dd>{context.rules.content_hash}</dd>
            <dt>Observation snapshot</dt>
            <dd>{context.snapshot_id}</dd>
          </dl>
        </details>
      </section>
      <ResearchBrief key={conditionId} conditionId={conditionId} onContext={setContext} />
      <ObservationHistory key={`history-${conditionId}`} conditionId={conditionId} />
    </div>
  );
}
