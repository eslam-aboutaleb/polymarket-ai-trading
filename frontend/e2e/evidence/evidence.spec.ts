import { test, expect } from "@playwright/test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import {
  assessEvidence,
  experimentHref,
  fillInfo,
  sumCostLines,
  type OpportunityEvidence,
} from "../../src/lib/evidence";
import EvidenceBadge from "../../src/components/evidence/EvidenceBadge";
import FillStatus from "../../src/components/evidence/FillStatus";
import CostBreakdown from "../../src/components/evidence/CostBreakdown";
import ModeBadge from "../../src/components/evidence/ModeBadge";
import VersionedExperimentLink from "../../src/components/evidence/VersionedExperimentLink";

const NOW = Date.parse("2026-11-25T12:00:00Z");
const good: OpportunityEvidence = {
  quoteObservedAt: "2026-11-25T11:59:50Z",
  ruleHash: "sha256:abc",
  ruleCertainty: "certain",
  tokenMatchesOutcome: true,
  netEdge: 0.04,
  forecastSampleSize: 120,
  resolutionDisputed: false,
  sourceVerified: true,
};
const html = (el: React.ReactElement) => renderToStaticMarkup(el);

test.describe("evidence assessment", () => {
  test("complete passing evidence is verified and allows a positive-edge label", () => {
    const a = assessEvidence(good, NOW);
    expect(a.state).toBe("verified");
    expect(a.positiveNetEdgeAllowed).toBe(true);
  });

  const cases: Array<[string, Partial<OpportunityEvidence>, string]> = [
    ["unknown rule", { ruleCertainty: "unknown", ruleHash: null }, "unknown_rule"],
    ["ambiguous rule", { ruleCertainty: "ambiguous" }, "unknown_rule"],
    ["mismatched token", { tokenMatchesOutcome: false }, "token_mismatch"],
    ["stale book", { quoteObservedAt: "2026-11-25T11:50:00Z" }, "stale_quote"],
    ["unparseable quote time", { quoteObservedAt: "yesterday-ish" }, "stale_quote"],
    ["negative net edge", { netEdge: -0.01 }, "negative_net_edge"],
    ["zero net edge", { netEdge: 0 }, "negative_net_edge"],
    ["insufficient forecast history", { forecastSampleSize: 5 }, "insufficient_forecast_history"],
    ["disputed resolution", { resolutionDisputed: true }, "disputed_resolution"],
  ];
  for (const [name, patch, code] of cases) {
    test(`${name} blocks and never shows a positive-edge badge`, () => {
      const ev = { ...good, ...patch };
      const a = assessEvidence(ev, NOW);
      expect(a.state).toBe("blocked");
      expect(a.blockers).toContain(code);
      expect(a.positiveNetEdgeAllowed).toBe(false);
      const out = html(createElement(EvidenceBadge, { evidence: ev, now: NOW }));
      expect(out).toContain('data-evidence-state="blocked"');
      expect(out).toContain("Not actionable");
      expect(out).not.toContain("Positive net edge");
      expect(out).not.toMatch(/profitable/i);
    });
  }

  test("missing evidence is indicative, never verified", () => {
    for (const ev of [null, undefined, {}, { netEdge: 0.2 }]) {
      const a = assessEvidence(ev, NOW);
      expect(a.state).toBe("indicative");
      expect(a.positiveNetEdgeAllowed).toBe(false);
    }
    const out = html(createElement(EvidenceBadge, { evidence: { netEdge: 0.2 }, now: NOW }));
    expect(out).toContain("evidence incomplete");
    expect(out).toContain("Missing: rule hash");
    expect(out).not.toContain("Positive net edge");
  });

  test("verified badge says estimate, not earnings", () => {
    const out = html(createElement(EvidenceBadge, { evidence: good, now: NOW }));
    expect(out).toContain("Positive net edge (estimate, not earnings)");
  });
});

test.describe("fill status", () => {
  test("only an exchange-confirmed full fill reads as completed", () => {
    for (const s of [
      "accepted",
      "submitted",
      "acknowledged",
      "live",
      "partially_filled",
      "partial",
      "weird",
      "",
      null,
      undefined,
    ]) {
      expect(fillInfo(s).completed).toBe(false);
      expect(fillInfo(s).pending).toBe(true);
    }
    expect(fillInfo("filled").completed).toBe(true);
    expect(fillInfo("rejected").pending).toBe(false);
  });

  test("delayed and partial fills do not render as complete", () => {
    const partial = html(
      createElement(FillStatus, { status: "partial", filledSize: 40, orderedSize: 100 }),
    );
    expect(partial).toContain('data-completed="false"');
    expect(partial).toContain("40 of 100 shares confirmed");
    expect(partial).toContain('role="status"');
    const accepted = html(createElement(FillStatus, { status: "accepted" }));
    expect(accepted).toContain("Accepted, not filled");
    expect(accepted).not.toContain("Filled (exchange confirmed)");
    const done = html(createElement(FillStatus, { status: "filled" }));
    expect(done).toContain('data-completed="true"');
  });
});

test.describe("cost breakdown, modes, experiment link", () => {
  test("unknown cost lines are labelled and the total is marked partial", () => {
    const lines = [
      { label: "Fee", amount: -0.25, measured: true },
      { label: "Slippage", amount: null },
    ];
    expect(sumCostLines(lines)).toEqual({ total: -0.25, hasUnknown: true });
    const out = html(createElement(CostBreakdown, { lines }));
    expect(out).toContain("unknown");
    expect(out).toContain("Total of known lines");
    expect(out).toContain("(modeled)");
    expect(out).toContain("-$0.25");
  });

  test("every mode has a text label, not only color", () => {
    for (const [mode, text] of [
      ["research", "Research only"],
      ["shadow", "Shadow"],
      ["paper", "Paper (notional)"],
      ["live_disabled", "Live disabled"],
    ] as const) {
      expect(html(createElement(ModeBadge, { mode }))).toContain(text);
    }
  });

  test("experiment link needs all versions", () => {
    expect(experimentHref({ experimentId: "e1", datasetVersion: "d2", feeVersion: "f3" })).toBe(
      "/backtesting?experiment=e1&dataset=d2&fees=f3",
    );
    expect(experimentHref({ experimentId: "e1", datasetVersion: "d2" })).toBeNull();
    const missing = html(
      createElement(
        MemoryRouter,
        null,
        createElement(VersionedExperimentLink, { experimentId: "e1" }),
      ),
    );
    expect(missing).toContain("No reproducible report");
    const ok = html(
      createElement(
        MemoryRouter,
        null,
        createElement(VersionedExperimentLink, {
          experimentId: "e1",
          datasetVersion: "d2",
          feeVersion: "f3",
        }),
      ),
    );
    expect(ok).toContain('href="/backtesting?experiment=e1&amp;dataset=d2&amp;fees=f3"');
  });
});
