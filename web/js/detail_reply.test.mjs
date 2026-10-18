// Plain-node test for detail.js's bead reply section, with a fake DOM.
// Run: node web/js/detail_reply.test.mjs (server/tests/test_nav.py runs it under pytest).

import assert from "node:assert/strict";

class Node {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.className = ""; this.textContent = "";
    this.value = ""; this.listeners = {};
  }
  setAttribute(k, v) { this.attrs[k] = v; }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(e, f) { this.listeners[e] = f; }
  text() {
    return `${this.textContent} ${this.children.map((c) => (c.text ? c.text() : String(c.t))).join(" ")}`;
  }
  find(pred, out = []) {
    if (pred(this)) out.push(this);
    this.children.forEach((c) => c.find && c.find(pred, out));
    return out;
  }
}
globalThis.document = { createElement: (t) => new Node(t), createTextNode: (t) => ({ t, text: () => t }) };
globalThis.window = { addEventListener() {} };
globalThis.location = { hash: "", hostname: "board.example.net" };

let replyStatus = 200;
let replyBody = {};
globalThis.fetch = async (url, opts) => {
  if (String(url).endsWith("/comments")) {
    return { ok: true, status: 200, json: async () => commentsBody };
  }
  if (String(url).includes("/api/config/layout")) {
    return { ok: true, status: 200, json: async () => ({ human_labels: ["example-human"] }) };
  }
  return { ok: replyStatus < 300, status: replyStatus, json: async () => replyBody };
};
let commentsBody = {};

const { renderReplySection } = await import("./detail.js");
const settle = () => new Promise((r) => setTimeout(r, 0));

async function render(id, status) {
  commentsBody = {
    comments: [], status, labels: ["example-human"], human_labels_present: ["example-human"],
    route_preview: "needs-agent",
  };
  const data = {
    settings: { bead_reply_enabled: true },
    beads: { items: [{ id, status, updated_at: "2026-10-17T12:00:00Z", labels: ["example-human"] }] },
  };
  // First call starts the comments fetch; once it settles the cache is warm.
  renderReplySection(new Node("div"), id, data);
  await settle();
  const body = new Node("div");
  renderReplySection(body, id, data);
  return body;
}
const buttons = (body) => body.find((n) => n.tag === "button");
const label = (b) => b.text().trim();

let passed = 0;
async function test(name, fn) {
  try { await fn(); passed += 1; } catch (err) { console.error(`FAIL: ${name}`); throw err; }
}

await test("open human bead shows Send back and Close bead", async () => {
  const labels = buttons(await render("b-open", "open")).map(label);
  assert.deepEqual(labels, ["Send back", "Close bead"]);
});

await test("blocked human bead shows Close bead and no Send back", async () => {
  const body = await render("b-blocked", "blocked");
  assert.deepEqual(buttons(body).map(label), ["Close bead"]);
  assert.ok(!/Send back/.test(body.text()));
});

await test("in_progress human bead shows no action buttons", async () => {
  assert.deepEqual(buttons(await render("b-prog", "in_progress")), []);
});

await test("a 409 reply renders the server message in the popup", async () => {
  const body = await render("b-409", "blocked");
  replyStatus = 409;
  replyBody = { detail: { reason: "not_actionable", message: "bead is not actionable: test message" } };
  buttons(body)[0].listeners.click();
  await settle(); await settle();
  const again = new Node("div");
  renderReplySection(again, "b-409", {
    settings: { bead_reply_enabled: true },
    beads: { items: [{ id: "b-409", status: "blocked", updated_at: "2026-10-17T12:00:00Z" }] },
  });
  assert.match(again.text(), /bead is not actionable: test message/);
});

await test("a non-JSON failure renders HTTP <n>", async () => {
  const body = await render("b-500", "open");
  replyStatus = 500;
  replyBody = null;
  buttons(body)[1].listeners.click();
  await settle(); await settle();
  const again = new Node("div");
  renderReplySection(again, "b-500", {
    settings: { bead_reply_enabled: true },
    beads: { items: [{ id: "b-500", status: "open", updated_at: "2026-10-17T12:00:00Z" }] },
  });
  assert.match(again.text(), /HTTP 500/);
});

console.log(`detail_reply.test.mjs: ${passed} passed`);
