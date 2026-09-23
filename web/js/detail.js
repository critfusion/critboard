// Detail-view content for the shared modal (modal.js). One place owns "what
// does a bead/agent/worktree/commit/activity item look like blown up to
// readable size" so bead_board.js and bead_table.js (etc.) don't each grow
// a bespoke popup. Not a widget itself -- a shared helper, like utils.js.

import { el, fmtAbsTime, fmtRelTime, fmtDuration, fmtPct, priorityColorVar, statusColorVar } from "./utils.js";
import { renderMarkdownLite } from "./markdown.js";
import * as modal from "./modal.js";

// ---------------- small render helpers ----------------

function field(label, valueNode) {
  if (valueNode === null || valueNode === undefined || valueNode === "") return null;
  if (typeof valueNode === "number") valueNode = String(valueNode);
  const row = el("div", { class: "detail-field" });
  row.appendChild(el("div", { class: "detail-field-label" }, label));
  row.appendChild(el("div", { class: "detail-field-value" }, valueNode));
  return row;
}

function fieldsGrid(rows) {
  const wrap = el("div", { class: "detail-fields-grid" });
  for (const r of rows) if (r) wrap.appendChild(r);
  return wrap;
}

function section(title, contentNode) {
  const s = el("div", { class: "detail-section" });
  s.appendChild(el("div", { class: "detail-section-title" }, title));
  s.appendChild(contentNode);
  return s;
}

function pill(text, colorVar) {
  return el("span", { class: "pill detail-pill", style: colorVar ? `color:var(${colorVar}); border-color:var(${colorVar});` : "" }, [
    el("span", { class: "pill-dot" }),
    text,
  ]);
}

function idLink(id, kind, labelText) {
  const a = el("a", { href: `#${kind}/${encodeURIComponent(id)}`, class: "detail-link mono" }, labelText || id);
  a.addEventListener("click", (e) => {
    e.preventDefault();
    if (kind === "bead") openBeadModal(id, window.__critdashData || null, null);
  });
  return a;
}

function idLinkList(ids, kind) {
  const wrap = el("div", { style: "display:flex; flex-wrap:wrap; gap:6px;" });
  for (const id of ids) wrap.appendChild(idLink(id, kind));
  return wrap;
}

function absRel(iso) {
  if (!iso) return null;
  return el("span", {}, [
    el("span", { class: "mono" }, fmtAbsTime(iso)),
    el("span", { class: "faint" }, ` (${fmtRelTime(iso)})`),
  ]);
}

function markdownBlock(text) {
  const holder = el("div", { class: "detail-markdown" });
  holder.innerHTML = renderMarkdownLite(text);
  return holder;
}

// ---------------- bead detail (priority target) ----------------

const beadDetailCache = new Map(); // id -> { status: 'loading'|'ok'|'error', body, error, forUpdatedAt }

function fetchBeadDetail(id, updatedAt) {
  const entry = beadDetailCache.get(id);
  if (entry && entry.status === "loading") return;
  // Once we have a result (success OR failure) for this bead's current
  // updated_at, don't refetch on every re-render -- refreshOpen() runs on
  // every SSE patch while the modal is open, and re-hitting a 404 endpoint
  // that often would hammer the backend and never let the network go idle.
  // Only a genuine change to updated_at invalidates the cache entry.
  if (entry && (entry.status === "ok" || entry.status === "error") && entry.forUpdatedAt === updatedAt) return;

  beadDetailCache.set(id, { status: "loading", body: null, error: null, forUpdatedAt: updatedAt });
  fetch(`/api/bead/${encodeURIComponent(id)}`, { cache: "no-store" })
    .then((res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    })
    .then((body) => {
      beadDetailCache.set(id, { status: "ok", body, error: null, forUpdatedAt: updatedAt });
      modal.refreshOpen({ force: true }); // FORCED: this fetch resolving is new data for THIS modal
    })
    .catch((err) => {
      beadDetailCache.set(id, {
        status: "error",
        body: null,
        error: String((err && err.message) || err),
        forUpdatedAt: updatedAt,
      });
      modal.refreshOpen({ force: true }); // FORCED -- see the .then() above
    });
}

const BEAD_STATUS_LABEL = { open: "OPEN", in_progress: "IN PROGRESS", blocked: "BLOCKED", closed: "CLOSED" };
const BEAD_STATUS_COLOR = {
  open: "--color-status-idle",
  in_progress: "--color-status-working",
  blocked: "--color-status-crit",
  closed: "--color-status-done",
};
const BEAD_LONG_TEXT_FIELDS = [
  ["description", "Description"],
  ["notes", "Notes"],
  ["design", "Design"],
  ["acceptance", "Acceptance"],
];

function renderBeadBody(bodyEl, id, data) {
  const bead = (data?.beads?.items || []).find((b) => b.id === id) || null;
  fetchBeadDetail(id, bead?.updated_at);
  const cacheEntry = beadDetailCache.get(id);
  const fetched = cacheEntry?.body || null;

  const title = fetched?.title || bead?.title || id;
  const status = fetched?.status ?? bead?.status;
  const priority = fetched?.priority ?? bead?.priority;
  const type = fetched?.type ?? bead?.type;
  const assignee = fetched?.assignee ?? bead?.assignee;
  const labels = fetched?.labels ?? bead?.labels ?? [];
  const createdAt = fetched?.created_at ?? bead?.created_at;
  const updatedAt = fetched?.updated_at ?? bead?.updated_at;
  const closedAt = fetched?.closed_at ?? bead?.closed_at;
  const ageS = bead?.age_s;
  const parent = fetched?.parent ?? bead?.parent;
  const blockedBy = fetched?.blocked_by ?? bead?.blocked_by ?? [];
  const blocks = fetched?.blocks ?? bead?.blocks ?? [];
  const repo = fetched?.repo ?? bead?.repo;
  const url = fetched?.url ?? bead?.url;

  const titleEl = document.getElementById("detail-modal-title");
  if (titleEl) titleEl.textContent = title;

  const head = el("div", { class: "detail-head" });
  head.appendChild(el("div", { class: "detail-title-full" }, title));
  const badgeRow = el("div", { style: "display:flex; flex-wrap:wrap; gap:8px; margin-top:8px;" });
  if (status) badgeRow.appendChild(pill(BEAD_STATUS_LABEL[status] || status, BEAD_STATUS_COLOR[status]));
  if (priority !== null && priority !== undefined) {
    badgeRow.appendChild(
      el("span", { class: "pill detail-pill", style: `color:var(${priorityColorVar(priority)}); border-color:var(${priorityColorVar(priority)});` }, [
        el("span", { class: "pill-dot" }),
        `P${priority}`,
      ])
    );
  }
  if (type) badgeRow.appendChild(pill(type));
  head.appendChild(badgeRow);
  bodyEl.appendChild(head);

  bodyEl.appendChild(
    fieldsGrid([
      field("ID", el("span", { class: "mono" }, id)),
      field("Assignee", assignee),
      field("Repo", repo),
      field("Labels", labels && labels.length ? el("div", { style: "display:flex; flex-wrap:wrap; gap:4px;" }, labels.map((l) => el("span", { class: "pill" }, l))) : null),
      field("Age", ageS !== null && ageS !== undefined ? fmtDuration(ageS) : null),
      field("Created", absRel(createdAt)),
      field("Updated", absRel(updatedAt)),
      field("Closed", closedAt ? absRel(closedAt) : null),
      field("Parent", parent ? idLink(parent, "bead") : null),
      field("Blocked by", blockedBy && blockedBy.length ? idLinkList(blockedBy, "bead") : null),
      field("Blocks", blocks && blocks.length ? idLinkList(blocks, "bead") : null),
      field("URL", url ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer", class: "detail-link" }, url) : null),
    ])
  );

  // long free-text fields, once the fetch resolves
  if (cacheEntry?.status === "loading") {
    bodyEl.appendChild(el("div", { class: "detail-loading" }, "Loading full description…"));
  } else if (cacheEntry?.status === "error") {
    bodyEl.appendChild(
      el("div", { class: "detail-note-warn" }, "Full description could not be loaded (backend detail endpoint unavailable). Showing dashboard snapshot data only.")
    );
  } else if (cacheEntry?.status === "ok") {
    let any = false;
    for (const [key, label] of BEAD_LONG_TEXT_FIELDS) {
      const text = fetched?.[key];
      if (text && String(text).trim()) {
        any = true;
        bodyEl.appendChild(section(label, markdownBlock(text)));
      }
    }
    if (!any) {
      bodyEl.appendChild(el("div", { class: "detail-note" }, "No description, notes, design, or acceptance text on this bead."));
    }
  }

  renderReplySection(bodyEl, id, data);
}

// ---------------- bead reply (send back / close, briefing feature) --------
// Off by default (data.settings.bead_reply_enabled, see main.py's
// _settings_block) -- only rendered at all when that flag is true AND the
// server says the bead currently carries at least one configured human
// label (GET /api/bead/{id}/comments' human_labels_present -- the server
// owns the label-matching/route logic so this module never duplicates it).

const beadCommentsCache = new Map(); // id -> { status, body, error, forUpdatedAt }
const beadReplyDraft = new Map(); // id -> in-progress reply text, survives modal rebuilds
const beadReplyState = new Map(); // id -> { sending, result: {message,next}|null, error }

let humanLabelsCache = null;
let humanLabelsPromise = null;

// Only needed for the post-success "Next" button (which open-labelled bead
// to jump to) -- config/layout.json's human_labels isn't part of the
// snapshot, so this is its own small, cached fetch (same pattern as
// fetchBeadDetail/fetchBeadComments below), not a duplication of anything
// already loaded elsewhere.
function ensureHumanLabels() {
  if (humanLabelsCache !== null) return Promise.resolve(humanLabelsCache);
  if (!humanLabelsPromise) {
    humanLabelsPromise = fetch("/api/config/layout", { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : {}))
      .then((doc) => {
        humanLabelsCache = Array.isArray(doc?.human_labels) ? doc.human_labels : [];
        return humanLabelsCache;
      })
      .catch(() => {
        humanLabelsCache = [];
        return humanLabelsCache;
      });
  }
  return humanLabelsPromise;
}

function fetchBeadComments(id, updatedAt) {
  const entry = beadCommentsCache.get(id);
  if (entry && entry.status === "loading") return;
  if (entry && (entry.status === "ok" || entry.status === "error") && entry.forUpdatedAt === updatedAt) return;

  beadCommentsCache.set(id, { status: "loading", body: null, error: null, forUpdatedAt: updatedAt });
  fetch(`/api/bead/${encodeURIComponent(id)}/comments`, { cache: "no-store" })
    .then(async (res) => {
      const payload = await res.json().catch(() => null);
      if (!res.ok) {
        const msg = payload?.detail?.message || payload?.detail || `HTTP ${res.status}`;
        throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
      }
      return payload;
    })
    .then((body) => {
      beadCommentsCache.set(id, { status: "ok", body, error: null, forUpdatedAt: updatedAt });
      modal.refreshOpen({ force: true }); // FORCED: this fetch resolving is new data for THIS modal
    })
    .catch((err) => {
      beadCommentsCache.set(id, {
        status: "error",
        body: null,
        error: String((err && err.message) || err),
        forUpdatedAt: updatedAt,
      });
      modal.refreshOpen({ force: true }); // FORCED -- see the .then() above
    });
}

// "Next" jumps to the next OPEN, human-labelled bead after this one, in the
// order the snapshot's beads.items array already lists them (top to bottom
// -- the owner works the queue one item at a time), wrapping around once.
function findNextHumanBead(data, currentId, humanLabels) {
  const items = data?.beads?.items || [];
  const isHumanOpen = (b) => b.status === "open" && (b.labels || []).some((l) => humanLabels.includes(l));
  const idx = items.findIndex((b) => b.id === currentId);
  const ordered = idx >= 0 ? items.slice(idx + 1).concat(items.slice(0, idx + 1)) : items;
  return ordered.find((b) => b.id !== currentId && isHumanOpen(b)) || null;
}

function commentNode(c) {
  const row = el("div", { class: "bead-reply-comment" });
  row.appendChild(
    el("div", { class: "bead-reply-comment-meta" }, [
      el("span", { class: "mono" }, c.author || "?"),
      el("span", { class: "faint" }, ` · ${fmtRelTime(c.created_at)}`),
    ])
  );
  // Plain text node (via el()'s string-child path, never innerHTML) -- a
  // comment body is arbitrary text, not markdown.
  row.appendChild(el("div", { class: "bead-reply-comment-body" }, c.text || ""));
  return row;
}

function renderReplySection(bodyEl, id, data) {
  if (!data?.settings?.bead_reply_enabled) return;

  const state = beadReplyState.get(id) || { sending: false, result: null, error: null };
  beadReplyState.set(id, state);

  // Once a send-back/close has succeeded, always show the confirmation --
  // even though the very write we just made may have removed the human
  // label the section below normally gates on (send_back's whole job is to
  // remove it), and a live SSE patch can re-render this modal seconds
  // later with that now-changed bead. Skip re-fetching comments too, since
  // the confirmation replaces that view entirely.
  if (state.result) {
    const wrap = el("div", { class: "bead-reply" });
    wrap.appendChild(el("div", { class: "bead-reply-result" }, state.result.message));
    if (state.result.next) {
      const nextBtn = el("button", { type: "button", class: "settings-btn-secondary" }, "Next");
      nextBtn.addEventListener("click", () => openBeadModal(state.result.next, window.__critdashData || data, null));
      wrap.appendChild(nextBtn);
    }
    bodyEl.appendChild(section("Reply", wrap));
    return;
  }

  const bead = (data?.beads?.items || []).find((b) => b.id === id) || null;
  fetchBeadComments(id, bead?.updated_at);
  const entry = beadCommentsCache.get(id);
  if (!entry) return;

  if (entry.status === "loading") {
    bodyEl.appendChild(section("Comments", el("div", { class: "detail-loading" }, "Loading comments…")));
    return;
  }
  if (entry.status === "error") {
    bodyEl.appendChild(
      section("Comments", el("div", { class: "detail-note-warn" }, `Could not load comments: ${entry.error}`))
    );
    return;
  }

  const info = entry.body || {};
  const humanPresent = info.human_labels_present || [];
  if (humanPresent.length === 0) return; // not human-owned -- no reply UI

  const wrap = el("div", { class: "bead-reply" });
  const comments = info.comments || [];
  wrap.appendChild(
    el(
      "div",
      { class: "bead-reply-comments" },
      comments.length ? comments.map(commentNode) : [el("div", { class: "detail-note" }, "No comments yet.")]
    )
  );

  const textarea = el("textarea", {
    class: "settings-input bead-reply-textarea",
    rows: "4",
    placeholder: "Write a reply…",
    // Stable key modal.js uses to reattach focus/caret to the equivalent
    // new textarea after a FORCED rebuild (e.g. the "sending" state change
    // just below) -- the old DOM node is gone every time renderBody() runs.
    "data-focus-key": "bead-reply-textarea",
  });
  textarea.value = beadReplyDraft.get(id) || "";
  textarea.addEventListener("input", () => beadReplyDraft.set(id, textarea.value));
  wrap.appendChild(textarea);

  wrap.appendChild(
    el("div", { class: "detail-note bead-reply-route" }, `Send back will route to: ${info.route_preview || "?"}`)
  );

  if (state.error) wrap.appendChild(el("div", { class: "detail-note-warn" }, state.error));

  const sendBtn = el(
    "button",
    { type: "button", class: "settings-btn-primary", disabled: state.sending ? "" : null },
    "Send back"
  );
  const closeBtn = el(
    "button",
    { type: "button", class: "settings-btn-secondary", disabled: state.sending ? "" : null },
    "Close bead"
  );
  wrap.appendChild(el("div", { class: "bead-reply-actions" }, [sendBtn, closeBtn]));

  function submit(action) {
    const s = beadReplyState.get(id) || {};
    s.sending = true;
    s.error = null;
    beadReplyState.set(id, s);
    // FORCED: the click that got us here already ended engagement with the
    // textarea, and this reply-flow state change (disabling the buttons)
    // must show up right away, not wait for the next passive tick.
    modal.refreshOpen({ force: true });

    fetch(`/api/bead/${encodeURIComponent(id)}/reply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, text: beadReplyDraft.get(id) || "" }),
    })
      .then(async (res) => {
        const payload = await res.json().catch(() => null);
        if (!res.ok) {
          const msg = payload?.detail?.message || payload?.detail || `HTTP ${res.status}`;
          throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
        }
        return payload;
      })
      .then((payload) =>
        ensureHumanLabels().then((humanLabels) => {
          beadReplyDraft.delete(id);
          let message;
          if (action === "send_back") {
            const removed = payload.removed_labels || [];
            message = `Sent back as ${payload.route} — removed label${removed.length === 1 ? "" : "s"} ${removed.join(", ") || "(none)"}.`;
          } else {
            message = `Closed${payload.comment_added ? " — your reply was added as a comment" : ""}.`;
          }
          const next = findNextHumanBead(window.__critdashData || data, id, humanLabels);
          beadReplyState.set(id, { sending: false, result: { message, next: next ? next.id : null }, error: null });
          modal.refreshOpen({ force: true }); // FORCED: send/close result must show immediately
        })
      )
      .catch((err) => {
        beadReplyState.set(id, { sending: false, result: null, error: String((err && err.message) || err) });
        modal.refreshOpen({ force: true }); // FORCED: error result must show immediately
      });
  }

  sendBtn.addEventListener("click", () => submit("send_back"));
  closeBtn.addEventListener("click", () => submit("close"));

  bodyEl.appendChild(section("Reply", wrap));
}

export function openBeadModal(idOrBead, data, triggerEl) {
  const id = typeof idOrBead === "string" ? idOrBead : idOrBead?.id;
  if (!id) return;
  const bead = typeof idOrBead === "string" ? (data?.beads?.items || []).find((b) => b.id === id) : idOrBead;
  modal.openModal({
    kind: "bead",
    id,
    title: bead?.title || id,
    triggerEl,
    rebuild: (el2, freshData) => renderBeadBody(el2, id, freshData),
  });
}

modal.registerResolver("bead", (id) => openBeadModal(id, window.__critdashData || null, null));

// ---------------- agent detail ----------------

const AGENT_KIND_LABEL = { claude: "Claude", codex: "Codex", grok: "Grok" };

function renderAgentBody(bodyEl, agentId, data) {
  const a = (data?.agents || []).find((x) => x.id === agentId);
  if (!a) {
    bodyEl.appendChild(el("div", { class: "detail-note-warn" }, "This agent is no longer reporting."));
    return;
  }
  const head = el("div", { class: "detail-head" });
  head.appendChild(el("div", { class: "detail-title-full" }, a.label || a.title || a.repo || a.id));
  const badgeRow = el("div", { style: "display:flex; flex-wrap:wrap; gap:8px; margin-top:8px;" });
  badgeRow.appendChild(pill((a.status || "unknown").toUpperCase(), statusColorVar(a.status)));
  badgeRow.appendChild(pill(AGENT_KIND_LABEL[a.kind] || a.kind || "?"));
  head.appendChild(badgeRow);
  bodyEl.appendChild(head);

  bodyEl.appendChild(
    fieldsGrid([
      field("Repo", a.repo ? `${a.repo}${a.branch ? " @ " + a.branch : ""}` : null),
      field("CWD", el("span", { class: "mono" }, a.cwd || "-")),
      field("Pane", a.pane ? `${a.pane} (workspace ${a.workspace || "-"})` : null),
      field("Session", el("span", { class: "mono" }, a.session_id || a.id)),
      field("Bead", a.bead ? idLink(a.bead, "bead") : null),
      field("Model", a.model),
      field("Last activity", absRel(a.last_activity)),
      field("Status since", absRel(a.status_since)),
      field("Tokens today", a.tokens_today ? `${(a.tokens_today.total ?? 0).toLocaleString("en-US")} total (in ${a.tokens_today.input ?? 0}, out ${a.tokens_today.output ?? 0}, cache read ${a.tokens_today.cache_read ?? 0}, cache write ${a.tokens_today.cache_write ?? 0})` : null),
      field("Cost today", a.cost_today_usd !== undefined && a.cost_today_usd !== null ? `$${Number(a.cost_today_usd).toFixed(2)}` : null),
      field("Messages today", a.msg_count_today),
      field("Active subagents", a.subagents_active),
    ])
  );
}

export function openAgentModal(agent, data, triggerEl) {
  const id = typeof agent === "string" ? agent : agent?.id;
  if (!id) return;
  const a = typeof agent === "string" ? (data?.agents || []).find((x) => x.id === id) : agent;
  modal.openModal({
    kind: "agent",
    id,
    title: a?.label || a?.title || a?.repo || id,
    triggerEl,
    rebuild: (el2, freshData) => renderAgentBody(el2, id, freshData),
  });
}

modal.registerResolver("agent", (id) => openAgentModal(id, window.__critdashData || null, null));

// ---------------- worktree / commit detail ----------------
// commit_feed rows are derived from worktrees[] (each worktree carries its
// last commit) -- there is no separate commits[] array in the snapshot, so
// both widgets open the same "worktree" detail, keyed by path.

function renderWorktreeBody(bodyEl, path, data) {
  const w = (data?.worktrees || []).find((x) => x.path === path);
  if (!w) {
    bodyEl.appendChild(el("div", { class: "detail-note-warn" }, "This worktree is no longer reporting."));
    return;
  }
  const dirtyTotal = (w.dirty ?? 0) + (w.untracked ?? 0) + (w.staged ?? 0);

  const head = el("div", { class: "detail-head" });
  head.appendChild(el("div", { class: "detail-title-full mono" }, w.repo || w.path));
  const badgeRow = el("div", { style: "display:flex; flex-wrap:wrap; gap:8px; margin-top:8px;" });
  if (w.branch) badgeRow.appendChild(pill(w.branch));
  badgeRow.appendChild(pill(dirtyTotal > 0 ? `${dirtyTotal} dirty` : "clean", dirtyTotal > 5 ? "--color-status-crit" : dirtyTotal > 0 ? "--color-status-warn" : "--color-text-faint"));
  head.appendChild(badgeRow);
  bodyEl.appendChild(head);

  bodyEl.appendChild(
    fieldsGrid([
      field("Path", el("span", { class: "mono" }, w.path)),
      field("Root", el("span", { class: "mono" }, w.root || "-")),
      field("Head", el("span", { class: "mono" }, w.head || "-")),
      field("Upstream", w.upstream),
      field("Ahead / behind", `${w.ahead ?? 0} / ${w.behind ?? 0}`),
      field("Dirty / untracked / staged", `${w.dirty ?? 0} / ${w.untracked ?? 0} / ${w.staged ?? 0}`),
      field("Stale", w.stale_days !== undefined ? `${w.stale_days}d since last commit` : null),
      field("Last commit", w.last_commit_msg),
      field("Last commit author", w.last_commit_author),
      field("Last commit at", absRel(w.last_commit_at)),
      field(
        "Agents here",
        w.agents && w.agents.length
          ? el(
              "div",
              { style: "display:flex; flex-wrap:wrap; gap:6px;" },
              w.agents.map((aid) => idLinkAgent(aid, data))
            )
          : null
      ),
    ])
  );
}

function idLinkAgent(agentId, data) {
  const a = (data?.agents || []).find((x) => x.id === agentId);
  const label = a?.label || a?.title || agentId;
  const link = el("a", { href: `#agent/${encodeURIComponent(agentId)}`, class: "detail-link mono" }, label);
  link.addEventListener("click", (e) => {
    e.preventDefault();
    openAgentModal(agentId, window.__critdashData || null, null);
  });
  return link;
}

export function openWorktreeModal(worktree, data, triggerEl) {
  const path = typeof worktree === "string" ? worktree : worktree?.path;
  if (!path) return;
  const w = typeof worktree === "string" ? (data?.worktrees || []).find((x) => x.path === path) : worktree;
  modal.openModal({
    kind: "worktree",
    id: path,
    title: w?.repo || path,
    triggerEl,
    rebuild: (el2, freshData) => renderWorktreeBody(el2, path, freshData),
  });
}

modal.registerResolver("worktree", (id) => openWorktreeModal(id, window.__critdashData || null, null));

// ---------------- activity detail ----------------
// events[] has no stable id field, so build a composite key from fields
// that are effectively unique per event (timestamp + kind + ref).

export function activityEventKey(ev) {
  return `${ev.t || ""}__${ev.kind || ""}__${ev.ref || ""}`;
}

function findEventByKey(data, key) {
  const events = data?.events || [];
  return events.find((e) => activityEventKey(e) === key) || null;
}

const EVENT_KIND_LABEL = {
  bead_created: "Bead created",
  bead_status: "Bead status change",
  bead_closed: "Bead closed",
  bead_claimed: "Bead claimed",
  agent_status: "Agent status change",
  commit: "Commit",
  dispatch: "Dispatch",
  alert: "Alert",
};

function renderActivityBody(bodyEl, key, data) {
  const ev = findEventByKey(data, key);
  if (!ev) {
    bodyEl.appendChild(el("div", { class: "detail-note-warn" }, "This event has scrolled out of the activity window."));
    return;
  }
  const head = el("div", { class: "detail-head" });
  head.appendChild(el("div", { class: "detail-title-full" }, EVENT_KIND_LABEL[ev.kind] || ev.kind || "Event"));
  bodyEl.appendChild(head);

  bodyEl.appendChild(
    fieldsGrid([
      field("Time", absRel(ev.t)),
      field("Severity", ev.severity ? ev.severity.toUpperCase() : null),
      field("Reference", ev.ref && ev.kind && ev.kind.startsWith("bead") ? idLink(ev.ref, "bead") : ev.ref || null),
    ])
  );

  if (ev.text) {
    bodyEl.appendChild(section("Details", el("div", { class: "detail-markdown detail-event-text" }, ev.text)));
  }
}

export function openActivityModal(ev, data, triggerEl) {
  const key = activityEventKey(ev);
  modal.openModal({
    kind: "event",
    id: key,
    title: EVENT_KIND_LABEL[ev.kind] || ev.kind || "Event",
    triggerEl,
    rebuild: (el2, freshData) => renderActivityBody(el2, key, freshData),
  });
}

modal.registerResolver("event", (key) => {
  const ev = findEventByKey(window.__critdashData || null, key);
  if (ev) openActivityModal(ev, window.__critdashData || null, null);
});

// ---------------- error detail (analytics.errors.top_errors) ----------------
// top_errors rows have no stable id -- tool+kind is unique per window on
// every sample observed on this host, so it serves as the composite key.

export function errorKey(e) {
  return `${e.tool || ""}::${e.kind || ""}`;
}

function findErrorByKey(data, key) {
  const items = data?.analytics?.errors?.top_errors || [];
  return items.find((e) => errorKey(e) === key) || null;
}

function renderErrorBody(bodyEl, key, data) {
  const e = findErrorByKey(data, key);
  if (!e) {
    bodyEl.appendChild(el("div", { class: "detail-note-warn" }, "This error entry has scrolled out of the current window."));
    return;
  }
  const head = el("div", { class: "detail-head" });
  head.appendChild(el("div", { class: "detail-title-full mono" }, e.kind));
  const badgeRow = el("div", { style: "display:flex; flex-wrap:wrap; gap:8px; margin-top:8px;" });
  if (e.tool) badgeRow.appendChild(pill(e.tool));
  badgeRow.appendChild(pill(`${e.count} occurrences`, "--color-status-crit"));
  head.appendChild(badgeRow);
  bodyEl.appendChild(head);

  bodyEl.appendChild(
    fieldsGrid([
      field("Tool", e.tool),
      field("Kind", e.kind),
      field("Count", e.count),
      field("Share of errors", e.pct !== undefined && e.pct !== null ? fmtPct(e.pct) : null),
      field("Last seen", absRel(e.last_seen)),
    ])
  );

  if (e.example) {
    bodyEl.appendChild(
      section("Example (redacted)", el("div", { class: "detail-event-text mono", style: "font-size:12px; color:var(--color-text-dim);" }, e.example))
    );
  }
}

export function openErrorModal(errorItem, data, triggerEl) {
  const key = errorKey(errorItem);
  modal.openModal({
    kind: "error",
    id: key,
    title: errorItem.kind,
    triggerEl,
    rebuild: (el2, freshData) => renderErrorBody(el2, key, freshData),
  });
}

modal.registerResolver("error", (key) => {
  const e = findErrorByKey(window.__critdashData || null, key);
  if (e) openErrorModal(e, window.__critdashData || null, null);
});
