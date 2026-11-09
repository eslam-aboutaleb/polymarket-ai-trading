/** Controlled public-screen data; backend provider contracts have separate integration suites. */
import type { Page } from "@playwright/test";
import type { LeaderboardEntry } from "../../src/services/tradesService";
import type { BrowseMarket } from "../../src/services/marketsService";

export async function publicLeaderboard(page: Page) {
  await page.route("**/api/trades/leaderboard?*", (route) => {
    const query = new URL(route.request().url()).searchParams;
    const count = Math.min(100, Number(query.get("limit") ?? 25));
    const entries: LeaderboardEntry[] = Array.from({ length: count }, (_, index) => ({
      rank: index + 1,
      address: "0x" + (index + 1).toString(16).padStart(40, "0"),
      display_name: `Recorded trader ${index + 1}`,
      profit_loss: 1000 - index,
      volume: 10000,
      markets_traded: 10,
      positions_value: 50,
      pnl_24h: 10,
      pnl_7d: 20,
      pnl_30d: 30,
      volume_24h: 100,
      is_followed: false,
      is_notification_followed: false,
    }));
    return route.fulfill({
      json: {
        entries,
        period: query.get("period"),
        total: entries.length,
        updated_at: new Date().toISOString(),
      },
    });
  });
}

export async function publicMarkets(page: Page) {
  const markets: BrowseMarket[] = [
    {
      condition_id: "0x" + "11".repeat(32),
      question: "Will Bitcoin reach the target price?",
      _event_title: "Bitcoin price targets",
      _event_slug: "bitcoin-targets",
      groupItemTitle: "$100,000",
      outcomePrices: [".60", ".40"],
      volume24hr: 2000,
      volume: 20000,
      endDate: "2099-12-31T00:00:00Z",
      tokens: [
        { token_id: "11", outcome: "Yes" },
        { token_id: "22", outcome: "No" },
      ],
    },
    {
      condition_id: "0x" + "22".repeat(32),
      question: "Will the measure pass?",
      _event_title: "Policy vote",
      _event_slug: "policy-vote",
      groupItemTitle: "Pass",
      outcomePrices: [".40", ".60"],
      volume24hr: 1000,
      volume: 10000,
      endDate: "2099-12-31T00:00:00Z",
      tokens: [
        { token_id: "33", outcome: "Yes" },
        { token_id: "44", outcome: "No" },
      ],
    },
  ];
  markets.push({
    ...markets[0],
    condition_id: "0x" + "33".repeat(32),
    question: "Will Bitcoin reach the second target price?",
    groupItemTitle: "$120,000",
    tokens: [
      { token_id: "55", outcome: "Yes" },
      { token_id: "66", outcome: "No" },
    ],
  });
  await page.route("**/api/markets/search?*", (route) => {
    const query = new URL(route.request().url()).searchParams;
    const search = (query.get("q") ?? "").toLowerCase();
    let selected = markets.filter((market) =>
      (market.question ?? "").toLowerCase().includes(search),
    );
    if (query.get("tag") === "politics")
      selected = selected.filter((market) => market._event_slug === "policy-vote");
    const offset = Number(query.get("offset") ?? 0);
    return route.fulfill({
      json: { markets: selected.slice(offset), total: selected.length, offset, has_more: false },
    });
  });
}
