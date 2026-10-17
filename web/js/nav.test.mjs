// Plain-node test for nav.js and knowledge_text.js (pure logic, no DOM).
// Run: node web/js/nav.test.mjs  (server/tests/test_nav.py runs it under pytest).

import assert from "node:assert/strict";
import {
  availableViews, neighbourView, resolveView, searchWithView, showNav, viewFromSearch,
} from "./nav.js";
import { describeProblem } from "./knowledge_text.js";
import {
  groupByKind, safeHref, statusCounts, statusKey, statusLabel, verifiedText,
} from "./infra_text.js";

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

test("host refusal tells the operator which name to add", () => {
  const msg = describeProblem({ code: "host_not_allowed", message: "x", http_status: 403 }, null, fmt, "board.example.net");
  assert.match(msg, /^CritBoard refused this address/);
  assert.match(msg, /add 'board\.example\.net' to modules\.knowledge\.allowed_hosts in config\/sources\.json$/);
  assert.doesNotMatch(msg, /BookStack/);
});

test("origin refusal says cross-origin, not a BookStack error", () => {
  const msg = describeProblem({ code: "origin_not_allowed", message: "x", http_status: 403 }, null, fmt, "h");
  assert.match(msg, /cross-origin request refused/);
  assert.doesNotMatch(msg, /BookStack/);
});

test("oversize page has its own wording", () => {
  const msg = describeProblem({ code: "upstream_too_large", message: "x", http_status: 200 }, { down_since: null, last_success: null }, fmt);
  assert.match(msg, /^BookStack page is larger than CritBoard's limit/);
});

// ---- Infrastructure view ----
test("Infrastructure appears in the nav only when the server lists it", () => {
  assert.deepEqual(availableViews([]), ["live"]);
  assert.equal(showNav(availableViews([])), false);
  assert.deepEqual(availableViews(["knowledge"]), ["live", "knowledge"]);
  const v = availableViews(["infrastructure"]);
  assert.deepEqual(v, ["live", "infrastructure"]);
  assert.equal(showNav(v), true);
  assert.equal(resolveView("infrastructure", availableViews([])), "live");
  assert.equal(resolveView("infrastructure", v), "infrastructure");
  assert.equal(searchWithView("", "infrastructure"), "?view=infrastructure");
});

test("Infrastructure sits before Knowledge in nav order", () => {
  assert.deepEqual(availableViews(["knowledge", "infrastructure"]), ["live", "infrastructure", "knowledge"]);
});

test("setup problem wording names the module label", () => {
  const msg = describeProblem({ code: "book_not_found", message: "no book" }, null, fmt, "", "Infrastructure");
  assert.equal(msg, "Infrastructure module setup problem: no book");
  assert.equal(describeProblem({ code: "not_configured", message: "m" }, null, fmt), "Knowledge module setup problem: m");
});

test("status labels are words; an unrecognised status reads UNKNOWN", () => {
  assert.equal(statusLabel("stale"), "STALE");
  assert.equal(statusLabel("needs-verification"), "NEEDS VERIFICATION");
  for (const junk of [undefined, null, "", "fresh", "__proto__", "toString"]) {
    assert.equal(statusKey(junk), "unknown", String(junk));
    assert.equal(statusLabel(junk), "UNKNOWN");
  }
});

test("only http(s) URLs become links", () => {
  assert.equal(safeHref("https://wiki.example.com/x"), "https://wiki.example.com/x");
  for (const bad of ["javascript:alert(1)", "data:text/html,x", "//evil.example.net", "/books/x", "ftp://a.example.net", "", null, undefined, "https://a b"]) {
    assert.equal(safeHref(bad), null, String(bad));
  }
});

test("records group by kind; unknown kind is its own last group, not dropped", () => {
  const g = groupByKind([
    { id: 1, kind: "service" }, { id: 2, kind: "unknown" }, { id: 3, kind: "host" },
    { id: 4 }, { id: 5, kind: "host" },
  ]);
  assert.deepEqual(g.map((x) => x.kind), ["host", "service", "unknown"]);
  assert.deepEqual(g[0].records.map((r) => r.id), [3, 5]);
  assert.deepEqual(g[2].records.map((r) => r.id), [2, 4]);
  assert.deepEqual(groupByKind(undefined), []);
});

test("status counts include every key and unknown for junk", () => {
  const c = statusCounts([{ status: "stale" }, { status: "stale" }, { status: "wat" }, {}]);
  assert.deepEqual(c, { current: 0, "needs-verification": 0, contradicted: 0, stale: 2, unknown: 2 });
});

test("an edit date is never worded as a verification", () => {
  const f = (iso) => `<${iso}>`;
  assert.equal(verifiedText({ verified_basis: "live_check", last_verified: "T1" }, f), "Live check <T1>");
  assert.match(verifiedText({ verified_basis: "page_updated", age_from: "T2", last_verified: "unknown" }, f), /^Never live-checked\. Page last edited <T2>/);
  assert.match(verifiedText({ verified_basis: "none" }, f), /^Never live-checked/);
  assert.match(verifiedText({ verified_basis: "live_check", last_verified: "unknown" }, f), /^Never live-checked/);
});

console.log(`nav.test.mjs: ${passed} passed`);
