// Plain-node test for nav.js and knowledge_text.js (pure logic, no DOM).
// Run: node web/js/nav.test.mjs  (server/tests/test_nav.py runs it under pytest).

import assert from "node:assert/strict";
import {
  availableViews, neighbourView, resolveView, searchWithView, showNav, viewFromSearch,
} from "./nav.js";
import { describeProblem } from "./knowledge_text.js";

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

// ---- default install: no modules -> no nav ----
test("no modules enabled -> only Live, no nav bar", () => {
  const v = availableViews([]);
  assert.deepEqual(v, ["live"]);
  assert.equal(showNav(v), false);
});

test("junk from /api/modules is treated as no modules", () => {
  for (const junk of [undefined, null, "knowledge", 5, {}, [null], ["bogus"], ["live"]]) {
    assert.deepEqual(availableViews(junk), ["live"], String(junk));
  }
});

// ---- enabled modules ----
test("one module enabled -> Live plus that view, nav shown", () => {
  const v = availableViews(["knowledge"]);
  assert.deepEqual(v, ["live", "knowledge"]);
  assert.equal(showNav(v), true);
});

test("views come out in fixed nav order whatever order the server sends", () => {
  assert.deepEqual(
    availableViews(["knowledge", "workflow", "infrastructure"]),
    ["live", "infrastructure", "workflow", "knowledge"],
  );
});

// ---- view selection ----
test("a requested view that is not enabled falls back to Live", () => {
  const v = availableViews([]);
  assert.equal(resolveView("knowledge", v), "live");
  assert.equal(resolveView("nonsense", availableViews(["knowledge"])), "live");
  assert.equal(resolveView("knowledge", availableViews(["knowledge"])), "knowledge");
});

test("view comes from the ?view= query parameter, Live by default", () => {
  assert.equal(viewFromSearch(""), "live");
  assert.equal(viewFromSearch("?view=knowledge"), "knowledge");
  assert.equal(viewFromSearch("?fixture=1&view=workflow"), "workflow");
});

test("switching view keeps other query parameters and never uses the hash", () => {
  assert.equal(searchWithView("?fixture=1", "knowledge"), "?fixture=1&view=knowledge");
  assert.equal(searchWithView("?fixture=1&view=knowledge", "live"), "?fixture=1");
  assert.equal(searchWithView("", "live"), "");
  assert.equal(searchWithView("?view=knowledge", "workflow"), "?view=workflow");
});

test("the TODAY/7D/30D window is not a view parameter", () => {
  // Navigation must not read or write the metrics filter.
  assert.equal(searchWithView("?window=7d", "knowledge"), "?window=7d&view=knowledge");
  assert.equal(viewFromSearch("?window=7d"), "live");
});

// ---- keyboard ----
test("arrow keys move between views and wrap; Home/End jump; other keys do nothing", () => {
  const v = ["live", "infrastructure", "knowledge"];
  assert.equal(neighbourView(v, "live", "ArrowRight"), "infrastructure");
  assert.equal(neighbourView(v, "knowledge", "ArrowRight"), "live");
  assert.equal(neighbourView(v, "live", "ArrowLeft"), "knowledge");
  assert.equal(neighbourView(v, "infrastructure", "Home"), "live");
  assert.equal(neighbourView(v, "infrastructure", "End"), "knowledge");
  assert.equal(neighbourView(v, "live", "Tab"), null);
  assert.equal(neighbourView(v, "unknown", "ArrowRight"), null);
});

// ---- Knowledge outage wording ----
const fmt = (iso) => `<${iso}>`;

test("unreachable states both times", () => {
  const msg = describeProblem(
    { code: "unreachable", message: "x" },
    { down_since: "T1", last_success: "T0" },
    fmt,
  );
  assert.equal(msg, "BookStack unreachable since <T1>; last success <T0>");
});

test("never reached -> last success is 'never'", () => {
  const msg = describeProblem({ code: "unreachable", message: "x" }, { down_since: "T1", last_success: null }, fmt);
  assert.equal(msg, "BookStack unreachable since <T1>; last success never");
});

test("auth failure names the HTTP status", () => {
  const msg = describeProblem(
    { code: "auth_failed", http_status: 401, message: "x" },
    { down_since: "T1", last_success: "T0" },
    fmt,
  );
  assert.match(msg, /^BookStack rejected the configured token \(HTTP 401\) since <T1>/);
});

test("setup problems are not described as outages; missing page is not a problem line", () => {
  const msg = describeProblem({ code: "token_file_permissions", message: "chmod 600" }, { down_since: null }, fmt);
  assert.equal(msg, "Knowledge module setup problem: chmod 600");
  assert.equal(describeProblem({ code: "not_found" }, null, fmt), null);
  assert.equal(describeProblem(null, null, fmt), null);
});

console.log(`nav.test.mjs: ${passed} passed`);
