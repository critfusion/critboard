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
//     data-focus-key -> a widget's rebuild() should stamp any text-entry
//                 control it wants focus/caret preserved across a rebuild
//                 (e.g. the bead reply textarea) with a stable
//                 data-focus-key attribute. renderBody() uses it to restore
//                 focus/selection to the equivalent new control, since the
//                 old DOM node is gone after every rebuild.
//     onClose    -> optional callback invoked once, at the start of
//                 closeModal(), however the modal closes (X, backdrop,
//                 Escape, or a widget's own explicit closeModal() call).
//                 Used by settings.js to revert its live theme-preset
//                 preview when the dialog is closed without saving.
//   registerResolver(kind, (id) => {...}) lets a detail module open itself
//     when the page loads with a matching #kind/id hash, or on back/forward.
//
// refreshOpen(opts) -> two kinds of caller:
//   - PASSIVE (default, opts.force falsy): app.js's periodic calls after a
//     snapshot/SSE render pass. Deferred (remembered as pending) while the
//     user is engaged with the modal body (focus in a text control, or a
//     pointer button held down inside the modal) -- otherwise a rebuild
//     mid-keystroke drops focus/caret, and a rebuild between mousedown and
//     click on a button silently eats the click (the button element gets
//     swapped out before the click event is dispatched). Applied once
//     engagement ends.
//   - FORCED (opts.force: true): a modal's own content asking to re-render
//     right now because ITS data changed (a fetch completed, a reply was
//     sent) -- these must never be silently dropped.

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

// PASSIVE-refresh engagement tracking (see refreshOpen doc above).
let pendingPassiveRefresh = false;
let pointerDownInDialog = false;

function isTextEntryControl(node) {
  if (!node) return false;
  const tag = (node.tagName || "").toLowerCase();
  if (tag === "textarea") return true;
  if (tag === "input") {
    const type = (node.type || "text").toLowerCase();
    return ["text", "search", "email", "url", "tel", "password", "number"].includes(type);
  }
  return !!node.isContentEditable;
}

function isUserEngaged() {
  if (pointerDownInDialog) return true;
  const active = document.activeElement;
  return !!(active && bodyEl && bodyEl.contains(active) && isTextEntryControl(active));
}

function flushPendingRefresh() {
  if (!pendingPassiveRefresh) return;
  if (!state.open || state.noLiveRefresh) return;
  if (isUserEngaged()) return; // engagement moved to another in-modal control -- keep waiting
  pendingPassiveRefresh = false;
  renderBody();
}

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

  // PASSIVE-refresh engagement tracking. A pointer held down anywhere in
  // the dialog (e.g. mousedown on "Send back") counts as engaged even
  // though the mousedown default action blurs the previously-focused
  // textarea right away -- rebuilding at that point would swap the button
  // out from under the pointer before its click event is dispatched, so
  // the click would never fire. Defer past the click with setTimeout(0):
  // browsers dispatch click after pointerup/mouseup in the same task, so a
  // 0ms timeout runs after it.
  dialogEl.addEventListener("pointerdown", () => {
    pointerDownInDialog = true;
  });
  // pointercancel too: a touch that turns into a scroll never fires
  // pointerup, which would leave refreshes deferred until the next tap.
  const endPointer = () => {
    if (!pointerDownInDialog) return;
    pointerDownInDialog = false;
    setTimeout(flushPendingRefresh, 0);
  };
  document.addEventListener("pointerup", endPointer);
  document.addEventListener("pointercancel", endPointer);
  // Focus leaving a text control for something outside the modal body ends
  // engagement immediately (no need to wait for the next passive tick).
  // Moving to another control INSIDE the modal (relatedTarget inside
  // bodyEl) is not a disengagement -- e.g. Tab from the textarea to the
  // Send button -- so don't rebuild there; isUserEngaged() re-checks fresh
  // on the next refreshOpen()/flush anyway. A pointer-down-driven blur
  // (the button-click trap above) is handled by the pointerup flush, not
  // here.
  bodyEl.addEventListener("focusout", (e) => {
    if (!isTextEntryControl(e.target)) return;
    if (pointerDownInDialog) return;
    const related = e.relatedTarget;
    if (related && bodyEl.contains(related)) return;
    flushPendingRefresh();
  });
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
  pendingPassiveRefresh = false;
  renderBody({ preserve: false });

  overlayEl.setAttribute("data-open", "true");
  overlayEl.classList.add("open");
  pushHash(kind, id);
  requestAnimationFrame(() => {
    if (dialogEl) dialogEl.focus();
  });
}

// preserve: true (the default, used by every refresh) keeps the body's
// scroll position and, when a text control the widget marked with
// data-focus-key had focus, restores focus/selection to the equivalent new
// control afterwards (old DOM nodes are gone after the rebuild, so this
// can only match by that stable attribute, never by identity). preserve:
// false (openModal's initial render) always starts at scrollTop 0 with no
// focus restore -- a fresh open is not a "refresh" of what was showing.
function renderBody({ preserve = true } = {}) {
  if (!bodyEl || !state.rebuild) return;

  const scrollTop = preserve ? bodyEl.scrollTop : 0;
  let focusRestore = null;
  if (preserve) {
    const active = document.activeElement;
    if (active && bodyEl.contains(active) && isTextEntryControl(active)) {
      const key = active.getAttribute("data-focus-key");
      if (key) {
        focusRestore = {
          key,
          selectionStart: typeof active.selectionStart === "number" ? active.selectionStart : null,
          selectionEnd: typeof active.selectionEnd === "number" ? active.selectionEnd : null,
        };
      }
    }
  }

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

  bodyEl.scrollTop = scrollTop;

  if (focusRestore) {
    let target = null;
    try {
      target = bodyEl.querySelector(`[data-focus-key="${CSS.escape(focusRestore.key)}"]`);
    } catch (e) {
      target = null;
    }
    if (target && typeof target.focus === "function") {
      target.focus();
      if (focusRestore.selectionStart !== null && typeof target.setSelectionRange === "function") {
        try {
          target.setSelectionRange(focusRestore.selectionStart, focusRestore.selectionEnd);
        } catch (e) {
          // some input types (e.g. number/email) don't support setSelectionRange -- ignore
        }
      }
    }
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
  pendingPassiveRefresh = false;
  pointerDownInDialog = false;
  if (!opts.fromHashChange) clearHash();
  if (focusTarget && typeof focusTarget.focus === "function") {
    try {
      focusTarget.focus();
    } catch (e) {
      // trigger element may no longer be in the DOM (e.g. table re-sorted); ignore
    }
  }
}

// Called by app.js after every render pass (full snapshot or SSE patch) --
// PASSIVE, opts.force falsy -- and by a modal's own content when ITS data
// changed and must render now -- FORCED, opts.force: true (e.g. detail.js's
// fetchBeadDetail/fetchBeadComments completions, and the reply
// send/result/error state changes). See the module doc comment above for
// why passive refreshes are deferred while the user is engaged with the
// modal. No-op when nothing is open.
export function refreshOpen(opts = {}) {
  if (!state.open || state.noLiveRefresh) return;
  if (!opts.force && isUserEngaged()) {
    pendingPassiveRefresh = true;
    return;
  }
  pendingPassiveRefresh = false;
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
