// An incompatible glob/minimatch upgrade can silently generate an empty SW.
import { readFileSync, readdirSync } from "node:fs";
import assert from "node:assert/strict";
const assets = readdirSync("dist/assets").filter((name) => /^index-.*\.(js|css)$/.test(name));
const worker = readFileSync("dist/sw.js", "utf8");
assert(assets.length >= 2, "Expected built application JS and CSS");
for (const name of assets)
  assert(worker.includes(`assets/${name}`), `Service worker omitted ${name}`);
