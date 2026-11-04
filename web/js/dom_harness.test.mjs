// Browser test for the DOM wiring that plain-node tests cannot reach
// (q733.8). Drives the real app in a headless Chromium against a running
// CritBoard server in fixture mode: index.html?fixture=1 reads
// web/fixtures/snapshot.json, but layout and theme still come from the
// server, so this also covers the fixture config 404 (q733.6).
//
// Usage: node web/js/dom_harness.test.mjs http://127.0.0.1:<port>
// server/tests/test_dom_harness.py starts an isolated server and runs this.
//
// Playwright is not a declared dependency of this repo. It is loaded with a
// CommonJS require, so a global install found through NODE_PATH works. The
// pytest wrapper skips when it cannot be resolved.

import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const base = process.argv[2];
if (!base) {
  console.error("usage: node dom_harness.test.mjs <base_url>");
  process.exit(2);
}

let passed = 0;

async function test(name, fn) {
  try {
    await fn();
    passed += 1;
  } catch (err) {
    console.error(`FAIL: ${name}`);
    throw err;
  }
}

// Exit 77 = skip: the pytest wrapper turns it into a skip. Needs Node >= 18
// (global fetch); the wrapper also skips below that.
if (Number(process.versions.node.split(".")[0]) < 18) {
  console.error("skip: Node >= 18 required");
  process.exit(77);
}

let browser;
try {
  browser = await chromium.launch();
} catch (err) {
  if (/Executable doesn't exist|playwright install/i.test(String(err))) {
    console.error(`skip: ${String(err).split("\n")[0]}`);
    process.exit(77);
  }
  throw err;
}
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const failed = [];
  page.on("response", (r) => {
    if (r.status() >= 400) failed.push(`${r.status()} ${new URL(r.url()).pathname}`);
  });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));

  const layout = await (await fetch(`${base}/api/config/layout`)).json();
  const expectedIds = layout.panels.filter((p) => !p.hidden).map((p) => p.id);

  await page.goto(`${base}/index.html?fixture=1`);

  await test("fixture mode loads layout and theme from the server without a 404", async () => {
    // Fixture mode opens no SSE stream, so the network goes idle after boot.
    await page.waitForLoadState("networkidle");
    const configFailures = failed.filter((f) => f.includes("/config/"));
    assert.deepEqual(configFailures, []);
    await page.waitForFunction(
      (n) => document.querySelectorAll("#grid .panel").length === n,
      expectedIds.length,
      { timeout: 10000 },
    );
  });

  await test("grid renders one panel per visible layout panel, in layout order", async () => {
    const ids = await page.$$eval("#grid .panel", (els) => els.map((e) => e.dataset.panelId));
    assert.deepEqual(ids, expectedIds);
    const title = await page.textContent("#app-title");
    assert.equal(title, layout.title || "CritBoard Mission Control");
  });

  await test("no uncaught page errors during boot", async () => {
    assert.deepEqual(pageErrors, []);
  });

  const shown = () =>
    page.evaluate(() => ({
      reload: document.getElementById("reload-banner").classList.contains("show"),
      update: document.getElementById("update-banner").classList.contains("show"),
    }));

  await test("reload banner shows on a version change", async () => {
    await page.evaluate(() => {
      window.__critdashSetVersion({ build: "base000", started_at: "2026-01-01T00:00:00Z" });
      window.__critdashSetVersion({ build: "next111", started_at: "2026-01-01T00:01:00Z" });
    });
    assert.deepEqual(await shown(), { reload: true, update: false });
  });

  await test("update banner hides the reload banner, never both at once", async () => {
    await page.evaluate(() =>
      window.__critdashSetUpdate({ update_available: true, latest: "abc1234", behind: 3 }),
    );
    assert.deepEqual(await shown(), { reload: false, update: true });
  });

  await test("a reload deferred behind the update banner shows once it clears", async () => {
    await page.evaluate(() => {
      window.__critdashSetVersion({ build: "next222", started_at: "2026-01-01T00:02:00Z" });
      window.__critdashSetUpdate({ update_available: false, latest: "abc1234", behind: 0 });
    });
    assert.deepEqual(await shown(), { reload: true, update: false });
  });
} finally {
  await browser.close();
}

console.log(`${passed} passed`);
