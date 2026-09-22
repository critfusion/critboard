// Plain-node test for banner_priority.js's pure decision logic (defect 3,
// macOS install report: #reload-banner and #update-banner showing at once).
// This repo has no frontend test harness (no package.json, no
// Playwright/Jest/Vitest) -- run directly with `node web/js/banner_priority.test.mjs`
// (see server/tests/test_banner_priority.py, which shells out to this).
//
// Deliberately NOT importing app.js itself: app.js touches `document`,
// `location`, `window` etc. at module load time and has no DOM available
// under plain node. banner_priority.js is kept dependency-free specifically
// so its logic is testable without a browser or a DOM shim.

import assert from "node:assert/strict";
import { isUpdateBannerActive } from "./banner_priority.js";

let passed = 0;

function test(name, fn) {
  try {
    fn();
    passed += 1;
  } catch (err) {
    console.error(`FAIL: ${name}`);
    throw err;
  }
}

test("no update object at all -> inactive", () => {
  assert.equal(isUpdateBannerActive(null, null), false);
  assert.equal(isUpdateBannerActive(undefined, null), false);
});

test("update_available false -> inactive even with a latest sha", () => {
  assert.equal(isUpdateBannerActive({ update_available: false, latest: "abc123" }, null), false);
});

test("update_available true, nothing dismissed -> active", () => {
  assert.equal(isUpdateBannerActive({ update_available: true, latest: "abc123" }, null), true);
});

test("update_available true, latest matches the dismissed key -> inactive", () => {
  assert.equal(isUpdateBannerActive({ update_available: true, latest: "abc123" }, "abc123"), false);
});

test("update_available true, latest differs from a stale dismissed key -> active again", () => {
  // A newer commit landed after the viewer dismissed an older one -- must
  // reappear (see app.js's update-banner section comment on this exact
  // behavior).
  assert.equal(isUpdateBannerActive({ update_available: true, latest: "def456" }, "abc123"), true);
});

test("update_available true with no latest sha at all -> still active", () => {
  // Matches the pre-refactor inline check in app.js: `key !== null && key
  // === dismissedUpdateKey` never suppresses a null key.
  assert.equal(isUpdateBannerActive({ update_available: true, latest: null }, null), true);
});

console.log(`banner_priority.test.mjs: ${passed} passed`);
