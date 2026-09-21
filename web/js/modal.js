// Reusable detail-overlay component. Widgets call openModal() to pop a card
// or row open into a larger, readable view. One overlay/dialog is shared
// across the whole app (built lazily on first use) -- widgets never build
// their own popup markup.
//
// Contract a widget uses:
//   import { openModal, refreshOpen, registerResolver, isOpenFor } from "../modal.js";
//   openModal({ kind, id, title, rebuild, triggerEl })
//     kind/id   -> identify the item; reflected in the URL as #kind/id
//     title     -> dialog title (used for aria-labelledby)
//     rebuild(bodyEl, freshSnapshot) -> populate bodyEl. Called on open, and
//                 again on every subsequent refreshOpen() call (app.js calls
//                 this after every render pass) so content never goes stale
//                 while the modal is open. Must be safe to call repeatedly.
//     triggerEl -> element to return focus to on close (only captured the
//                 first time the modal opens for a given interaction).
//     noLiveRefresh -> opt-in; when true, app.js's periodic refreshOpen()
//                 calls (fired after every snapshot/SSE patch) are skipped
//                 while this modal is open. For a form the user is actively
//                 typing into (e.g. settings.js), the default behaviour --
//                 wipe bodyEl and rebuild from scratch on every tick -- would
//                 drop focus and in-progress edits every few seconds.
//     onClose    -> optional callback invoked once, at the start of
//                 closeModal(), however the modal closes (X, backdrop,
//                 Escape, or a widget's own explicit closeModal() call).
//                 Used by settings.js to revert its live theme-preset
//                 preview when the dialog is closed without saving.
//   registerResolver(kind, (id) => {...}) lets a detail module open itself
//     when the page loads with a matching #kind/id hash, or on back/forward.

import { el } from "./utils.js";

let overlayEl = null;
let dialogEl = null;
let titleEl = null;
let bodyEl = null;
let closeBtn = null;

const state = {
  open: false,
  kind: null,
  id: null,
  rebuild: null,
  lastFocused: null,
  noLiveRefresh: false,
  onClose: null,
};

const resolvers = {};

function ensureDom() {
  if (overlayEl) return;

  overlayEl = el("div", { class: "detail-modal-overlay", "data-open": "false" });
  dialogEl = el("div", {
    class: "detail-modal",
    role: "dialog",
    "aria-modal": "true",
    "aria-labelledby": "detail-modal-title",
    tabindex: "-1",
  });
  const header = el("div", { class: "detail-modal-header" });
  titleEl = el("h2", { id: "detail-modal-title", class: "detail-modal-title" }, "");
  closeBtn = el("button", { class: "detail-modal-close", type: "button", "aria-label": "Close" }, "×");
  header.appendChild(titleEl);
  header.appendChild(closeBtn);

  bodyEl = el("div", { class: "detail-modal-body" });

  dialogEl.appendChild(header);
  dialogEl.appendChild(bodyEl);
  overlayEl.appendChild(dialogEl);
  document.body.appendChild(overlayEl);

  closeBtn.addEventListener("click", () => closeModal());
  overlayEl.addEventListener("mousedown", (e) => {
    if (e.target === overlayEl) closeModal();
  });
  document.addEventListener("keydown", onKeydown, true);
  window.addEventListener("popstate", resolveFromHash);
}

function onKeydown(e) {
  if (!state.open) return;
  if (e.key === "Escape") {
    e.stopPropagation();
    closeModal();
    return;
  }
  if (e.key === "Tab") trapFocus(e);
}

function trapFocus(e) {
  const focusable = dialogEl.querySelectorAll(
    'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])'
  );
  if (focusable.length === 0) {
    e.preventDefault();
    return;
  }
  const list = Array.from(focusable);
  const first = list[0];
  const last = list[list.length - 1];
  if (e.shiftKey) {
    if (document.activeElement === first || !dialogEl.contains(document.activeElement)) {
      e.preventDefault();
      last.focus();
    }
  } else if (document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function pushHash(kind, id) {
  const h = `#${kind}/${encodeURIComponent(id)}`;
  if (location.hash !== h) {
    history.pushState(null, "", h);
  }
}

function clearHash() {
  if (location.hash) {
    history.pushState(null, "", location.pathname + location.search);
  }
}

export function openModal({ kind, id, title, rebuild, triggerEl, noLiveRefresh = false, onClose = null }) {
  ensureDom();
  const wasOpen = state.open;
  if (!wasOpen) {
    state.lastFocused = triggerEl || document.activeElement;
  }
  state.open = true;
  state.kind = kind;
  state.id = id;
  state.rebuild = rebuild;
  state.noLiveRefresh = noLiveRefresh;
  state.onClose = onClose;

  titleEl.textContent = title || String(id ?? "");
  renderBody();

  overlayEl.setAttribute("data-open", "true");
  overlayEl.classList.add("open");
  pushHash(kind, id);
  requestAnimationFrame(() => {
    if (dialogEl) dialogEl.focus();
  });
}

function renderBody() {
  if (!bodyEl || !state.rebuild) return;
  bodyEl.innerHTML = "";
  // Reset to the baseline class every render so a class a widget's rebuild()
  // added (e.g. settings.js's settings-modal-body, which changes this
  // element's padding) never leaks into a different kind of modal opened
  // afterwards -- same wipe-and-rebuild guarantee innerHTML already gets.
  bodyEl.className = "detail-modal-body";
  try {
    state.rebuild(bodyEl, window.__critdashData || null);
  } catch (err) {
    bodyEl.appendChild(
      el("div", { class: "detail-modal-error" }, `Error rendering detail: ${String((err && err.message) || err)}`)
    );
  }
}

export function closeModal(opts = {}) {
  if (!state.open) return;
  const onClose = state.onClose;
  if (typeof onClose === "function") {
    try {
      onClose();
    } catch (e) {
      console.error("[modal] onClose handler threw", e);
    }
  }
  overlayEl.classList.remove("open");
  overlayEl.setAttribute("data-open", "false");
  const focusTarget = state.lastFocused;
  state.open = false;
  state.kind = null;
  state.id = null;
  state.rebuild = null;
  state.lastFocused = null;
  state.noLiveRefresh = false;
  state.onClose = null;
  if (!opts.fromHashChange) clearHash();
  if (focusTarget && typeof focusTarget.focus === "function") {
    try {
      focusTarget.focus();
    } catch (e) {
      // trigger element may no longer be in the DOM (e.g. table re-sorted); ignore
    }
  }
}

// Called by app.js after every render pass (full snapshot or SSE patch) so
// an open modal's content re-reads the latest snapshot instead of going
// stale behind the user. No-op when nothing is open.
export function refreshOpen() {
  if (!state.open || state.noLiveRefresh) return;
  renderBody();
}

export function isOpenFor(kind, id) {
  return state.open && state.kind === kind && (id === undefined || state.id === id);
}

export function currentHash() {
  const h = location.hash.replace(/^#/, "");
  if (!h) return null;
  const idx = h.indexOf("/");
  if (idx === -1) return null;
  return { kind: h.slice(0, idx), id: decodeURIComponent(h.slice(idx + 1)) };
}

export function registerResolver(kind, fn) {
  resolvers[kind] = fn;
}

function resolveFromHash() {
  const h = currentHash();
  if (!h) {
    if (state.open) closeModal({ fromHashChange: true });
    return;
  }
  if (state.open && state.kind === h.kind && state.id === h.id) return;
  const fn = resolvers[h.kind];
  if (fn) fn(h.id);
}

// Call once, after the first snapshot has loaded, so a page opened (or
// reloaded) with a #kind/id hash in the URL restores that modal.
export function resolveInitialHash() {
  ensureDom();
  resolveFromHash();
}
