import type {
  DeepDiveCost,
  DeepDiveEvidence,
  DeepDiveForecast,
  MarketDeepDiveResult,
} from "../../services/marketDeepDiveService";

const DECISION_LABEL: Record<string, string> = {
  investigate: "Worth investigating (research only, not a trade signal)",
  no_clear_edge: "No clear edge after costs",
  abstain: "Abstain: required checks did not pass",
};

const dash = (value: string | null | undefined) => (value == null ? "Unavailable" : value);

export function MarketSummaryCard({ result }: { result: MarketDeepDiveResult }) {
  const { identity } = result;
  return (
    <section className="surface-panel p-6" aria-labelledby="dd-summary">
      <h2 id="dd-summary" className="text-lg font-bold">
        {identity.question}
      </h2>
      <p className="text-sm my-2">
        Research only · market {identity.market_status} · source retrieved{" "}
        {new Date(identity.as_of).toLocaleString()}
        {identity.selected_outcome ? ` · selected side ${identity.selected_outcome}` : ""}
      </p>
      <p>{result.summary || "No summary was produced."}</p>
      {result.key_insights.length > 0 && (
        <ul className="list-disc pl-5 mt-3">
          {result.key_insights.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function ResolutionRuleCard({ result }: { result: MarketDeepDiveResult }) {
  const { resolution } = result;
  return (
    <section className="surface-panel p-6" aria-labelledby="dd-rules">
      <h2 id="dd-rules" className="text-lg font-bold">
        What determines the outcome?
      </h2>
      <p className="my-2">Rule interpretation: {resolution.verification}</p>
      <blockquote className="whitespace-pre-wrap border-l-2 pl-4">
        {resolution.raw_rule_text || "Rules unavailable. Do not infer them from the title."}
      </blockquote>
      <dl className="grid grid-cols-2 gap-1 text-sm mt-3">
        <dt>Oracle</dt>
        <dd>{dash(resolution.oracle)}</dd>
        <dt>Cutoff</dt>
        <dd>{dash(resolution.cutoff)}</dd>
        <dt>Comparator</dt>
        <dd>{dash(resolution.comparator)}</dd>
        <dt>Threshold</dt>
        <dd>{dash(resolution.threshold)}</dd>
      </dl>
      {resolution.warning_codes.length > 0 && (
        <ul className="list-disc pl-5 mt-3" aria-label="Rule warnings">
          {resolution.warning_codes.map((code) => (
            <li key={code}>{code}</li>
          ))}
        </ul>
      )}
      <a
        className="underline inline-block mt-3"
        href={resolution.source_url}
        target="_blank"
        rel="noopener noreferrer"
      >
        Review original market source
      </a>
    </section>
  );
}

function EvidenceRow({ item }: { item: DeepDiveEvidence }) {
  return (
    <li className="py-2">
      <a className="underline" href={item.url} target="_blank" rel="noopener noreferrer">
        {item.publisher}
      </a>{" "}
      <span className="chip">{item.verification.replace("_", " ")}</span>
      {item.source_family === "social_post" && (
        <span className="chip" data-testid="social-badge">
          Social post · untrusted
        </span>
      )}
      <p className="text-xs">
        Retrieved {new Date(item.retrieved_at).toLocaleString()} · Published{" "}
        {item.published_at ? new Date(item.published_at).toLocaleString() : "date unavailable"}
      </p>
      {item.excerpt && (
        <blockquote className="text-sm border-l-2 pl-3 mt-1">{item.excerpt}</blockquote>
      )}
    </li>
  );
}

export function EvidenceExplorer({ result }: { result: MarketDeepDiveResult }) {
  const byId = new Map(result.evidence.map((item) => [item.evidence_id, item]));
  return (
    <section className="surface-panel p-6" aria-labelledby="dd-evidence">
      <h2 id="dd-evidence" className="text-lg font-bold">
        Evidence and claims
      </h2>
      {result.social && result.social.mode !== "disabled" && (
        <div className="text-sm mt-2" data-testid="social-coverage">
          <p>
            Social source:{" "}
            {result.social.mode === "api"
              ? "authorized API"
              : "search-index snippets (fallback, not an authenticated feed)"}
            . {result.social.posts_used} used, {result.social.duplicates} duplicate,{" "}
            {result.social.deleted} deleted, {result.social.rejected_future} rejected as later than
            the analysis time. Coverage is not comprehensive and social posts never count as
            independent sources.
          </p>
          <ul className="list-disc pl-5">
            {result.social.notices.map((notice) => (
              <li key={notice}>{notice}</li>
            ))}
          </ul>
        </div>
      )}
      {result.claims.length === 0 && <p className="mt-2">No attributable claims were found.</p>}
      <ul className="space-y-4 mt-3">
        {result.claims.map((claim) => (
          <li key={claim.claim_id}>
            <p>
              <strong>{claim.direction}</strong>
              {claim.pivotal ? " · pivotal" : ""} · {claim.attribution}
              {claim.attribution === "unsupported" ? " (not shown as fact)" : ""}
            </p>
            <p>{claim.statement}</p>
            {claim.uncertainty && <p className="text-xs">Uncertainty: {claim.uncertainty}</p>}
            <p className="text-xs">
              {claim.independent_source_count} independent source
              {claim.independent_source_count === 1 ? "" : "s"}
            </p>
            <ul className="divide-y mt-1">
              {claim.cited_evidence_ids.map((id) => {
                const item = byId.get(id);
                return item ? <EvidenceRow key={id} item={item} /> : null;
              })}
            </ul>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function ForecastPanel({ forecast }: { forecast: DeepDiveForecast | null }) {
  const label =
    forecast?.label === "validated_model_forecast"
      ? "Validated model forecast"
      : "No validated forecast";
  return (
    <section className="surface-panel p-6" aria-labelledby="dd-forecast">
      <h2 id="dd-forecast" className="text-lg font-bold">
        Model forecast
      </h2>
      <p className="my-2">{label}</p>
      {forecast && forecast.validity_status !== "unavailable" ? (
        <dl className="grid grid-cols-2 gap-1 text-sm">
          <dt>Calibrated probability</dt>
          <dd>{dash(forecast.calibrated_probability)}</dd>
          <dt>Interval</dt>
          <dd>
            {forecast.interval
              ? `${forecast.interval[0]} to ${forecast.interval[1]}`
              : "Unavailable"}
          </dd>
          <dt>Resolved calibration sample</dt>
          <dd>{forecast.resolved_sample_count}</dd>
          <dt>Model Brier vs market baseline</dt>
          <dd>
            {dash(forecast.brier)} vs {dash(forecast.market_baseline_brier)}
          </dd>
        </dl>
      ) : (
        <p className="text-sm">No forecast is shown. A neutral percentage is never substituted.</p>
      )}
      {forecast && forecast.validity_reasons.length > 0 && (
        <p className="text-xs mt-2">Reasons: {forecast.validity_reasons.join(", ")}</p>
      )}
    </section>
  );
}

export function CostPanel({ cost }: { cost: DeepDiveCost | null }) {
  return (
    <section className="surface-panel p-6" aria-labelledby="dd-cost">
      <h2 id="dd-cost" className="text-lg font-bold">
        Price and cost
      </h2>
      <p className="my-2">Quote status: {cost ? cost.validity_status : "unavailable"}</p>
      {cost && (
        <dl className="grid grid-cols-2 gap-1 text-sm">
          <dt>Best ask</dt>
          <dd>{dash(cost.ask)}</dd>
          <dt>Depth-walked average price</dt>
          <dd>{dash(cost.vwap)}</dd>
          <dt>Net expected value per share</dt>
          <dd>{dash(cost.net_ev)}</dd>
          <dt>Break-even probability</dt>
          <dd>{dash(cost.break_even_probability)}</dd>
          <dt>Quote age</dt>
          <dd>
            {cost.quote_age_ms == null ? "Unavailable" : `${Math.round(cost.quote_age_ms / 1000)}s`}
          </dd>
        </dl>
      )}
      {cost && cost.validity_reasons.length > 0 && (
        <p className="text-xs mt-2">Reasons: {cost.validity_reasons.join(", ")}</p>
      )}
    </section>
  );
}

export function ResearchConclusionCard({ result }: { result: MarketDeepDiveResult }) {
  return (
    <section className="surface-panel p-6" aria-labelledby="dd-conclusion">
      <h2 id="dd-conclusion" className="text-lg font-bold">
        Research conclusion
      </h2>
      <p className="my-2" data-testid="dd-decision">
        {DECISION_LABEL[result.decision]}
      </p>
      <p className="text-xs">Reason codes: {result.reason_codes.join(", ") || "none"}</p>
      <h3 className="font-semibold mt-3">Risks</h3>
      <ul className="list-disc pl-5">
        {result.risks.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
      <h3 className="font-semibold mt-3">What would change this view</h3>
      <ul className="list-disc pl-5">
        {result.invalidation_conditions.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
      <h3 className="font-semibold mt-3">Still unknown</h3>
      <ul className="list-disc pl-5">
        {result.missing_information.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
      <p className="text-xs mt-3">This analysis cannot place orders. Execution eligibility: no.</p>
    </section>
  );
}
