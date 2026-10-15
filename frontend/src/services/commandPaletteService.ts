/**
 * Client-side router for the voice/chat command palette.
 *
 * Plan 01's orchestration route is a *planner* API: it takes one of three
 * registered intents (`market_deep_dive`, `opportunity_sweep`,
 * `trader_diligence`) plus the payload that planner reads, and it has no
 * order capability at all — the furthest any plan reaches is
 * `GenerateTradePlan`, which returns prose the user still has to act on.
 * This module turns a free-text question into that shape:
 *
 * 1. `classifyQuery` parses the text (wallet address, price, position size,
 *    the market it names) with plain regexes — no NLU library, so the
 *    routing is auditable and unit-testable;
 * 2. `buildRequest` fills only the fields the chosen planner reads, with the
 *    same defaults `OrchestratorPage` uses;
 * 3. `runPaletteQuery` streams the plan through
 *    `orchestrationService.streamOrchestrate` against
 *    `/api/analysis/orchestrate/stream`.
 *
 * The original phase text asked for `intent="natural_language"`. No such
 * planner exists: `PLANNERS` in `services/llm-chain/src/orchestrator.py`
 * registers exactly three keys and an unknown one is rejected with HTTP 400
 * (`backend/tests/test_orchestration.py` asserts it), so the palette
 * resolves the intent locally and shows which planner it picked.
 *
 * ## Why this module cannot place an order
 *
 * `PaletteActionLink` is a link, not a button: its only navigational field
 * is `to`, typed as the closed union `PaletteLinkRoute`, and the type has no
 * `onClick`/`onSubmit` slot, so a trade action cannot be expressed without a
 * compile error. `PALETTE_ENDPOINTS` lists every network path reachable from
 * here — the orchestration stream plus one public market GET — and
 * `assertReadOnlySurface` re-checks that list and every action at import
 * time, throwing if a money-path path or a non-GET ever appears.
 *
 * @module services/commandPaletteService
 */

import { marketsService, type BrowseMarket } from "./marketsService";
import {
  orchestrationService,
  type OrchestrateRequest,
  type OrchestrationStep,
} from "./orchestrationService";

// ── Read-only link surface ─────────────────────────────────────────

/** Pages the palette may link to. None of them can execute an order. */
export const PALETTE_LINKS = {
  markets: "/markets",
  opportunities: "/opportunities",
  analysis: "/analysis",
  portfolio: "/",
  orchestrator: "/orchestrator",
  leaderboard: "/leaderboard",
} as const;

export type PaletteLinkRoute = (typeof PALETTE_LINKS)[keyof typeof PALETTE_LINKS];

const PALETTE_ROUTES: readonly PaletteLinkRoute[] = Object.freeze(Object.values(PALETTE_LINKS));

/**
 * One suggested next step. Always a link: the `kind` discriminant and the
 * handler-free shape are what keep "suggest an action" from turning into
 * "perform an action".
 */
export interface PaletteActionLink {
  readonly kind: "link";
  readonly id: string;
  readonly label: string;
  readonly hint: string;
  readonly to: PaletteLinkRoute;
}

const ORCHESTRATION_STREAM_PATH = "/api/analysis/orchestrate/stream";

/** Every HTTP path the palette can reach, directly or transitively. */
export interface PaletteEndpoint {
  readonly path: string;
  readonly method: "GET" | "POST";
  readonly purpose: string;
}

export const PALETTE_ENDPOINTS: readonly PaletteEndpoint[] = Object.freeze([
  {
    path: ORCHESTRATION_STREAM_PATH,
    method: "POST",
    purpose: "stream an analysis plan (synthesis only — no order capability)",
  },
  {
    path: "/api/markets/search",
    method: "GET",
    purpose: "public market list used to seed an opportunity sweep",
  },
]);

/** Path fragments that only ever belong to the money path. */
const MONEY_PATH_PATTERN =
  /\/api\/(trades|orders|market-maker|copy-trading|stop-loss|take-profit|inverse-bot|trading-key|emergency|api-keys)/i;

/**
 * Re-check the read-only guarantee at runtime.
 *
 * Called once at import time; throws rather than degrading, so a future edit
 * that adds a money-path endpoint or an action pointing outside the
 * allowlist fails immediately in dev and in tests instead of shipping.
 */
export function assertReadOnlySurface(
  actions: readonly PaletteActionLink[] = ALL_PALETTE_ACTIONS,
): void {
  for (const endpoint of PALETTE_ENDPOINTS) {
    if (MONEY_PATH_PATTERN.test(endpoint.path)) {
      throw new Error(`command palette reaches the money path: ${endpoint.path}`);
    }
    // The orchestration stream is a POST, but it only ever returns a
    // synthesis; anything else must be a read.
    if (endpoint.method !== "GET" && endpoint.path !== ORCHESTRATION_STREAM_PATH) {
      throw new Error(`command palette would call ${endpoint.method} ${endpoint.path}`);
    }
  }

  for (const action of actions) {
    if (action.kind !== "link") {
      throw new Error(`palette action "${action.id}" is not a link`);
    }
    if (!PALETTE_ROUTES.includes(action.to)) {
      throw new Error(`palette action "${action.id}" points outside the palette: ${action.to}`);
    }
  }
}

// ── Intents ────────────────────────────────────────────────────────

/** The only planners the service knows; `PALETTE_INTENTS` is the allowlist. */
export const PALETTE_INTENTS = [
  "market_deep_dive",
  "opportunity_sweep",
  "trader_diligence",
] as const;

export type PaletteIntent = (typeof PALETTE_INTENTS)[number];

/** Step names each planner runs, in order (mirrors `OrchestratorPage`). */
export const PALETTE_PLANNED_STEPS: Record<PaletteIntent, readonly string[]> = {
  market_deep_dive: ["analyze_market", "assess_risk", "trade_plan"],
  opportunity_sweep: ["scan_markets", "deep_dive_1", "deep_dive_2", "deep_dive_3", "assess_risk"],
  trader_diligence: ["analyze_trader", "evaluate_copy_trade", "assess_risk"],
};

export const PALETTE_INTENT_LABEL: Record<PaletteIntent, string> = {
  market_deep_dive: "Market Deep Dive",
  opportunity_sweep: "Opportunity Sweep",
  trader_diligence: "Trader Diligence",
};

/** Suggested next steps per planner — links only, never actions. */
export function actionsForIntent(intent: PaletteIntent): readonly PaletteActionLink[] {
  const openMarkets: PaletteActionLink = {
    kind: "link",
    id: "markets",
    label: "Open Markets",
    hint: "Browse live Polymarket markets and prices.",
    to: PALETTE_LINKS.markets,
  };
  const openOrchestrator: PaletteActionLink = {
    kind: "link",
    id: "orchestrator",
    label: "Run in the Orchestrator",
    hint: "Fill the payload form by hand and run the same plan.",
    to: PALETTE_LINKS.orchestrator,
  };

  if (intent === "opportunity_sweep") {
    return [
      openMarkets,
      {
        kind: "link",
        id: "opportunities",
        label: "AI Opportunities",
        hint: "The ranked opportunity list the sweep draws on.",
        to: PALETTE_LINKS.opportunities,
      },
      openOrchestrator,
    ];
  }

  if (intent === "trader_diligence") {
    return [
      {
        kind: "link",
        id: "leaderboard",
        label: "Leaderboard",
        hint: "Compare this trader against the tracked leaderboard.",
        to: PALETTE_LINKS.leaderboard,
      },
      openMarkets,
      openOrchestrator,
    ];
  }

  return [
    openMarkets,
    {
      kind: "link",
      id: "analysis",
      label: "Deep Analysis",
      hint: "Run the full analysis on one market.",
      to: PALETTE_LINKS.analysis,
    },
    openOrchestrator,
  ];
}

const ALL_PALETTE_ACTIONS: readonly PaletteActionLink[] = Object.freeze(
  PALETTE_INTENTS.flatMap((intent) => actionsForIntent(intent)),
);

// ── Query parsing (pure) ───────────────────────────────────────────

const WALLET_PATTERN = /0x[a-f0-9]{40}\b/i;

/** A quoted phrase that asks for several markets at once. */
const SWEEP_SIGNAL =
  /(opportunit|sweep|\bscan\b|misprice|arbitrage|\barb\b|best market|top market|what should i (?:look|buy|trade)|anything (?:to|worth)|edge)/i;

/** A query that is nothing but that request ("markets", "opportunities?"). */
const BARE_SWEEP_QUERY = /^(?:markets?|opportunities|opportunity|scan|sweep|market scan)[.?!\s]*$/;

/** Defaults the planners apply when the query is silent, same as the form. */
const DEFAULT_YES_PRICE = 0.5;
const DEFAULT_POSITION_SIZE = 100;
const MAX_TITLE_LENGTH = 160;

export function extractWalletAddress(query: string): string | null {
  const match = query.match(WALLET_PATTERN);
  return match ? match[0].toLowerCase() : null;
}

/**
 * Read a YES price out of the text: `0.62`, `62 cents`, `62¢` or `62%`.
 * The leading boundary is a character class rather than a lookbehind so the
 * bundle still parses on Safari < 16.4.
 */
export function extractPrice(query: string): number | null {
  const decimal = query.match(/(?:^|[^0-9A-Za-z.])(0?\.[0-9]{1,3})(?![0-9.])/);
  if (decimal) {
    const value = Number(decimal[1]);
    if (value > 0 && value < 1) return value;
  }

  const cents = query.match(/(?:^|[^0-9A-Za-z])([0-9]{1,2})\s*(?:¢|cents?|c)(?![0-9A-Za-z])/i);
  if (cents) {
    const value = Number(cents[1]) / 100;
    if (value > 0 && value < 1) return value;
  }

  const percent = query.match(/(?:^|[^0-9A-Za-z])([0-9]{1,3})\s*%(?![0-9A-Za-z])/);
  if (percent) {
    const value = Number(percent[1]) / 100;
    if (value > 0 && value < 1) return value;
  }

  return null;
}

/** Read a position size: `$250`, `250 usdc`, `250 dollars`. */
export function extractPositionSize(query: string): number | null {
  const dollar = query.match(/\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)/);
  if (dollar) return clampSize(Number(dollar[1].replace(/,/g, "")));

  const worded = query.match(
    /(?:^|[^0-9A-Za-z])([0-9][0-9,]*)\s*(?:usdc|usd|dollars?|bucks?)(?![0-9A-Za-z])/i,
  );
  if (worded) return clampSize(Number(worded[1].replace(/,/g, "")));

  return null;
}

function clampSize(value: number): number | null {
  if (!Number.isFinite(value) || value <= 0) return null;
  return Math.min(Math.round(value * 100) / 100, 1_000_000);
}

/**
 * Conversational scaffolding that precedes the market name. Applied
 * repeatedly, longest-match first, so "Should I buy YES on the Fed rate
 * decision market?" leaves "Fed rate decision market".
 */
const TITLE_STRIP_PATTERNS: readonly RegExp[] = [
  /^(?:hey|hi|hello|ok|okay|please)\b[\s,]*/i,
  /^(?:can|could|would|will)\s+you\b[\s,]*/i,
  /^(?:i\s+want\s+to\s+know|i'?d\s+like\s+to\s+know|i\s+want|i'?d\s+like)\b[\s,]*/i,
  /^(?:what's|whats|what|how's|how|whether|which|when|where)\b[\s,]*/i,
  /^(?:do|does|did|is|are|was|were|should|shall|can|could|will|would)\b[\s,]*(?:i|we|you|it|they)?\b[\s,]*/i,
  /^(?:think|thought)\s+(?:about|of|on)\b[\s,]*/i,
  /^(?:tell|show|give|explain|walk)\s+(?:me|us)?\s*(?:about|through)?\b[\s,]*/i,
  /^(?:analyse|analyze|assess|evaluate|review|check|look\s+at|dig\s+into|deep\s+dive(?:\s+(?:on|into))?|scan(?:\s+for)?)\b[\s,]*/i,
  /^(?:should\s+i\s+)?(?:buy|sell|trade|invest|enter|exit|add\s+to)\b[\s,]*(?:yes|no)?\b[\s,]*/i,
  /^(?:copy|copying|follow|following)\b[\s,]*/i,
  /^worth\s+(?:following|copying|watching)\b[\s,]*/i,
  /^(?:risk|odds|probability|chances?|outlook|verdict|view|analysis|sentiment|edge|fair\s+value)\b[\s,]*(?:of|on|for|in)?\b[\s,]*/i,
  /^(?:the\s+)?(?:market|question|event)\b[\s,]*/i,
  /^(?:on|in|about|for|at|of|with|to|from|against)\b[\s,]*/i,
  /^(?:this|that|it)\b[\s,]*/i,
  /^(?:trader|wallet|guy|person|address)\b[\s,]*/i,
  // Last, so a leading article only falls off once the phrases it guarded
  // are gone: "the Fed rate decision" → "Fed rate decision".
  /^(?:a|an|the)\s+/i,
];

/**
 * Trailing price/size clauses, so "…recession market at 0.31 with $500"
 * leaves "recession market" and the numbers reach the payload instead.
 */
const TITLE_TAIL_PATTERNS: readonly RegExp[] = [
  /\s*\b(?:at|@|around|about|near|price(?:d)?(?:\s+at)?)\s*(?:0?\.[0-9]{1,3}|[0-9]{1,2}\s*(?:¢|cents?)|[0-9]{1,3}\s*%)\s*$/i,
  /\s*\b(?:with|for|size(?:d)?(?:\s+at)?)\s*(?:\$\s*[0-9][0-9,]*(?:\.[0-9]+)?|[0-9][0-9,]*\s*(?:usdc|usd|dollars?|bucks?))\s*$/i,
  /\s*\$\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*$/,
];

function stripRoutingPhrases(text: string): string {
  let current = text.trim();

  for (let pass = 0; pass < 8; pass += 1) {
    const before = current;
    for (const pattern of TITLE_STRIP_PATTERNS) {
      current = current.replace(pattern, "");
    }
    for (const pattern of TITLE_TAIL_PATTERNS) {
      current = current.replace(pattern, "");
    }
    // Trailing punctuation goes before the next pass, otherwise "at 62 cents?"
    // never matches the tail patterns above.
    current = current.replace(/[\s\-–—:,;?!.]+$/, "").trim();
    current = current.replace(/^[\s\-–—:,;.]+/, "").trim();
    if (current === before) break;
  }

  return current;
}

/**
 * The market the query names, or an empty string when it names none.
 * An empty title blocks the deep-dive planner, which requires one.
 */
export function extractMarketTitle(query: string): string {
  const stripped = stripRoutingPhrases(query.replace(WALLET_PATTERN, " "));
  const cleaned = stripped
    .replace(/\s+/g, " ")
    .replace(/[\s\-–—:,;.?]+$/, "")
    .trim()
    .slice(0, MAX_TITLE_LENGTH);

  return cleaned.length >= 3 ? cleaned : "";
}

/** Words that are routing noise rather than a topic for a market search. */
const TOPIC_STOPWORDS: ReadonlySet<string> = new Set([
  "a",
  "alpha",
  "about",
  "after",
  "all",
  "am",
  "an",
  "and",
  "any",
  "anything",
  "arb",
  "arbs",
  "arbitrage",
  "are",
  "around",
  "as",
  "at",
  "be",
  "best",
  "biggest",
  "bring",
  "buy",
  "can",
  "could",
  "current",
  "currently",
  "day",
  "days",
  "dig",
  "do",
  "does",
  "edge",
  "find",
  "for",
  "from",
  "get",
  "give",
  "going",
  "good",
  "great",
  "has",
  "have",
  "how",
  "hunt",
  "i",
  "in",
  "inside",
  "into",
  "is",
  "it",
  "its",
  "just",
  "let",
  "let's",
  "like",
  "list",
  "look",
  "looking",
  "market",
  "markets",
  "me",
  "moment",
  "misprice",
  "mispriced",
  "most",
  "much",
  "my",
  "need",
  "new",
  "now",
  "of",
  "on",
  "opportunities",
  "opportunity",
  "or",
  "out",
  "pick",
  "please",
  "potential",
  "promising",
  "put",
  "right",
  "run",
  "scan",
  "search",
  "see",
  "should",
  "show",
  "something",
  "spot",
  "sweep",
  "tell",
  "than",
  "that",
  "the",
  "their",
  "there",
  "these",
  "they",
  "this",
  "those",
  "time",
  "to",
  "today",
  "top",
  "trade",
  "trading",
  "untitled",
  "up",
  "us",
  "want",
  "was",
  "watch",
  "we",
  "what",
  "what's",
  "whats",
  "when",
  "where",
  "which",
  "why",
  "will",
  "with",
  "worth",
  "would",
  "you",
  "your",
]);

/** Topic words left over for a sweep search, e.g. "politics". */
export function extractTopic(query: string): string {
  const words = query
    .toLowerCase()
    .replace(/[^a-z0-9\s'-]/g, " ")
    .split(/\s+/)
    .filter((word) => word.length > 1 && !TOPIC_STOPWORDS.has(word));

  return Array.from(new Set(words)).slice(0, 4).join(" ");
}

export interface PaletteClassification {
  /** Planner the query was routed to. */
  intent: PaletteIntent;
  /** The question that was classified, trimmed. */
  query: string;
  /** Why that planner was picked — shown under the answer. */
  reason: string;
  /** Market named in the query, empty when none. */
  title: string;
  /** Topic words for a sweep search, empty for "everything". */
  topic: string;
  wallet: string | null;
  price: number | null;
  positionSize: number | null;
  /** Set when the query cannot be run as written. */
  blocked: string | null;
}

export function classifyQuery(query: string): PaletteClassification {
  const trimmed = query.trim();
  const wallet = extractWalletAddress(trimmed);
  const price = extractPrice(trimmed);
  const positionSize = extractPositionSize(trimmed);
  const title = extractMarketTitle(trimmed);
  const topic = extractTopic(trimmed);

  if (wallet) {
    return {
      intent: "trader_diligence",
      query: trimmed,
      reason: `A wallet address was found, so this runs the Trader Diligence plan on ${wallet.slice(0, 10)}…`,
      title,
      topic,
      wallet,
      price,
      positionSize,
      blocked: null,
    };
  }

  if (BARE_SWEEP_QUERY.test(trimmed) || SWEEP_SIGNAL.test(trimmed)) {
    return {
      intent: "opportunity_sweep",
      query: trimmed,
      reason: "This asks for several markets at once, so it runs the Opportunity Sweep plan.",
      title,
      topic,
      wallet,
      price,
      positionSize,
      blocked: null,
    };
  }

  return {
    intent: "market_deep_dive",
    query: trimmed,
    reason: "This names one market, so it runs the Market Deep Dive plan.",
    title,
    topic,
    wallet,
    price,
    positionSize,
    blocked: title
      ? null
      : "Name the market you want analysed — for example “analyse the Fed rate decision market”.",
  };
}

// ── Request building ───────────────────────────────────────────────

export interface PalettePlan {
  query: string;
  classification: PaletteClassification;
  request: OrchestrateRequest;
  actions: readonly PaletteActionLink[];
  /** How many markets a sweep was seeded with (0 for the other planners). */
  marketCount: number;
}

/**
 * Map a browse result onto the market dict the sweep planner reads.
 * Live order-book prices win, exactly like `Markets.tsx` does.
 */
function toSweepMarket(market: BrowseMarket): Record<string, unknown> {
  const title = market.question || market._event_title || "Untitled market";
  const bestAsk = market.bestAsk != null ? Number(market.bestAsk) : NaN;
  const bestBid = market.bestBid != null ? Number(market.bestBid) : NaN;
  const lastTrade = market.lastTradePrice != null ? Number(market.lastTradePrice) : NaN;

  let yesPrice = NaN;
  if (!Number.isNaN(bestAsk) && bestAsk > 0 && bestAsk < 1) yesPrice = bestAsk;
  else if (!Number.isNaN(bestBid) && bestBid > 0 && bestBid < 1) yesPrice = bestBid;
  else if (!Number.isNaN(lastTrade) && lastTrade > 0 && lastTrade < 1) yesPrice = lastTrade;

  if (Number.isNaN(yesPrice) && market.outcomePrices) {
    try {
      const prices =
        typeof market.outcomePrices === "string"
          ? JSON.parse(market.outcomePrices)
          : market.outcomePrices;
      yesPrice = Number(Array.isArray(prices) ? prices[0] : NaN);
    } catch {
      yesPrice = NaN;
    }
  }

  if (!Number.isFinite(yesPrice) || yesPrice <= 0 || yesPrice >= 1) yesPrice = DEFAULT_YES_PRICE;
  const volume = market.volume24hr ?? market._event_volume_24hr ?? market.volumeNum ?? 0;

  return {
    title,
    description: market._event_title || title,
    yes_price: Math.round(yesPrice * 10000) / 10000,
    no_price: Math.round((1 - yesPrice) * 10000) / 10000,
    volume_24h: Number(volume) || 0,
    end_date: market.endDate || market.end_date_iso || "",
  };
}

/** How many markets a sweep is seeded with (the planner deep-dives the top 3). */
const SWEEP_MARKET_LIMIT = 8;

/** Sweep seed: the busiest markets, optionally narrowed by the topic. */
async function fetchSweepMarkets(topic: string): Promise<Record<string, unknown>[]> {
  const response = await marketsService.searchMarkets({
    q: topic || undefined,
    limit: SWEEP_MARKET_LIMIT,
    sort: "volume24hr",
  });
  return (response.markets ?? []).map(toSweepMarket);
}

/** Compose the orchestrator request for a classified query. */
export function buildRequest(
  classification: PaletteClassification,
  sweepMarkets: readonly Record<string, unknown>[] = [],
): OrchestrateRequest {
  const { intent, title, price, positionSize } = classification;
  const yesPrice = price ?? DEFAULT_YES_PRICE;
  const size = positionSize ?? DEFAULT_POSITION_SIZE;

  if (intent === "opportunity_sweep") {
    return {
      intent,
      payload: { markets: [...sweepMarkets], query: classification.query },
    };
  }

  if (intent === "trader_diligence") {
    return {
      intent,
      payload: {
        wallet_address: classification.wallet ?? "",
        market_title: title || "Untitled market",
        current_price: yesPrice,
        trade_size: size,
        position_size: size,
        query: classification.query,
      },
    };
  }

  return {
    intent,
    payload: {
      market_title: title,
      yes_price: yesPrice,
      no_price: Math.round((1 - yesPrice) * 10000) / 10000,
      position_size: size,
      query: classification.query,
    },
  };
}

export interface PaletteRunCallbacks {
  onStep: (step: OrchestrationStep) => void;
  onRecommendation: (synthesis: string, partial: boolean) => void;
  onError: (message: string) => void;
  onDone?: (payload?: Record<string, unknown>) => void;
}

export interface PaletteRun {
  controller: AbortController;
  plan: PalettePlan;
}

/**
 * Classify a question, seed it, and stream the plan.
 *
 * Rejects with a user-facing message when the query names no market or the
 * seed fetch fails, so the palette can show one inline error instead of a
 * backend 400.
 */
export async function runPaletteQuery(
  query: string,
  callbacks: PaletteRunCallbacks,
): Promise<PaletteRun> {
  const trimmed = query.trim();
  const classification = classifyQuery(trimmed);
  if (classification.blocked) {
    throw new Error(classification.blocked);
  }

  let sweepMarkets: Record<string, unknown>[] = [];
  if (classification.intent === "opportunity_sweep") {
    try {
      sweepMarkets = await fetchSweepMarkets(classification.topic);
    } catch {
      throw new Error(
        "Could not load the market list for a sweep. Open the Orchestrator and paste the markets you care about instead.",
      );
    }
    if (sweepMarkets.length === 0) {
      throw new Error(
        "No markets matched that sweep. Try naming a category, or paste markets into the Orchestrator.",
      );
    }
  }

  const plan: PalettePlan = {
    query: trimmed,
    classification,
    request: buildRequest(classification, sweepMarkets),
    actions: actionsForIntent(classification.intent),
    marketCount: sweepMarkets.length,
  };

  const controller = orchestrationService.streamOrchestrate(plan.request, callbacks);
  return { controller, plan };
}

// ── Recent queries ─────────────────────────────────────────────────

export const RECENT_QUERIES_KEY = "polymarket_command_palette_recent";
export const MAX_RECENT_QUERIES = 5;

export function loadRecentQueries(): string[] {
  try {
    const raw = window.localStorage.getItem(RECENT_QUERIES_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item): item is string => typeof item === "string" && item.trim().length > 0)
      .slice(0, MAX_RECENT_QUERIES);
  } catch {
    // Private-mode / unparsable storage must not break the palette.
    return [];
  }
}

/** Push a query to the front of the recent list and persist it. */
export function rememberQuery(query: string): string[] {
  const trimmed = query.trim();
  const next = [trimmed, ...loadRecentQueries().filter((item) => item !== trimmed)].slice(
    0,
    MAX_RECENT_QUERIES,
  );

  try {
    window.localStorage.setItem(RECENT_QUERIES_KEY, JSON.stringify(next));
  } catch {
    // Storage is a convenience, never a requirement.
  }

  return next;
}

/** Shown when there is no history yet; each one routes somewhere different. */
export const EXAMPLE_QUERIES: readonly string[] = [
  "Should I buy YES on the Fed rate decision market?",
  "What is the risk on the 2028 election market at 62 cents?",
  "Should I copy 0x0000000000000000000000000000000000000001 on the bitcoin market with $250?",
  "Find the best opportunities in politics right now",
];

// Import-time self-check: a widened surface fails loudly rather than shipping.
assertReadOnlySurface();
