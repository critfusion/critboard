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
import {
  availabilityBadge, availabilityKey, costText, effectiveRow, isExpired, gateKey, gateLabel, gateMissingText, policyStateText, valuesText,
} from "./workflow_text.js";

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

// ---- Workflow view wording ----
test("an unknown gate value is shown as missing, never fine", () => {
  for (const g of [undefined, null, "", "ok", "passed", "complete", "wat"]) {
    assert.equal(gateKey(g), "missing", String(g));
    assert.equal(gateLabel(g), "REVIEW GATE MISSING");
  }
  assert.equal(gateLabel("named"), "REVIEWER AND APPROVER NAMED");
  assert.equal(gateLabel("no_review_marked"), "NO REVIEW MARKED");
});

test("missing gate parts are named in words", () => {
  assert.equal(gateMissingText(["reviewer", "approver"]), "no reviewer named; no final approver named");
  assert.equal(gateMissingText(["independent_reviewer"]), "reviewer is the coder");
  assert.equal(gateMissingText(undefined), "");
});

test("an expired or unknown reading never reads as observed", () => {
  assert.equal(availabilityBadge({ expired: true, state: "observed" }), "STALE");
  assert.equal(availabilityBadge({ state: "stale" }), "STALE");
  assert.equal(availabilityBadge({ state: "unknown", basis: "unknown" }), "UNKNOWN");
  assert.equal(availabilityBadge({ state: "observed", basis: "provider-observed", expired: false }), "OBSERVED");
  assert.equal(availabilityBadge({ state: "available", basis: "owner-reported", expired: false }), "AVAILABLE");
  assert.equal(availabilityBadge(null), "STALE");
  assert.equal(availabilityKey({ expired: true }), "stale");
});

test("expiry is judged from expires_at at render time", () => {
  const row = { state: "observed", basis: "provider-observed", expired: false, expires_at: "2026-10-17T12:00:00Z",
    values: { remaining: 5 } };
  const t = Date.parse("2026-10-17T12:00:00Z");
  assert.equal(isExpired(row, t - 1), false);
  assert.equal(isExpired(row, t), true);
  assert.deepEqual(effectiveRow(row, t - 1), row);
  const gone = effectiveRow(row, t + 1000);
  assert.equal(gone.state, "stale");
  assert.equal(gone.expired, true);
  assert.equal(gone.values, null);
  assert.equal(availabilityBadge(gone), "STALE");
  assert.equal(row.values.remaining, 5); // the input row is not changed
});

test("an observed row with no readable expires_at is treated as expired", () => {
  for (const at of [undefined, null, "", "soon"]) {
    assert.equal(isExpired({ state: "observed", expires_at: at }, 0), true, String(at));
  }
  assert.equal(isExpired({ state: "unknown", expires_at: null }, 0), false);
  assert.equal(effectiveRow({ state: "unknown", values: null }, 0).state, "unknown");
});

test("the new gate parts are named in words", () => {
  assert.equal(gateMissingText(["independent_approver", "coder_unknown"]), "approver is the coder (self-approval); coder unknown");
});

test("reading text is only built from numbers", () => {
  assert.equal(valuesText({ remaining: 5, limit: 10, unit: "usd", period: "month" }), "5 of 10 usd remaining (month)");
  assert.equal(valuesText({ remaining: "5" }), null);
  assert.equal(valuesText(null), null);
});

test("a cost figure always says estimate and never spend", () => {
  assert.equal(costText({ cost_estimate_today_usd: 1.234 }), "estimate: $1.23 today (not spend)");
  assert.equal(costText({}), "no estimate");
  assert.doesNotMatch(costText({ cost_estimate_today_usd: 3 }), /remaining/);
});

test("policy problems are worded, ok has no problem line", () => {
  assert.equal(policyStateText({ state: "ok" }), null);
  assert.match(policyStateText({ state: "not_configured" }), /not configured/);
  assert.match(policyStateText({ state: "weird" }), /\(weird\)/);
});

console.log(`nav.test.mjs: ${passed} passed`);
