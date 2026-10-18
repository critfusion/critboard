// Plain-node test for knowledge_view.js, knowledge_text.js link and marker
// wording, and the Edit/History links in the infrastructure and workflow
// views, with a fake DOM.
// Run: node web/js/knowledge_view.test.mjs (server/tests/test_nav.py runs it under pytest).

import assert from "node:assert/strict";

class Node {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.className = ""; this.textContent = "";
    this.hidden = false; this.disabled = false; this.listeners = {}; this.value = "";
    const set = new Set();
    this.classList = { set, add: (c) => set.add(c), remove: (c) => set.delete(c) };
  }
  setAttribute(k, v) { this.attrs[k] = v; }
  appendChild(c) { this.children.push(c); return c; }
  append(...c) { this.children.push(...c); }
  replaceChildren(...c) { this.children = c; }
  addEventListener(e, f) { this.listeners[e] = f; }
  text() {
    return `${typeof this.textContent === "string" ? this.textContent : ""} ${this.children.map((c) => (c.text ? c.text() : String(c.t))).join(" ")}`;
  }
  find(pred, out = []) {
    if (pred(this)) out.push(this);
    this.children.forEach((c) => c.find && c.find(pred, out));
    return out;
  }
}
globalThis.document = { createElement: (t) => new Node(t), createTextNode: (t) => ({ t, text: () => t }) };
globalThis.location = { hostname: "board.example.net" };
const NOW = Date.parse("2026-10-17T12:30:00Z");
Date.now = () => NOW;

const { servedLine, wikiLinkItems } = await import("./knowledge_text.js");
const { mountKnowledge } = await import("./knowledge_view.js");
const { mountInfrastructure } = await import("./infrastructure_view.js");
const { mountWorkflow } = await import("./workflow_view.js");

let passed = 0;
async function test(name, fn) {
  try { await fn(); passed += 1; } catch (err) { console.error(`FAIL: ${name}`); throw err; }
}
const anchors = (n) => n.find((x) => x.tag === "a");
const hrefs = (n) => anchors(n).map((a) => a.attrs.href);

const EDIT = "https://wiki.example.com/books/b/page/p/edit";
const HIST = "https://wiki.example.com/books/b/page/p/revisions";
const relAgo = () => "5m ago";

// ---- pure wording ----
await test("pending says not yet served and always names the restart", () => {
  const t = servedLine({ state: "pending", served_at: null }, relAgo);
  assert.equal(t, "Pending: not yet served to a session start (since CritBoard restart)");
});

await test("served says served to a session start with the time and the restart note", () => {
  const t = servedLine({ state: "served", served_at: "2026-10-17T12:25:00Z", project: "proj-a" }, relAgo);
  assert.equal(t, "Served to a session start 5m ago (since CritBoard restart)");
});

await test("the marker never claims consumption", () => {
  for (const s of [{ state: "pending" }, { state: "served", served_at: "2026-10-17T12:25:00Z" }]) {
    assert.doesNotMatch(servedLine(s, relAgo), /consum|loaded|all sessions|read by/i);
  }
});

await test("a served state without a time falls back to pending, no marker gives no line", () => {
  assert.match(servedLine({ state: "served", served_at: null }, relAgo), /^Pending/);
  assert.equal(servedLine(undefined, relAgo), null);
});

await test("link items keep only http(s) URLs", () => {
  assert.deepEqual(wikiLinkItems({ edit_url: EDIT, history_url: HIST }).map((i) => i.text), ["Edit", "History"]);
  assert.deepEqual(wikiLinkItems({ edit_url: "javascript:alert(1)", history_url: null }), []);
  assert.deepEqual(wikiLinkItems({}), []);
  assert.equal(wikiLinkItems({ edit_url: EDIT }, "Page X")[0].label, "Edit Page X");
  assert.deepEqual(wikiLinkItems(null), []);
});

// ---- Knowledge view ----
const pending = { state: "pending", served_at: null, project: null };
const served = { state: "served", served_at: "2026-10-17T12:25:00Z", project: "proj-a" };
const responses = {
  "/api/knowledge/search": {
    ok: true, fetched_at: "2026-10-17T12:30:00Z", status: {}, total: 3,
    results: [
      { id: 1, name: "Page one", book: "Book", preview: "p1", edit_url: EDIT, history_url: HIST, served },
      { id: 2, name: "Page two", book: "Book", preview: "p2", edit_url: null, history_url: null, served: pending },
      { id: 3, name: "Page three", book: "Book", preview: "p3", edit_url: "javascript:alert(1)", history_url: null, served: pending },
    ],
  },
  "/api/knowledge/page/1": {
    ok: true, fetched_at: "2026-10-17T12:30:00Z", status: {}, id: 1, name: "Page one", book: { name: "Book" },
    text: "body", truncated: false, updated_at: "2026-10-17T12:00:00Z", provenance: { tags: [] },
    edit_url: EDIT, history_url: HIST, open_url: "https://wiki.example.com/link/1", served,
  },
  "/api/knowledge/page/2": {
    ok: true, fetched_at: "2026-10-17T12:30:00Z", status: {}, id: 2, name: "Page two", book: { name: "Book" },
    text: "body", truncated: false, updated_at: "2026-10-17T12:00:00Z", provenance: { tags: [] },
    edit_url: null, history_url: null, open_url: "https://wiki.example.com/link/2", served: pending,
  },
};
globalThis.fetch = async (url) => {
  const key = String(url).split("?")[0];
  return { status: 200, json: async () => responses[key] || { ok: false, error: { code: "not_found" }, status: {} } };
};
const settle = () => new Promise((r) => setTimeout(r, 0));

const root = new Node("section");
mountKnowledge(root);
const form = root.children[0];
form.listeners.submit({ preventDefault() {} });
await settle();
const results = root.find((n) => n.className === "kb-results")[0];
const detail = root.find((n) => n.className === "kb-detail")[0];
const rows = results.children;

await test("each search row shows the marker and the links the server validated", () => {
  assert.equal(rows.length, 3);
  assert.match(rows[0].text(), /Served to a session start 5m ago \(since CritBoard restart\)/);
  assert.match(rows[1].text(), /Pending: not yet served to a session start \(since CritBoard restart\)/);
  assert.deepEqual(hrefs(rows[0]), [EDIT, HIST]);
  assert.deepEqual(hrefs(rows[1]), []);
  assert.deepEqual(hrefs(rows[2]), []);
});

await test("links sit inside their card and name the page for screen readers", () => {
  assert.equal(rows[0].className, "kb-card");
  assert.deepEqual(anchors(rows[0]).map((a) => a.attrs["aria-label"]), ["Edit Page one", "History of Page one"]);
  assert.equal(rows[1].find((n) => n.className === "kb-result-links").length, 0);
});

await test("every link opens in a new tab with noopener noreferrer", () => {
  const all = [...anchors(rows[0]), ...anchors(detail)];
  assert.ok(all.length >= 2);
  for (const a of all) {
    assert.equal(a.attrs.target, "_blank");
    assert.equal(a.attrs.rel, "noopener noreferrer");
  }
});

await test("page detail shows Edit and History links and the marker", async () => {
  rows[0].children[0].listeners.click();
  await settle();
  const got = anchors(detail);
  assert.deepEqual(got.map((a) => a.attrs.href), [EDIT, HIST, "https://wiki.example.com/link/1"]);
  assert.deepEqual(got.map((a) => a.text().trim()), ["Edit in BookStack", "History in BookStack", "Open in BookStack"]);
  assert.match(detail.text(), /Served to a session start 5m ago \(since CritBoard restart\)/);
});

await test("a page with no valid slug shows no Edit or History link and says pending", async () => {
  rows[1].children[0].listeners.click();
  await settle();
  assert.deepEqual(hrefs(detail), ["https://wiki.example.com/link/2"]);
  assert.match(detail.text(), /Pending: not yet served/);
});

// ---- infrastructure and workflow views ----
const rec = (extra) => ({
  id: 5, title: "Svc A", url: "https://wiki.example.com/books/b/page/p", status: "current", status_reason: null,
  status_conflict: null, kind: "service", purpose: "x", owner: "o", environment: "e", runbooks: [], backups: [],
  related: [], source: { repo: "unknown", path: "unknown", git_sha: "unknown", class: "unknown" },
  updated_at: "2026-10-17T12:00:00Z", updated_by: "unknown", verified_basis: "none", last_verified: "unknown",
  ...extra,
});
const wfPolicy = (extra) => ({ id: 6, title: "Rule A", url: "https://wiki.example.com/books/b/page/p", status: "current",
  source: { repo: "unknown", path: "unknown", git_sha: "unknown" }, updated_at: "2026-10-17T12:00:00Z", ...extra });

await test("infrastructure cards link Edit and History; a record without them shows none", async () => {
  responses["/api/infrastructure"] = {
    ok: true, fetched_at: "2026-10-17T12:30:00Z", status: {}, total: 2, truncated: false, stale_after_days: 30,
    book: { name: "Infra", slug: "infra", url: "https://wiki.example.com/books/infra" },
    records: [rec({ edit_url: EDIT, history_url: HIST }), rec({ id: 6, title: "Svc B", edit_url: null, history_url: null })],
  };
  const r = new Node("section");
  const view = mountInfrastructure(r);
  await view.onShow();
  await settle();
  const cards = r.find((n) => n.tag === "article");
  assert.equal(cards.length, 2);
  const a = cards.find((c) => /Svc A/.test(c.text()));
  const b = cards.find((c) => /Svc B/.test(c.text()));
  assert.deepEqual(a.find((n) => n.className === "infra-wiki-links").flatMap(hrefs), [EDIT, HIST]);
  assert.deepEqual(anchors(a).filter((x) => x.attrs["aria-label"]).map((x) => x.attrs["aria-label"]), ["Edit Svc A", "History of Svc A"]);
  assert.equal(b.find((n) => n.className === "infra-wiki-links").length, 0);
});

await test("workflow policy cards link Edit and History", async () => {
  responses["/api/workflow"] = {
    ok: true, fetched_at: "2026-10-17T12:30:00Z", status: {},
    policy: { state: "ok", pages: [wfPolicy({ edit_url: EDIT, history_url: HIST })], total: 1, truncated: false },
    observed: { sessions: [], sources: {} }, assignments: { items: [] }, availability: { ttl_s: 1800, rows: [] },
  };
  const r = new Node("section");
  const view = mountWorkflow(r);
  await view.onShow();
  await settle();
  assert.deepEqual(r.find((n) => n.className === "infra-wiki-links").flatMap(hrefs), [EDIT, HIST]);
  assert.deepEqual(anchors(r).map((x) => x.attrs["aria-label"]).filter(Boolean), ["Edit Rule A", "History of Rule A"]);
});

console.log(`knowledge_view.test.mjs: ${passed} passed`);
process.exit(0);
