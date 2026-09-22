// Settings dialog: gear icon (top-right of the header, outside #grid) opens
// a panel for editing config/layout.json + config/theme.json fields that
// used to require hand-editing JSON. Reuses web/js/modal.js rather than
// building a second dialog -- see modal.js's contract comment for the two
// small additions made there (`noLiveRefresh`, `onClose`) to support a form
// the user is actively typing into.
//
// Contract this module works against (server/**, frozen, see AGENTS.md
// briefing / task spec): GET+POST /api/config/layout, GET+POST
// /api/config/theme, GET /api/settings/suggest, GET+POST
// /api/settings/updates, and a `settings` object on /api/snapshot. As of
// this writing GET layout/theme already work; POST layout validates but the
// write path is still landing; POST theme, GET suggest, and GET+POST
// settings/updates all 404; snapshot has no `settings` or `update` key yet.
// Every section below is written to degrade to a clear inline message
// instead of throwing when a call 404s -- do not assume any endpoint is
// live.
import { el, applyTheme, fmtRelTime } from "./utils.js";
import { openModal, closeModal } from "./modal.js";
import { PROVIDER_LABELS } from "./widgets/provider_quota.js";

const LAYOUT_URL = "/api/config/layout";
const THEME_URL = "/api/config/theme";
const SUGGEST_URL = "/api/settings/suggest";
const UPDATES_URL = "/api/settings/updates";

// Bumped on every openSettings() call. Async fetches started by an earlier
// open (e.g. the user closed the dialog before a GET resolved, then
// reopened it) check this before touching the DOM, so a stale response
// never overwrites a newer session's state.
let session = 0;

function deepClone(x) {
  return x === null || x === undefined ? x : JSON.parse(JSON.stringify(x));
}

async function fetchJSONSafe(url) {
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) return { ok: false, status: res.status };
    const data = await res.json();
    return { ok: true, data };
  } catch (e) {
    return { ok: false, status: 0, error: String((e && e.message) || e) };
  }
}

function extractErrorMessage(data, status) {
  if (status === 404) return "This save endpoint is not available yet (404 -- backend still landing it).";
  if (status === 403) return "Config writes are disabled on the server (403).";
  if (!data) return `Save failed (HTTP ${status}).`;
  const d = Object.prototype.hasOwnProperty.call(data, "detail") ? data.detail : data;
  if (typeof d === "string") return d;
  if (d && Array.isArray(d.errors)) return d.errors.join("; ");
  if (Array.isArray(d)) return d.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join("; ");
  if (d && typeof d.message === "string") return d.message;
  try {
    return JSON.stringify(d);
  } catch (e) {
    return `Save failed (HTTP ${status}).`;
  }
}

async function postJSONSafe(url, body) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      // no/invalid JSON body -- fine, message falls back to status text
    }
    if (res.ok) return { ok: true, status: res.status, data };
    return { ok: false, status: res.status, message: extractErrorMessage(data, res.status) };
  } catch (e) {
    return { ok: false, status: 0, message: `Network error: ${String((e && e.message) || e)}` };
  }
}

function isValidTimezone(tz) {
  if (!tz) return false;
  try {
    Intl.DateTimeFormat(undefined, { timeZone: tz });
    return true;
  } catch (e) {
    return false;
  }
}

// ---------------- gear button ----------------

const GEAR_SVG =
  '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<circle cx="12" cy="12" r="3"></circle>' +
  '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 ' +
  '1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 ' +
  '1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path>' +
  "</svg>";

// Called once from app.js's boot(), before config/snapshot loading -- see
// that call site's comment for why this has to happen first.
export function setupSettingsGear() {
  const header = document.getElementById("app-header");
  if (!header) return;
  let gear = document.getElementById("settings-gear");
  if (!gear) {
    gear = el("button", {
      id: "settings-gear",
      type: "button",
      class: "icon-btn",
      "aria-label": "Settings",
      "aria-haspopup": "dialog",
      html: GEAR_SVG,
    });
    header.appendChild(gear);
  }
  gear.addEventListener("click", () => openSettings(gear));
}

// ---------------- dialog state ----------------

function freshState() {
  return {
    mySession: 0,
    loaded: false,
    loadError: null,
    layout: null,
    layoutOriginal: null,
    theme: null,
    themeOriginal: null,
    suggest: null,
    suggestStatus: null, // "ok" | "unavailable"
    updatesInfo: null, // raw GET /api/settings/updates response (repo/current/latest/behind/checked_at/last_error + the 3 editable fields)
    updatesDraft: null, // { check_enabled, check_interval_s, auto_apply } -- the only shape POST accepts
    updatesDraftOriginal: null,
    updatesStatus: null, // "ok" | "unavailable"
    snapshotSettings: null,
    saving: false,
    saveErrors: [], // strings shown in the shared error box
    saveConfirmed: false,
    els: {}, // filled in by render, read back by handlers
  };
}

let S = freshState();

function isDirty() {
  if (!S.loaded) return false;
  return (
    JSON.stringify(S.layout) !== JSON.stringify(S.layoutOriginal) ||
    JSON.stringify(S.theme) !== JSON.stringify(S.themeOriginal) ||
    (S.updatesDraft !== null && JSON.stringify(S.updatesDraft) !== JSON.stringify(S.updatesDraftOriginal))
  );
}

function findQuotaPanel(layout) {
  return (layout.panels || []).find((p) => p.type === "provider_quota") || null;
}

// Fills in fields the dialog needs to edit but a stored layout.json may not
// have yet (human_labels, timezone, and the provider-quota panel's default
// hidden set) -- returns a fresh object, never mutates `raw`. Called once
// per load and cloned into both S.layout and S.layoutOriginal so these
// fill-ins never show up as a phantom unsaved change (see loadAll()).
function normalizeLayout(raw, suggest) {
  const layout = deepClone(raw) || {};
  if (!Array.isArray(layout.human_labels)) layout.human_labels = [];
  if (!layout.timezone) {
    layout.timezone = (suggest && suggest.timezone && suggest.timezone.detected) || "UTC";
  }
  const quotaPanel = findQuotaPanel(layout);
  if (quotaPanel) {
    quotaPanel.options = quotaPanel.options || {};
    if (suggest && suggest.quota_providers && !quotaPanel.options.hidden_providers) {
      quotaPanel.options.hidden_providers = [...(suggest.quota_providers.never_available || [])];
    }
  }
  return layout;
}

// ---------------- open / close ----------------

function openSettings(gearEl) {
  session += 1;
  S = freshState();
  S.mySession = session;

  openModal({
    kind: "settings",
    id: "panel",
    title: "Settings",
    triggerEl: gearEl,
    noLiveRefresh: true, // see modal.js: don't let background snapshot ticks wipe this form
    onClose: handleDialogClose,
    rebuild: (bodyEl) => renderShell(bodyEl),
  });

  loadAll(S.mySession);
}

function handleDialogClose() {
  // Cancel / Escape / backdrop / X all funnel through modal.js's
  // closeModal(), which calls this before tearing the dialog down. A
  // successful save updates S.themeOriginal to the just-saved theme before
  // closing itself, so re-applying "original" here is a no-op in that case
  // and a real revert of the live preview in every other case.
  if (S.themeOriginal) applyTheme(S.themeOriginal);
}

// ---------------- data loading ----------------

async function loadAll(mySession) {
  const [layoutRes, themeRes, suggestRes, updatesRes] = await Promise.all([
    fetchJSONSafe(LAYOUT_URL),
    fetchJSONSafe(THEME_URL),
    fetchJSONSafe(SUGGEST_URL),
    fetchJSONSafe(UPDATES_URL),
  ]);
  if (mySession !== session) return; // dialog closed/reopened while we were fetching

  if (!layoutRes.ok) {
    S.loadError = "Could not load the current layout config" + (layoutRes.status ? ` (HTTP ${layoutRes.status})` : "") + ".";
    renderNow();
    return;
  }

  if (themeRes.ok) {
    S.theme = deepClone(themeRes.data);
    S.themeOriginal = deepClone(themeRes.data);
  } else {
    // Theme is optional for the rest of the dialog to function -- app.js
    // itself falls back to CSS defaults when this 404s (see loadConfig()).
    S.theme = null;
    S.themeOriginal = null;
  }

  if (suggestRes.ok) {
    S.suggest = suggestRes.data;
    S.suggestStatus = "ok";
  } else {
    S.suggest = null;
    S.suggestStatus = "unavailable";
  }

  if (updatesRes.ok && updatesRes.data) {
    S.updatesInfo = updatesRes.data;
    S.updatesDraft = {
      check_enabled: !!updatesRes.data.check_enabled,
      check_interval_s: typeof updatesRes.data.check_interval_s === "number" ? updatesRes.data.check_interval_s : 300,
      auto_apply: !!updatesRes.data.auto_apply,
    };
    S.updatesDraftOriginal = deepClone(S.updatesDraft);
    S.updatesStatus = "ok";
  } else {
    S.updatesInfo = null;
    S.updatesDraft = null;
    S.updatesDraftOriginal = null;
    S.updatesStatus = "unavailable";
  }

  S.snapshotSettings = (window.__critdashData && window.__critdashData.settings) || null;

  // Build ONE normalized baseline from the raw GET response, then clone it
  // twice (into S.layout and S.layoutOriginal). Filling in missing-but-
  // required-for-editing fields (human_labels, timezone) and the
  // never_available-defaults-to-hidden quota default has to land on BOTH
  // copies identically -- otherwise the dialog opens already "dirty" with
  // no user edit at all, just because S.layout picked up a default that
  // S.layoutOriginal never saw. See normalizeLayout().
  const baseline = normalizeLayout(layoutRes.data, S.suggest);
  S.layout = deepClone(baseline);
  S.layoutOriginal = deepClone(baseline);

  S.loaded = true;
  renderNow();
}

// ---------------- rendering ----------------
// noLiveRefresh means modal.js will not call rebuild() again on its own, so
// every re-render after the initial synchronous one is triggered by us,
// directly, by calling renderNow() (which just re-invokes the same rebuild
// function modal.js would have). Safe to call repeatedly.

function renderNow() {
  const bodyEl = document.querySelector(".detail-modal-body");
  if (!bodyEl) return;
  bodyEl.innerHTML = "";
  renderShell(bodyEl);
}

function section(titleText, ...children) {
  return el("div", { class: "settings-section" }, [el("h3", { class: "settings-section-title" }, titleText), ...children]);
}

function hint(text) {
  return el("div", { class: "settings-hint" }, text);
}

function degraded(text) {
  return el("div", { class: "settings-degraded" }, text);
}

function fieldError(msg) {
  return msg ? el("div", { class: "settings-field-error" }, msg) : null;
}

function renderShell(bodyEl) {
  bodyEl.classList.add("settings-modal-body");
  if (S.loadError) {
    bodyEl.appendChild(
      el("div", { class: "settings-degraded settings-degraded-main" }, [
        el("div", {}, "Settings could not load."),
        el("div", { class: "settings-hint" }, S.loadError),
      ])
    );
    return;
  }
  if (!S.loaded) {
    bodyEl.appendChild(el("div", { class: "detail-loading" }, "Loading settings…"));
    return;
  }

  const form = el("div", { class: "settings-form" });
  form.appendChild(renderTitleSection());
  form.appendChild(renderHumanLabelsSection());
  form.appendChild(renderQuotaSection());
  form.appendChild(renderPanelVisibilitySection());
  form.appendChild(renderThemeSection());
  form.appendChild(renderTimezoneSection());
  const cadence = renderRefreshCadenceSection();
  if (cadence) form.appendChild(cadence);
  form.appendChild(renderUpdatesSection());
  form.appendChild(renderFooter());
  bodyEl.appendChild(form);
}

// ---- 1. title ----

function renderTitleSection() {
  const input = el("input", {
    type: "text",
    class: "settings-input",
    id: "settings-title-input",
    value: S.layout.title || "",
    "aria-label": "Dashboard title",
  });
  input.addEventListener("input", () => {
    S.layout.title = input.value;
    updateFooter();
  });
  return section("Title", el("div", { class: "settings-row" }, [input]));
}

// ---- 2. human labels ----

function renderHumanLabelsSection() {
  const listHost = el("div", { class: "settings-chip-list" });
  // normalizeLayout() already guarantees this is an array on both S.layout
  // and S.layoutOriginal -- no fallback-assignment here (that would only
  // touch S.layout and make the form spuriously dirty on open).
  const labels = S.layout.human_labels;

  function redrawChips() {
    listHost.innerHTML = "";
    if (labels.length === 0) {
      listHost.appendChild(el("span", { class: "settings-hint" }, "No human labels set -- every bead reads as fleet-routable."));
    }
    labels.forEach((label, i) => {
      const chip = el("span", { class: "settings-chip" }, [
        el("span", { class: "mono" }, label),
        el(
          "button",
          {
            type: "button",
            class: "settings-chip-remove",
            "aria-label": `Remove label ${label}`,
            onclick: () => {
              labels.splice(i, 1);
              redrawChips();
              updateFooter();
            },
          },
          "×"
        ),
      ]);
      listHost.appendChild(chip);
    });
  }
  redrawChips();

  const addInput = el("input", {
    type: "text",
    class: "settings-input settings-input-inline",
    placeholder: "add a label…",
    "aria-label": "Add human work label",
  });
  const addBtn = el("button", { type: "button", class: "settings-btn-secondary" }, "Add");
  function doAdd() {
    const v = addInput.value.trim();
    if (!v || labels.includes(v)) return;
    labels.push(v);
    addInput.value = "";
    redrawChips();
    updateFooter();
  }
  addBtn.addEventListener("click", doAdd);
  addInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      doAdd();
    }
  });

  const children = [
    hint('Labels that mean "a person owns this bead" -- the fleet dispatcher never claims or wakes an agent for one of these.'),
    listHost,
    el("div", { class: "settings-row" }, [addInput, addBtn]),
  ];

  if (S.suggestStatus === "unavailable") {
    children.push(degraded("Detected-label suggestion is unavailable (the /api/settings/suggest endpoint isn't live yet)."));
  } else if (S.suggest && S.suggest.human_labels) {
    const detected = S.suggest.human_labels.detected || [];
    const reason = S.suggest.human_labels.reason || "";
    const useBtn = el(
      "button",
      { type: "button", class: "settings-btn-secondary" },
      `Use detected: ${detected.length ? detected.join(", ") : "(none found)"}`
    );
    useBtn.disabled = detected.length === 0;
    useBtn.addEventListener("click", () => {
      // Never applied silently -- only on this explicit click, and even
      // then only staged into the draft; Save is still required.
      for (const d of detected) if (!labels.includes(d)) labels.push(d);
      redrawChips();
      updateFooter();
    });
    children.push(el("div", { class: "settings-suggest-row" }, [useBtn, reason ? el("span", { class: "settings-hint" }, reason) : null]));
  }

  return section("Human work labels", ...children);
}

// ---- 3. provider quota visibility ----

function renderQuotaSection() {
  const quotaPanel = findQuotaPanel(S.layout);
  if (!quotaPanel) {
    return section("Provider quota visibility", degraded("No provider-quota panel is configured on this dashboard -- nothing to show or hide."));
  }
  // normalizeLayout() already guarantees quotaPanel.options exists on both
  // S.layout and S.layoutOriginal -- read only, no fallback-assignment.
  const hidden = new Set(quotaPanel.options.hidden_providers || []);

  const neverAvailable = new Set((S.suggest && S.suggest.quota_providers && S.suggest.quota_providers.never_available) || []);
  const needsCredential = new Set((S.suggest && S.suggest.quota_providers && S.suggest.quota_providers.needs_credential) || []);

  const providerIds = new Set([
    ...Object.keys(PROVIDER_LABELS),
    ...neverAvailable,
    ...needsCredential,
    ...((S.suggest && S.suggest.quota_providers && S.suggest.quota_providers.working) || []),
  ]);

  const rows = [...providerIds].sort().map((id) => {
    const cb = el("input", { type: "checkbox", id: `settings-quota-${id}` });
    cb.checked = !hidden.has(id);
    cb.addEventListener("change", () => {
      if (cb.checked) hidden.delete(id);
      else hidden.add(id);
      quotaPanel.options.hidden_providers = [...hidden];
      updateFooter();
    });
    const badges = [];
    if (neverAvailable.has(id)) {
      badges.push(el("span", { class: "pill pill-idle settings-inline-pill" }, "no endpoint -- will never report"));
    } else if (needsCredential.has(id)) {
      badges.push(el("span", { class: "pill pill-info settings-inline-pill" }, "not yet configured"));
    }
    return el("label", { class: "settings-checkbox-row", for: `settings-quota-${id}` }, [cb, el("span", {}, PROVIDER_LABELS[id] || id), ...badges]);
  });

  const children = [
    hint(
      '"Hidden" means you turned the row off below. "Not yet configured" means the provider could work once a credential is set -- those stay visible by default so you know a credential would unlock them. Rows with no endpoint default to hidden since they can never report data.'
    ),
  ];
  if (S.suggestStatus === "unavailable") {
    children.push(degraded("Availability detection is unavailable (the /api/settings/suggest endpoint isn't live yet) -- showing every known provider without the hidden/not-configured distinction."));
  }
  children.push(el("div", { class: "settings-checkbox-list" }, rows));

  return section("Provider quota visibility", ...children);
}

// ---- 4. panel visibility ----

function renderPanelVisibilitySection() {
  const panels = S.layout.panels || [];
  if (panels.length === 0) {
    return section("Panel visibility", degraded("No panels configured."));
  }
  const rows = panels.map((panel) => {
    const cb = el("input", { type: "checkbox", id: `settings-panel-${panel.id}` });
    cb.checked = !panel.hidden;
    cb.addEventListener("change", () => {
      // Only the `hidden` flag is touched -- x/y/w/h are never rewritten,
      // so toggling a panel off then back on restores its original grid
      // position instead of appending it at the end.
      if (cb.checked) delete panel.hidden;
      else panel.hidden = true;
      updateFooter();
    });
    return el("label", { class: "settings-checkbox-row", for: `settings-panel-${panel.id}` }, [cb, el("span", {}, panel.title || panel.type)]);
  });
  return section(
    "Panel visibility",
    hint("Hiding a panel here just turns it off -- its grid position is kept, so showing it again puts it back exactly where it was."),
    el("div", { class: "settings-checkbox-list" }, rows)
  );
}

// ---- 5. theme preset ----

function renderThemeSection() {
  if (!S.theme) {
    return section("Theme preset", degraded("Theme config could not be loaded -- preset switching is unavailable this session."));
  }
  const presets = S.theme._presets || {};
  const presetNames = Object.keys(presets).filter((k) => k !== "_comment");

  const select = el("select", { class: "settings-input", "aria-label": "Theme preset" }, [
    el("option", { value: "" }, S.theme.name || "current"),
    ...presetNames.map((name) => el("option", { value: name }, name)),
  ]);
  select.value = "";
  select.addEventListener("change", () => {
    const name = select.value;
    if (!name) {
      S.theme = deepClone(S.themeOriginal);
    } else {
      const preset = presets[name];
      const merged = deepClone(S.themeOriginal);
      for (const group of ["colors", "fonts", "radius", "density", "motion", "reload_banner"]) {
        if (preset[group]) merged[group] = { ...merged[group], ...preset[group] };
      }
      merged.name = name;
      S.theme = merged;
    }
    applyTheme(S.theme); // live preview -- Cancel/Escape reverts via onClose
    updateFooter();
  });

  const children = [hint("Applies immediately as a preview. Nothing is written until you click Save.")];
  if (presetNames.length === 0) {
    children.push(degraded("No alternate presets defined in config/theme.json's _presets block."));
  }
  children.push(el("div", { class: "settings-row" }, [select]));
  return section("Theme preset", ...children);
}

// ---- 6. timezone ----

function renderTimezoneSection() {
  const detected = (S.suggest && S.suggest.timezone && S.suggest.timezone.detected) || "UTC";
  // normalizeLayout() already guarantees S.layout.timezone is set (on both
  // S.layout and S.layoutOriginal) -- read only, no fallback-assignment.
  const current = S.layout.timezone;

  const list = el(
    "datalist",
    { id: "settings-tz-list" },
    ["UTC", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "Europe/London", "Europe/Berlin", "Asia/Tokyo", "Asia/Kolkata", "Australia/Sydney"].map((z) =>
      el("option", { value: z })
    )
  );
  const input = el("input", {
    type: "text",
    class: "settings-input",
    list: "settings-tz-list",
    value: current,
    "aria-label": "Timezone",
  });
  const err = el("div", { class: "settings-field-error" }, "");
  input.addEventListener("input", () => {
    S.layout.timezone = input.value;
    err.textContent = isValidTimezone(input.value) ? "" : "Not a recognized IANA timezone name.";
    updateFooter();
  });

  const children = [
    hint("Stored data always stays in UTC -- this only changes how times are displayed."),
    el("div", { class: "settings-row" }, [input, list]),
    err,
  ];
  if (S.suggestStatus === "ok" && S.suggest.timezone) {
    children.push(el("div", { class: "settings-hint" }, `Detected: ${detected} (source: ${S.suggest.timezone.source || "unknown"})`));
  }
  return section("Timezone", ...children);
}

// ---- 7. refresh cadence (omitted entirely if snapshot doesn't expose it) ----

function renderRefreshCadenceSection() {
  const settings = S.snapshotSettings;
  if (!settings || !("refresh_cadence" in settings)) return null; // real behaviour today: omitted
  const select = el("select", { class: "settings-input", "aria-label": "Refresh cadence" }, [
    el("option", { value: "normal" }, "Normal"),
    el("option", { value: "relaxed" }, "Relaxed"),
  ]);
  select.value = settings.refresh_cadence === "relaxed" ? "relaxed" : "normal";
  select.addEventListener("change", () => {
    S.layout.settings = S.layout.settings || {};
    S.layout.settings.refresh_cadence = select.value;
    updateFooter();
  });
  return section("Refresh cadence", hint("Relaxed polls less often -- useful on a slow connection."), el("div", { class: "settings-row" }, [select]));
}

// ---- 8. updates (self-update: GET+POST /api/settings/updates) ----

function statusRow(label, value) {
  return el("div", { class: "table-card-field" }, [
    el("span", { class: "table-card-field-label" }, label),
    el("span", { class: "table-card-field-value" }, value),
  ]);
}

function renderUpdatesSection() {
  if (S.updatesStatus === "unavailable" || !S.updatesDraft) {
    return section(
      "Updates",
      degraded("Update settings are not available yet (the /api/settings/updates endpoint isn't live yet -- backend still landing it).")
    );
  }

  const draft = S.updatesDraft;
  const info = S.updatesInfo || {};
  const repoConfigured = !!info.repo;
  const children = [];

  if (!repoConfigured) {
    children.push(
      degraded(
        "Updates are not configured -- no repository is set for this install, so there is nothing to check yet. The settings below take effect once one is."
      )
    );
  }

  // Check for updates automatically -> check_enabled
  const enabledCb = el("input", { type: "checkbox", id: "settings-update-check-enabled" });
  enabledCb.checked = !!draft.check_enabled;
  enabledCb.addEventListener("change", () => {
    draft.check_enabled = enabledCb.checked;
    updateFooter();
  });
  children.push(
    el("label", { class: "settings-checkbox-row", for: "settings-update-check-enabled" }, [enabledCb, el("span", {}, "Check for updates automatically")])
  );

  // Check interval -> check_interval_s, edited in minutes; server clamps below 300s (5m).
  const intervalInput = el("input", {
    type: "number",
    min: "5",
    step: "1",
    class: "settings-input settings-input-inline",
    id: "settings-update-interval",
    "aria-label": "Check interval in minutes",
    value: String(Math.max(5, Math.round((draft.check_interval_s || 300) / 60))),
  });
  function commitInterval() {
    const raw = Number(intervalInput.value);
    const mins = Number.isFinite(raw) && raw > 0 ? Math.round(raw) : 5;
    const clampedMins = Math.max(5, mins);
    intervalInput.value = String(clampedMins); // reflect the clamp immediately -- never let the field imply a smaller value took
    draft.check_interval_s = clampedMins * 60;
    updateFooter();
  }
  intervalInput.addEventListener("change", commitInterval);
  children.push(
    el("div", { class: "settings-row" }, [
      el("label", { for: "settings-update-interval", class: "settings-hint", style: "margin-bottom:0;" }, "Check interval (minutes)"),
      intervalInput,
    ])
  );
  children.push(hint("The server enforces a 5 minute (300s) minimum -- entering less is rounded up."));

  // Apply updates automatically -> auto_apply. Deliberately styled apart from
  // the checkboxes above: this one lets the machine change its own running
  // code with nobody watching, so it does not get to look like an ordinary
  // preference (see AGENTS.md briefing / task spec).
  const autoCb = el("input", { type: "checkbox", id: "settings-update-autoapply" });
  autoCb.checked = !!draft.auto_apply;
  autoCb.addEventListener("change", () => {
    draft.auto_apply = autoCb.checked;
    updateFooter();
  });
  const originLabel = repoConfigured ? `${info.repo}${info.branch ? ` @ ${info.branch}` : ""}` : "the configured origin";
  children.push(
    el("div", { class: "settings-warn-box" }, [
      el("div", { class: "settings-warn-box-icon", "aria-hidden": "true" }, "⚠"),
      el("div", { class: "settings-warn-box-body" }, [
        el("label", { class: "settings-checkbox-row settings-warn-label", for: "settings-update-autoapply" }, [
          autoCb,
          el("span", {}, "Apply updates automatically — no confirmation"),
        ]),
        el(
          "div",
          { class: "settings-hint" },
          `Only fast-forward updates from ${originLabel} are ever applied -- never a rewritten or diverged history. With this on, the dashboard updates itself the moment a new commit is detected, with no one asked first. Leave it off to review and click "Update now" yourself.`
        ),
      ]),
    ])
  );

  // Read-only current state -- only shown once a repo exists to report on,
  // so an unconfigured install never renders a checker that looks broken.
  if (repoConfigured) {
    children.push(
      el("div", { class: "table-card-fields", style: "margin-top: 4px;" }, [
        statusRow("Repo", `${info.repo}${info.branch ? ` @ ${info.branch}` : ""}`),
        statusRow("Current commit", info.current || "--"),
        statusRow("Latest commit", info.latest || "--"),
        statusRow("Behind", typeof info.behind === "number" ? String(info.behind) : "--"),
        statusRow("Last checked", info.checked_at ? fmtRelTime(info.checked_at) : "never"),
      ])
    );
    if (info.last_error) {
      // A checker that has been failing silently must not read as "up to
      // date" -- surface the error plainly rather than just the last-known
      // current/latest pair, which alone would look healthy.
      children.push(degraded(`Last check failed: ${info.last_error}`));
    }
  }

  return section("Updates", ...children);
}

// ---- footer: dirty indicator, error box, cancel/save ----

function renderFooter() {
  const dirtyDot = el("span", { class: "settings-dirty-dot", "aria-hidden": "true" });
  const dirtyLabel = el("span", { class: "settings-dirty-label" }, "");
  const errorBox = el("div", { class: "settings-save-error", style: "display:none;" });
  const confirmBox = el("div", { class: "settings-save-confirm", style: "display:none;" }, "Saved.");

  const cancelBtn = el("button", { type: "button", class: "settings-btn-secondary" }, "Cancel");
  cancelBtn.addEventListener("click", () => closeModal());

  const saveBtn = el("button", { type: "button", class: "settings-btn-primary", id: "settings-save-btn" }, "Save");
  saveBtn.addEventListener("click", () => doSave(saveBtn, errorBox, confirmBox));

  S.els.dirtyDot = dirtyDot;
  S.els.dirtyLabel = dirtyLabel;
  S.els.saveBtn = saveBtn;
  S.els.errorBox = errorBox;
  S.els.confirmBox = confirmBox;
  updateFooter();

  return el("div", { class: "settings-footer" }, [
    el("div", { class: "settings-footer-status" }, [dirtyDot, dirtyLabel]),
    errorBox,
    confirmBox,
    el("div", { class: "settings-footer-actions" }, [cancelBtn, saveBtn]),
  ]);
}

function updateFooter() {
  const dirty = isDirty();
  if (S.els.dirtyDot) S.els.dirtyDot.classList.toggle("dirty", dirty);
  if (S.els.dirtyLabel) S.els.dirtyLabel.textContent = dirty ? "Unsaved changes" : "No changes";
  if (S.els.saveBtn) S.els.saveBtn.disabled = !dirty || S.saving;
}

async function doSave(saveBtn, errorBox, confirmBox) {
  if (saveBtn.disabled) return;
  const mySession = S.mySession;
  S.saving = true;
  saveBtn.disabled = true;
  saveBtn.textContent = "Saving…";
  errorBox.style.display = "none";
  errorBox.innerHTML = "";
  confirmBox.style.display = "none";

  const messages = [];
  const layoutChanged = JSON.stringify(S.layout) !== JSON.stringify(S.layoutOriginal);
  const themeChanged = S.theme && JSON.stringify(S.theme) !== JSON.stringify(S.themeOriginal);
  const updatesChanged = S.updatesDraft && JSON.stringify(S.updatesDraft) !== JSON.stringify(S.updatesDraftOriginal);

  if (layoutChanged) {
    const res = await postJSONSafe(LAYOUT_URL, S.layout);
    if (mySession !== session) return;
    if (res.ok) {
      S.layoutOriginal = deepClone(S.layout);
    } else {
      messages.push(`Title / labels / quota / panels / timezone: ${res.message}`);
    }
  }
  if (themeChanged) {
    const res = await postJSONSafe(THEME_URL, S.theme);
    if (mySession !== session) return;
    if (res.ok) {
      S.themeOriginal = deepClone(S.theme);
    } else {
      messages.push(`Theme preset: ${res.message}`);
    }
  }
  if (updatesChanged) {
    // The endpoint accepts exactly these three keys -- never send the
    // read-only fields (repo/current/latest/...) back, even though they
    // live in the same S.updatesInfo object for rendering.
    const res = await postJSONSafe(UPDATES_URL, S.updatesDraft);
    if (mySession !== session) return;
    if (res.ok) {
      // Server may clamp/normalize (e.g. the 300s interval floor) -- fold
      // whatever it echoes back into both the draft and the read-only info
      // so the form reflects reality, not just what the user typed. Mutated
      // in place (not reassigned) so the checkbox/input closures above,
      // which close over this same `draft` object, stay live if the dialog
      // re-renders on a later tick.
      if (res.data) {
        S.updatesInfo = { ...(S.updatesInfo || {}), ...res.data };
        if (typeof res.data.check_enabled === "boolean") S.updatesDraft.check_enabled = res.data.check_enabled;
        if (typeof res.data.check_interval_s === "number") S.updatesDraft.check_interval_s = res.data.check_interval_s;
        if (typeof res.data.auto_apply === "boolean") S.updatesDraft.auto_apply = res.data.auto_apply;
      }
      S.updatesDraftOriginal = deepClone(S.updatesDraft);
    } else {
      messages.push(`Updates: ${res.message}`);
    }
  }

  S.saving = false;
  saveBtn.textContent = "Save";

  if (messages.length === 0) {
    confirmBox.style.display = "";
    if (window.__critdashBus) window.__critdashBus.emit("settings:saved");
    updateFooter();
    setTimeout(() => {
      if (mySession === session) closeModal();
    }, 550);
    return;
  }

  errorBox.style.display = "";
  for (const m of messages) errorBox.appendChild(el("div", { class: "settings-field-error" }, m));
  updateFooter();
}
