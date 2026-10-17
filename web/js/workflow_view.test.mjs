// Plain-node test for workflow_view.js with a fake DOM and a fake clock.
// Run: node web/js/workflow_view.test.mjs (server/tests/test_nav.py runs it under pytest).

import assert from "node:assert/strict";

class Node {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.className = ""; this.textContent = "";
    this.hidden = false; this.disabled = false; this.listeners = {};
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
}
globalThis.document = { createElement: (t) => new Node(t), createTextNode: (t) => ({ t, text: () => t }) };
globalThis.location = { hostname: "board.example.net" };

const EXPIRES = "2026-10-17T12:30:00Z";
let clock = Date.parse("2026-10-17T12:00:00Z");
Date.now = () => clock;
const timers = [];
globalThis.setInterval = (f, ms) => { timers.push({ f, ms }); return timers.length; };
const tick = () => timers.forEach((t) => t.f());

const body = {
  ok: true, fetched_at: "2026-10-17T12:00:00Z",
  policy: { state: "ok", pages: [] }, observed: { sessions: [], sources: {} }, assignments: { items: [] },
  availability: { ttl_s: 1800, rows: [{
    provider: "prov-a", basis: "provider-observed", state: "observed", expired: false, detail: null,
    values: { remaining: 5, limit: 10, unit: "usd", period: "month" },
    observed_at: "2026-10-17T12:00:00Z", expires_at: EXPIRES }] },
};
let failing = false;
let duringFetch = null; // runs while a request is in flight (the clock can move)
globalThis.fetch = async () => (duringFetch?.(), failing
  ? { status: 500, json: async () => ({ ok: false, error: { code: "upstream_error", message: "x" }, status: null }) }
  : { status: 200, json: async () => body });

const { mountWorkflow } = await import("./workflow_view.js");
const root = new Node("section");
const view = mountWorkflow(root);
const panels = root.children[1];
const numbers = () => /5 of 10/.test(panels.text());
const stale = () => /STALE/.test(panels.text());

let passed = 0;
async function test(name, fn) {
  try { await fn(); passed += 1; } catch (err) { console.error(`FAIL: ${name}`); throw err; }
}

await test("repeated onShow keeps exactly one interval, at 60 s", async () => {
  for (let i = 0; i < 5; i += 1) await view.onShow();
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, 60000);
});

await test("a fresh reading shows numbers, then goes stale after a tick past expires_at", async () => {
  assert.equal(numbers(), true);
  assert.equal(stale(), false);
  clock = Date.parse(EXPIRES) - 1000;
  tick();
  assert.equal(numbers(), true);
  clock = Date.parse(EXPIRES) + 1000;
  tick();
  assert.equal(numbers(), false);
  assert.equal(stale(), true);
});

await test("the timer does not render while the view is hidden", async () => {
  clock = Date.parse("2026-10-17T12:00:00Z");
  await view.onShow();
  assert.equal(numbers(), true);
  root.hidden = true;
  clock = Date.parse(EXPIRES) + 5000;
  tick();
  assert.equal(numbers(), true); // not re-rendered while hidden
});

await test("re-showing an expired copy never shows its numbers, even before the fetch ends", async () => {
  root.hidden = false;
  const pending = view.onShow(); // fetch not settled yet
  assert.equal(numbers(), false);
  assert.equal(stale(), true);
  await pending;
});

await test("a failed refresh keeps the dimmed copy with expired numbers hidden", async () => {
  clock = Date.parse("2026-10-17T12:00:00Z");
  failing = false;
  await view.onShow();
  assert.equal(numbers(), true);
  failing = true;
  clock = Date.parse(EXPIRES) - 1000; // fresh when the refresh starts
  duringFetch = () => { clock = Date.parse(EXPIRES) + 5000; }; // expires while the request is in flight
  await view.onShow();
  duringFetch = null;
  assert.equal(numbers(), false);
  assert.equal(stale(), true);
  assert.equal(panels.classList.set.has("infra-outdated"), true);
  tick();
  assert.equal(numbers(), false);
});

console.log(`workflow_view.test.mjs: ${passed} passed`);
