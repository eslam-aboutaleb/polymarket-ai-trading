/**
 * Groups a flat list of markets by their parent event, using _event_slug.
 * Markets sharing the same _event_slug are grouped into one EventGroup.
 */

/** Minimal shape a market must have for grouping */
interface Groupable {
  _event_slug?: string;
  _event_title?: string;
  _event_image?: string;
  _event_volume?: string;
  _event_liquidity?: string;
  _event_volume_24hr?: number;
  question?: string;
  groupItemTitle?: string;
}

export interface EventGroup<T extends Groupable> {
  /** Unique key for this group (the event slug, or a fallback) */
  eventSlug: string;
  /** Display title for the event panel header */
  eventTitle: string;
  /** Event image URL */
  eventImage?: string;
  /** Aggregate event-level volume (from Gamma API) */
  eventVolume?: string;
  /** Aggregate event-level liquidity (from Gamma API) */
  eventLiquidity?: string;
  /** 24-hour volume for the event */
  eventVolume24hr?: number;
  /** All sub-markets belonging to this event */
  markets: T[];
  /** True when the event contains only one market (render as a regular card) */
  isSingle: boolean;
}

export type PrimaryOptionSource = "ai_recommended" | "popularity";

export interface ResolvedPrimaryOption<T extends Groupable> {
  market: T;
  index: number;
  label: string;
  prices: { yes: number; no: number };
  source: PrimaryOptionSource;
}

/**
 * Group markets by `_event_slug`. Markets without an event slug are treated
 * as unique single-market events.
 */
export function groupMarketsByEvent<T extends Groupable>(
  markets: T[],
): EventGroup<T>[] {
  const map = new Map<string, EventGroup<T>>();
  let uniqCounter = 0;

  for (const m of markets) {
    const slug = m._event_slug || `__unique_${uniqCounter++}`;
    const existing = map.get(slug);
    if (existing) {
      existing.markets.push(m);
      existing.isSingle = false;
    } else {
      map.set(slug, {
        eventSlug: slug,
        eventTitle: m._event_title || m.question || "Unknown Event",
        eventImage: m._event_image,
        eventVolume: m._event_volume,
        eventLiquidity: m._event_liquidity,
        eventVolume24hr: m._event_volume_24hr,
        markets: [m],
        isSingle: true,
      });
    }
  }

  return Array.from(map.values());
}

/**
 * Derive a short sub-market label for display inside grouped panels.
 *
 * Priority:
 *  1. `groupItemTitle` from the Gamma API (e.g. "Before July 2026")
 *  2. The suffix of `question` that differs from `_event_title`
 *  3. The full `question`
 */
export function getSubMarketLabel<T extends Groupable>(
  market: T,
  eventTitle: string,
): string {
  // 1. Prefer explicit Gamma group-item title
  if (market.groupItemTitle) return market.groupItemTitle;

  const q = market.question || "";
  if (!q) return "Option";

  // 2. Try to extract the differentiating suffix
  //    e.g. event = "Will USA strike Iran?"
  //         question = "Will USA strike Iran before July 2026?"
  //    → suffix = "before July 2026?"
  const normEvent = eventTitle
    .replace(/[?.!]+$/, "")
    .trim()
    .toLowerCase();
  const normQ = q
    .replace(/[?.!]+$/, "")
    .trim()
    .toLowerCase();

  if (normQ.startsWith(normEvent) && normQ.length > normEvent.length) {
    // The question extends the event title — grab the extra part
    const suffix = q
      .slice(normEvent.length)
      .replace(/^[\s\-–—:]+/, "")
      .trim();
    if (suffix.length > 0) {
      // Capitalize first letter
      return suffix.charAt(0).toUpperCase() + suffix.slice(1);
    }
  }

  // 3. If the question and event title are nearly the same, use a short label
  if (normQ === normEvent) return "Main";

  // 4. Fallback: full question
  return q;
}

/** Normalize labels for tolerant matching (trim + lowercase + punctuation-insensitive). */
export function normalizeOptionLabel(label: string | undefined): string {
  if (!label) return "";
  return label
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function labelsMatch(a: string, b: string): boolean {
  if (!a || !b) return false;
  if (a === b) return true;
  return a.includes(b) || b.includes(a);
}

/**
 * Resolve one primary option for a grouped event:
 *  1) AI-recommended option label match
 *  2) Highest Yes probability
 */
export function resolvePrimaryGroupOption<T extends Groupable>(
  group: EventGroup<T>,
  options: {
    parsePrices: (market: T) => { yes: number; no: number };
    preferredOptionLabel?: string;
  },
): ResolvedPrimaryOption<T> | null {
  if (!group.markets.length) return null;

  const withPrices = group.markets.map((market, index) => ({
    prices: (() => {
      const parsed = options.parsePrices(market);
      const yes = Number.isFinite(parsed.yes) ? parsed.yes : 0.5;
      const no = Number.isFinite(parsed.no) ? parsed.no : 1 - yes;
      return { yes, no };
    })(),
    market,
    index,
    label: getSubMarketLabel(market, group.eventTitle),
    normalizedGroupItemTitle: normalizeOptionLabel(market.groupItemTitle),
    normalizedQuestion: normalizeOptionLabel(market.question),
  }));

  const preferred = normalizeOptionLabel(options.preferredOptionLabel);
  if (preferred) {
    const matched = withPrices.find((item) => {
      return (
        labelsMatch(preferred, item.normalizedGroupItemTitle) ||
        labelsMatch(preferred, item.normalizedQuestion)
      );
    });
    if (matched) {
      return {
        market: matched.market,
        index: matched.index,
        label: matched.label,
        prices: matched.prices,
        source: "ai_recommended",
      };
    }
  }

  let best = withPrices[0];
  for (let i = 1; i < withPrices.length; i++) {
    if (withPrices[i].prices.yes > best.prices.yes) best = withPrices[i];
  }

  return {
    market: best.market,
    index: best.index,
    label: best.label,
    prices: best.prices,
    source: "popularity",
  };
}
