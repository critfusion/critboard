// Plain-node pin for the usage history widget's drawChart (web/js/widgets/usage_history.js).
// Run: node web/js/usage_history.test.mjs  (server/tests/test_usage_history_js.py runs it under pytest).
//
// uPlot draws to a canvas, so there is no SVG to compare. Instead this
// records everything drawChart produces: the legend, notice and chart DOM
// it builds, the options and data it hands to uPlot (axis value callbacks
// and the coverage-shading hook are called with fixed inputs), and what a
// legend click and a resize do. The record is compared with
// usage_history.golden.json, written from the code before drawChart was
// split into helpers. Regenerate only on purpose: PIN_REGENERATE=1 node web/js/usage_history.test.mjs

import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";

const GOLDEN = new URL("./usage_history.golden.json", import.meta.url);

// ---- minimal fake DOM: just what utils.el and the widget touch ----
class FakeClassList {
  constructor(node) {
    this.node = node;
  }
  _list() {
    return this.node.className ? this.node.className.split(" ").filter(Boolean) : [];
  }
  add(c) {
    const l = this._list();
    if (!l.includes(c)) this.node.className = [...l, c].join(" ");
  }
  toggle(c, on) {
    const l = this._list().filter((x) => x !== c);
    if (on) l.push(c);
    this.node.className = l.join(" ");
  }
}

class FakeNode {
  constructor(tag, text = null) {
    this.tag = tag;
    this.text = text;
    this.attrs = {};
    this.className = "";
    this.children = [];
    this.listeners = {};
    this.isConnected = true;
    this.classList = new FakeClassList(this);
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
  }
  appendChild(c) {
    this.children.push(c);
    return c;
  }
  set innerHTML(v) {
    assert.equal(v, "");
    this.children = [];
  }
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  dispatch(type, ev = {}) {
    for (const fn of this.listeners[type] || []) fn({ preventDefault() {}, ...ev });
  }
  getBoundingClientRect() {
    return { width: 640.7, height: 220.2 };
  }
}

function serialize(n) {
  if (n.tag === "#text") return n.text;
  const out = { tag: n.tag };
  if (n.className) out.class = n.className;
  if (Object.keys(n.attrs).length) out.attrs = n.attrs;
  if (n.value !== undefined) out.value = n.value;
  const ev = Object.keys(n.listeners);
  if (ev.length) out.on = ev.sort();
  if (n.children.length) out.children = n.children.map(serialize);
  return out;
}

const cssVars = { "--color-accent-cyan": "#00ffff", "--color-text-faint": "#111111" };
globalThis.document = {
  documentElement: {},
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (t) => new FakeNode("#text", String(t)),
};
globalThis.getComputedStyle = () => ({ getPropertyValue: (name) => cssVars[name] || "" });
const storage = {};
globalThis.window = {
  localStorage: {
    getItem: (k) => (k in storage ? storage[k] : null),
    setItem: (k, v) => {
      storage[k] = v;
    },
  },
};

let frames = [];
globalThis.requestAnimationFrame = (fn) => {
  frames.push(fn);
  return frames.length;
};
globalThis.cancelAnimationFrame = () => {};
let observers = [];
globalThis.ResizeObserver = class {
  constructor(cb) {
    this.cb = cb;
    this.observed = [];
    this.disconnected = false;
    observers.push(this);
  }
  observe(n) {
    this.observed.push(n);
  }
  disconnect() {
    this.disconnected = true;
  }
};

let charts = [];
class FakeUPlot {
  constructor(opts, data, host) {
    this.opts = opts;
    this.data = data;
    this.host = host;
    this.destroyed = false;
    this.sizes = [];
    charts.push(this);
  }
  destroy() {
    this.destroyed = true;
  }
  setSize(s) {
    this.sizes.push(s);
  }
}

function recordOpts(opts, data) {
  const calls = [];
  const ctx = {
    save: () => calls.push(["save"]),
    restore: () => calls.push(["restore"]),
    fillRect: (...a) => calls.push(["fillRect", ...a]),
    set fillStyle(v) {
      calls.push(["fillStyle", v]);
    },
  };
  const xs = data[0];
  const u = {
    scales: { x: { min: xs[0], max: xs[xs.length - 1] } },
    valToPos: (v) => Math.round((v - xs[0]) * 1000) / 1000,
    ctx,
    bbox: { top: 5, height: 100 },
  };
  for (const fn of opts.hooks?.drawClear || []) fn(u);
  const uNull = { ...u, scales: { x: { min: null, max: null } } };
  for (const fn of opts.hooks?.drawClear || []) fn(uNull);
  const json = JSON.parse(JSON.stringify(opts));
  json.axes = opts.axes.map((a, i) => ({
    ...JSON.parse(JSON.stringify(a)),
    values:
      typeof a.values === "function"
        ? a.values(u, i === 0 ? [0, 1, 2, 7] : [0, 0.5, 12.345, 1500, 2500000])
        : a.values === undefined
          ? "undefined"
          : a.values,
  }));
  json.hookCalls = calls;
  json.hookKeys = Object.keys(opts.hooks || {});
  return json;
}

// ---- load the widget with a fetch stub ----
let nextJson = null;
globalThis.fetch = async (url) => ({ ok: true, json: async () => JSON.parse(JSON.stringify(nextJson)), url });
const fetched = [];
const realFetch = globalThis.fetch;
globalThis.fetch = async (url, init) => {
  fetched.push([url, init]);
  return realFetch(url, init);
};

const { default: widget } = await import("./widgets/usage_history.js");

const flush = () => new Promise((r) => setTimeout(r, 0));

function snap(container) {
  return {
    dom: serialize(container),
    charts: charts.map((c) => ({
      destroyed: c.destroyed,
      sizes: c.sizes,
      data: c.data,
      opts: recordOpts(c.opts, c.data),
      hostIsChartHost: c.host === findChartHost(container),
    })),
    observers: observers.map((o) => ({ disconnected: o.disconnected, observesContainer: o.observed[0] === container })),
    roFrame: container.__roFrame ?? null,
  };
}

function findChartHost(container) {
  const wrap = container.children[0];
  return wrap ? wrap.children[3] : null;
}

// Legend chips that toggle a series (excluded chips have no handler).
function legendChips(container) {
  return container.children[0].children[1].children.filter((n) => n.attrs.role === "button");
}

async function run(name, json, { saved = null, breakpoint = "desktop", withUPlot = true } = {}) {
  charts = [];
  observers = [];
  frames = [];
  for (const k of Object.keys(storage)) delete storage[k];
  if (saved) storage[`critdash.usage_history.${name}`] = JSON.stringify(saved);
  window.uPlot = withUPlot ? FakeUPlot : undefined;
  nextJson = json;
  const container = new FakeNode("div");
  widget.render(container, { breakpoint, panel: { id: name } });
  await flush();
  return container;
}

const H = (h) => `2026-09-18T${String(h).padStart(2, "0")}:00:00Z`;
const BASE = {
  buckets: [H(8), H(9), H(10), H(11)],
  series: [
    { key: "claude-opus-5", provider: "anthropic", tokens: [100, 2000, 0, 50], cost_usd: [0.5, 1.25, null, 0.01] },
    { key: "claude-haiku-5", provider: "anthropic", tokens: [5000, 6000, 7000, 8000], cost_usd: [0.1, 0.1, 0.1, 0.1] },
    { key: "kimi-k2", provider: "moonshot", tokens: [10, 20, 30, 40], cost_usd: [null, null, null, null] },
    { key: "gpt-x", tokens: [1, 1, 1, 1], cost_usd: [2, 0, 0, 0] },
  ],
  coverage: { fleet_complete_from: H(10) },
};

const record = {};
let c;

// 1. spend, desktop, coverage shading, cost-unknown series excluded
c = await run("spend", BASE);
record.spend = snap(c);
// click a legend chip: hide it and redraw
legendChips(c)[1].dispatch("click");
record.spend_after_click = snap(c);
// keyboard toggle back on
legendChips(c)[1].dispatch("keydown", { key: "Enter" });
record.spend_after_key = snap(c);
legendChips(c)[1].dispatch("keydown", { key: " " });
legendChips(c)[0].dispatch("keydown", { key: "x" });
record.spend_after_other_key = snap(c);
// resize: under threshold, then over it
observers.at(-1).cb();
record.frames_queued = frames.length;
frames.shift()();
observers.at(-1).cb();
findChartHost(c).getBoundingClientRect = () => ({ width: 900, height: 300 });
frames.shift()();
record.after_resize = snap(c);

// 2. tokens metric, mobile, group by host, no coverage
c = await run("tokens", { ...BASE, coverage: {} }, { saved: { window: "7d", groupBy: "host", metric: "tokens" }, breakpoint: "mobile" });
record.tokens = snap(c);

// 3. non-time buckets, tablet, group by provider
c = await run("labels", { ...BASE, buckets: ["a", "b", "c", "d"] }, { saved: { groupBy: "provider", metric: "tokens" }, breakpoint: "tablet" });
record.labels = snap(c);

// 4. empty data
c = await run("empty", { buckets: [], series: [], coverage: {} });
record.empty = snap(c);

// 5. spend with only cost-unknown series
c = await run("unknown_only", { ...BASE, series: [BASE.series[2]] });
record.unknown_only = snap(c);

// 6. every series hidden by clicks
c = await run("all_hidden", { ...BASE, series: [BASE.series[0]] }, { saved: { metric: "tokens" } });
legendChips(c)[0].dispatch("click");
record.all_hidden = snap(c);

// 7. coverage start before the first bucket: no notice
c = await run("early_coverage", { ...BASE, coverage: { fleet_complete_from: H(1) } });
record.early_coverage = snap(c);

record.fetched = fetched.map(([url, init]) => [url, init]);

const got = JSON.parse(JSON.stringify(record));
if (process.env.PIN_REGENERATE === "1") {
  writeFileSync(GOLDEN, JSON.stringify(got, null, 1) + "\n");
}
const want = JSON.parse(readFileSync(GOLDEN, "utf8"));
let passed = 0;
for (const key of Object.keys(want)) {
  assert.deepStrictEqual(got[key], want[key], key);
  passed += 1;
}
assert.deepStrictEqual(Object.keys(got).sort(), Object.keys(want).sort());
console.log(`${passed} passed`);
