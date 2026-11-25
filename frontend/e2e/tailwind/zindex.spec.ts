import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

/**
 * Tailwind v4 regression: `resolveZ()` builds `z-<layer>` class names at
 * runtime, which the v4 scanner cannot see (JS `safelist` is ignored). These
 * tests pin every named layer against src/lib/zIndex.ts, on the real compiled
 * stylesheet, at desktop and mobile viewports.
 */

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = fs.readFileSync(path.resolve(HERE, "../../src/lib/zIndex.ts"), "utf8");
const BLOCK = /export const zIndex = \{([\s\S]*?)\} as const/.exec(SRC)?.[1] ?? "";
const LAYERS: Record<string, number> = Object.fromEntries(
  [...BLOCK.matchAll(/(\w+):\s*(\d+)/g)].map((m) => [m[1], Number(m[2])]),
);

test("zIndex.ts layers parsed", () => {
  expect(Object.keys(LAYERS).sort()).toEqual(
    ["drawer", "login", "modal", "modalNested", "nav", "navMenu", "toast", "tutorial"].sort(),
  );
});

test("every named z layer resolves to its zIndex.ts value in compiled CSS", async ({ page }) => {
  await page.goto("/guide");
  const result = await page.evaluate((layers) => {
    const out: Record<string, string> = {};
    for (const name of Object.keys(layers)) {
      const el = document.createElement("div");
      el.className = `fixed z-${name}`;
      document.body.appendChild(el);
      out[name] = getComputedStyle(el).zIndex;
      el.remove();
    }
    return out;
  }, LAYERS);
  for (const [name, value] of Object.entries(LAYERS)) {
    expect(result[name], `z-${name}`).toBe(String(value));
  }
});

test("overlapping fixed layers stack in zIndex.ts order (modal overlays, toast, drawer)", async ({
  page,
}) => {
  await page.goto("/guide");
  const ordered = Object.entries(LAYERS).sort((a, b) => a[1] - b[1]);
  const top = await page.evaluate((names) => {
    const els = names.map((n) => {
      const el = document.createElement("div");
      el.id = `probe-${n}`;
      el.className = `fixed inset-0 z-${n}`;
      document.body.appendChild(el);
      return el;
    });
    const hit = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
    const id = hit?.id;
    els.forEach((e) => e.remove());
    return id;
  }, ordered.map(([n]) => n));
  expect(top).toBe("probe-toast");

  // Modal sits under nested modal, tutorial, login, drawer and toast.
  for (const [lower, upper] of [
    ["modal", "modalNested"],
    ["nav", "modal"],
    ["navMenu", "modal"] as const,
  ].filter(([l, u]) => LAYERS[l] < LAYERS[u])) {
    const id = await page.evaluate(
      ([a, b]) => {
        const mk = (n: string) => {
          const el = document.createElement("div");
          el.id = `p-${n}`;
          el.className = `fixed inset-0 z-${n}`;
          document.body.appendChild(el);
          return el;
        };
        const e1 = mk(a);
        const e2 = mk(b);
        const hit = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2)?.id;
        e1.remove();
        e2.remove();
        return hit;
      },
      [lower, upper],
    );
    expect(id).toBe(`p-${upper}`);
  }
});

test("app navigation renders with Tailwind v4 utilities applied (no unstyled layout)", async ({
  page,
}) => {
  await page.goto("/guide");
  await expect(page.getByRole("heading", { name: "📖 Your Guide" })).toBeVisible();
  // `hidden`/`fixed`/`flex` utilities must exist in the compiled sheet.
  const probe = await page.evaluate(() => {
    const el = document.createElement("div");
    el.className = "hidden";
    document.body.appendChild(el);
    const hidden = getComputedStyle(el).display;
    el.className = "fixed";
    const fixed = getComputedStyle(el).position;
    el.className = "bg-primary-500";
    const primary = getComputedStyle(el).backgroundColor;
    el.remove();
    return { hidden, fixed, primary };
  });
  expect(probe.hidden).toBe("none");
  expect(probe.fixed).toBe("fixed");
  // Custom `primary` palette from tailwind.config.js (loaded through @config).
  expect(probe.primary).toMatch(/^(rgb\(14, 165, 233\)|color\(srgb 0\.0|oklab|oklch|lab)/);
});
