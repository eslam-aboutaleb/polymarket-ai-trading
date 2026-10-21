import { test, expect } from "../fixtures/base";

/** No routes or providers are mocked: browser -> real API -> gRPC -> paid provider. */
test("signed-in user receives successful quick analysis from the real provider", async ({
  userPage: page,
}) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Dashboard", exact: true })).toBeVisible();
  const response = await page.request.post("/api/analysis/quick", {
    data: {
      question:
        "Explain in two sentences what a binary prediction market price of 0.60 means. Do not recommend a trade.",
      current_price: 0.6,
    },
    timeout: 150_000,
  });
  expect(response.status()).toBe(200);
  const result = await response.json();
  expect(result.success).toBe(true);
  expect(result.backend).toBe("llm_chain");
  expect(result.data.analysis).toEqual(expect.any(String));
  expect(result.data.analysis.trim().length).toBeGreaterThan(40);
});

test("real streamed analysis reaches successful completion in the browser", async ({
  userPage: page,
}) => {
  const params = new URLSearchParams({
    market_title: "Understanding binary prediction market prices",
    market_description:
      "Explain a hypothetical binary contract and its uncertainty. Do not claim a real forecast or recommend trading.",
    yes_price: "0.6",
    no_price: "0.4",
    include_research: "false",
  });
  const streamResponse = page.waitForResponse((response) =>
    response.url().includes("/api/analysis/market/stream"),
  );
  await page.goto(`/analysis?${params}`);
  const stream = await streamResponse;
  expect(stream.status()).toBe(200);
  const body = await stream.text();
  const events = body
    .split("\n\n")
    .filter((line) => line.startsWith("data: "))
    .map((line) => JSON.parse(line.slice(6)));
  expect(events.some((event) => event.error)).toBe(false);
  expect(events.some((event) => event.done === true)).toBe(true);
  const generated = events
    .map((event) => (typeof event.chunk === "string" ? event.chunk : ""))
    .join("");
  // The server's progress text alone is far shorter than a completed response.
  expect(generated.trim().length).toBeGreaterThan(100);
  await expect(page.getByText("Analysis complete", { exact: true })).toBeVisible();
  await expect(page.getByText(/Analysis error:/)).toHaveCount(0);
});

test("research-backed analysis includes real search and news evidence", async ({
  userPage: page,
}) => {
  await page.goto("/");
  const response = await page.request.post("/api/analysis/market", {
    data: {
      market_title: "Federal Reserve interest rate decision",
      market_description:
        "Summarize current official policy information and uncertainty. This is a research integration check, not a trade recommendation.",
      yes_price: 0.5,
      no_price: 0.5,
      volume_24h: 0,
      end_date: new Date(Date.now() + 30 * 86400_000).toISOString(),
      include_research: true,
    },
    timeout: 150_000,
  });
  expect(response.status()).toBe(200);
  const result = await response.json();
  expect(result.success).toBe(true);
  expect(result.data.analysis.trim().length).toBeGreaterThan(100);
  expect(result.data.research_context).toContain("== Tavily Research ==");
  expect(result.data.research_context).toContain("Source: https://");
  expect(result.data.research_context).toContain("== Recent News Headlines ==");
});
