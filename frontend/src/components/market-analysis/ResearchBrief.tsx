import { useEffect, useRef, useState } from "react";
import type { MarketContext } from "../../services/marketContextService";
import {
  streamMarketResearch,
  type MarketResearchResult,
} from "../../services/marketResearchService";

export default function ResearchBrief({
  conditionId,
  onContext,
}: {
  conditionId: string;
  onContext: (context: MarketContext) => void;
}) {
  const controller = useRef<AbortController | null>(null);
  const [status, setStatus] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<MarketResearchResult | null>(null);
  const generation = useRef(0);
  useEffect(
    () => () => {
      generation.current++;
      controller.current?.abort();
    },
    [],
  );

  const start = () => {
    controller.current?.abort();
    const run = ++generation.current;
    setResult(null);
    setError(null);
    setRunning(true);
    setStatus("Loading market source…");
    controller.current = streamMarketResearch(conditionId, {
      onStatus: (text) => {
        if (run === generation.current) setStatus(text);
      },
      onResult: (value) => {
        if (run === generation.current) {
          setResult(value);
          onContext(value.context);
        }
      },
      onDone: () => {
        if (run === generation.current) {
          setRunning(false);
          setStatus("Research complete");
        }
      },
      onError: (message) => {
        if (run === generation.current) {
          setError(message);
          setResult(null);
          setRunning(false);
        }
      },
    });
  };
  const cancel = () => {
    generation.current++;
    controller.current?.abort();
    setRunning(false);
    setResult(null);
    setStatus("Research cancelled");
  };

  return (
    <section className="surface-panel p-6" aria-labelledby="brief-heading">
      <h2 id="brief-heading" className="text-lg font-bold">
        Evidence-grounded Deep Dive
      </h2>
      <p className="my-3 text-sm">
        Compare supporting and contradicting source excerpts. This brief does not approve trades.
      </p>
      <button className="btn-accent" onClick={start} disabled={running}>
        {result || error ? "Run research again" : "Run Deep Dive"}
      </button>
      {running && (
        <button className="btn-secondary ml-3" onClick={cancel}>
          Cancel research
        </button>
      )}
      <p role="status" className="mt-3 text-sm">
        {status}
      </p>
      {error && (
        <p role="alert" className="mt-3">
          {error}
        </p>
      )}
      {result && !running && !error && (
        <div className="mt-5 space-y-5">
          <h3 className="font-bold">
            {result.status === "investigate" ? "Investigate further" : "Insufficient evidence"}
          </h3>
          <p>
            Generated research summary · Review the excerpts before relying on its interpretation.
          </p>
          <p className="whitespace-pre-wrap">{result.summary}</p>
          {(["supporting", "contradicting"] as const).map((polarity) => (
            <section key={polarity} aria-label={`${polarity} evidence`}>
              <h3 className="font-bold capitalize">{polarity} evidence</h3>
              {result.claims.filter((claim) => claim.polarity === polarity).length === 0 && (
                <p>No {polarity} evidence was supplied. This is a coverage gap.</p>
              )}
              {result.claims
                .filter((claim) => claim.polarity === polarity)
                .map((claim, index) => (
                  <article key={index} className="border-l-2 pl-4 mt-3">
                    <p>{claim.statement}</p>
                    <p className="text-sm my-2">
                      {claim.attribution === "unverified"
                        ? "Unverified attribution"
                        : claim.attribution === "venue_excerpt"
                          ? "Matched venue excerpt · Interpretation unverified"
                          : "Matched search excerpt · Full source unverified"}
                      {claim.pivotal ? " · Pivotal claim" : ""}
                    </p>
                    <blockquote className="whitespace-pre-wrap">{claim.quote}</blockquote>
                    {claim.source_url && claim.attribution !== "unverified" && (
                      <a
                        className="underline"
                        href={claim.source_url}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        Review claim source
                      </a>
                    )}
                  </article>
                ))}
            </section>
          ))}
          <h3 className="font-bold">Missing information and limits</h3>
          <ul className="list-disc pl-5">
            {result.missing_information.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
          <p className="text-sm break-all">
            Research ID: {result.analysis_id} · Source snapshot: {result.context.snapshot_id}
          </p>
          {(result.provider || result.model) && (
            <p className="text-sm">
              Provider: {result.provider || "Not reported"} · Model:{" "}
              {result.model || "Not reported"}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
