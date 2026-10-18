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
  scrollIntoView(opts) { (globalThis.scrolls ||= []).push({ node: this, opts }); }
  find(pred, out = []) {
    if (pred(this)) out.push(this);
    this.children.forEach((c) => c.find && c.find(pred, out));
    return out;
  }
}
globalThis.document = { createElement: (t) => new Node(t), createTextNode: (t) => ({ t, text: () => t }) };
globalThis.window = { addEventListener() {} };
globalThis.location = { hash: "", hostname: "board.example.net" };

const rafQueue = [];
globalThis.requestAnimationFrame = (f) => rafQueue.push(f);
const flushRaf = () => rafQueue.splice(0).forEach((f) => f());

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

const { renderReplySection, findNextHumanBead } = await import("./detail.js");
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
  const alertNode = again.find((n) => n.attrs.role === "alert")[0];
  // exact message: the JSON.stringify fallback would also contain the text
  assert.equal(alertNode.text().trim(), "bead is not actionable: test message");
  assert.ok(!alertNode.text().trim().startsWith("{"));
});

await test("a failed reply scrolls the error into view, once", async () => {
  flushRaf(); // frames left queued by earlier tests
  globalThis.scrolls = [];
  const body = await render("b-scroll", "open");
  replyStatus = 409;
  replyBody = { detail: { message: "nope" } };
  buttons(body)[1].listeners.click();
  await settle(); await settle();
  const data = {
    settings: { bead_reply_enabled: true },
    beads: { items: [{ id: "b-scroll", status: "open", updated_at: "2026-10-17T12:00:00Z" }] },
  };
  renderReplySection(new Node("div"), "b-scroll", data);
  // modal.js restores scrollTop right after the rebuild: nothing may scroll before that
  assert.equal(globalThis.scrolls.length, 0, "scroll must wait for the next frame");
  flushRaf();
  assert.equal(globalThis.scrolls.length, 1);
  assert.deepEqual(globalThis.scrolls[0].opts, { block: "nearest" });
  assert.equal(globalThis.scrolls[0].node.attrs.role, "alert");
  renderReplySection(new Node("div"), "b-scroll", data); // passive rebuild: no re-scroll
  flushRaf();
  assert.equal(globalThis.scrolls.length, 1);
});

for (const later of ["in_progress", "closed"]) {
  await test(`a 409 message survives the bead turning ${later}, with no buttons and no new scroll`, async () => {
    flushRaf();
    globalThis.scrolls = [];
    const id = `b-flip-${later}`;
    const body = await render(id, "open");
    replyStatus = 409;
    replyBody = { detail: { message: "refused: exact text" } };
    buttons(body)[1].listeners.click();
    await settle(); await settle();
    // the 409 is rendered and scrolled to once
    const mk = (status, updated_at) => ({
      settings: { bead_reply_enabled: true },
      beads: { items: [{ id, status, updated_at, labels: ["example-human"] }] },
    });
    renderReplySection(new Node("div"), id, mk("open", "2026-10-17T12:00:00Z"));
    flushRaf();
    assert.equal(globalThis.scrolls.length, 1);
    // snapshot and comments refresh: still human-labelled, no longer actionable
    commentsBody = { comments: [], status: later, labels: ["example-human"], human_labels_present: ["example-human"], route_preview: "x" };
    const flipped = mk(later, "2026-10-17T12:05:00Z");
    renderReplySection(new Node("div"), id, flipped); // starts the refetch
    await settle();
    const after = new Node("div");
    renderReplySection(after, id, flipped);
    flushRaf();
    const alerts = after.find((n) => n.attrs.role === "alert");
    assert.equal(alerts.length, 1);
    assert.equal(alerts[0].text().trim(), "refused: exact text");
    assert.deepEqual(buttons(after), []);
    assert.equal(globalThis.scrolls.length, 1, "no second scroll");
  });
}

await test("Next includes blocked human beads", async () => {
  const items = [
    { id: "a", status: "open", labels: ["example-human"] },
    { id: "b", status: "blocked", labels: ["example-human"] },
    { id: "c", status: "closed", labels: ["example-human"] },
  ];
  assert.equal(findNextHumanBead({ beads: { items } }, "a", ["example-human"]).id, "b");
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
