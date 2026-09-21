import { getWidget, dependencies } from "./registry.js";
import { get, setPath, fmtRelTime, el } from "./utils.js";
import * as modal from "./modal.js";

// ---------------- data source resolution ----------------
// Two ways to run against the fixture instead of the live API (documented
// in README.md): open web/dev.html, or add ?fixture=1 (or ?fixture=degraded)
// to index.html's URL.

const qs = new URLSearchParams(location.search);
const fixtureFlag = window.__DASHBOARD_FIXTURE__ === true || qs.has("fixture");
const fixtureName = qs.get("fixture");
const FIXTURE_MODE = fixtureFlag;
const FIXTURE_PATH =
  fixtureName === "degraded" ? "fixtures/snapshot-degraded.json" : "fixtures/snapshot.json";

const SNAPSHOT_URL = "/api/snapshot";
const STREAM_URL = "/api/stream";
// Fixture mode is served as a plain static directory (see README), so config
// is reached through web/config -> a symlink to ../config, not a relative
// "../" path outside the served root.
const LAYOUT_URL = FIXTURE_MODE ? "config/layout.json" : "/api/config/layout";
const THEME_URL = FIXTURE_MODE ? "config/theme.json" : "/api/config/theme";

const state = {
  data: null,
  layout: null,
  theme: null,
  selectedWindow: "today",
  lastGoodAt: null,
  connStatus: FIXTURE_MODE ? "live" : "offline",
  reconnectAttempt: 0,
  panelEls: {}, // panel id -> { el, panel, widget }
  knownBuild: null,
  knownStartedAt: null,
  dismissedVersionKey: null,
  autoReloadTimer: null,
  breakpoint: "desktop", // "mobile" | "tablet" | "desktop" -- see computeBreakpoint()
};

const bus = {
  _handlers: {},
  on(evt, fn) {
    (this._handlers[evt] ||= []).push(fn);
    return () => this.off(evt, fn);
  },
  off(evt, fn) {
    this._handlers[evt] = (this._handlers[evt] || []).filter((f) => f !== fn);
  },
  emit(evt, payload) {
    for (const fn of this._handlers[evt] || []) {
      try {
        fn(payload);
      } catch (e) {
        console.error("[bus]", evt, e);
      }
    }
  },
};
window.__critdashBus = bus;

// ---------------- theme ----------------

function applyTheme(theme) {
  if (!theme) return;
  const root = document.documentElement.style;
  const groups = { color: theme.colors, font: theme.fonts, radius: theme.radius, density: theme.density, motion: theme.motion };
  for (const [prefix, obj] of Object.entries(groups)) {
    if (!obj) continue;
    for (const [k, v] of Object.entries(obj)) {
      root.setProperty(`--${prefix}-${k}`, v);
    }
  }
}

// ---------------- layout / grid ----------------
//
// The grid engine is breakpoint-aware: it computes a layout mode from the
// viewport width against config/layout.json's grid.breakpoints (data, not a
// hardcoded value) and emits different inline placement per panel per mode.
// This has to happen in JS, not CSS, because desktop placement is itself an
// inline style (gridColumn/gridRow span) that a plain CSS media query cannot
// override -- see buildPanelPlacement() below for the three modes.

function computeBreakpoint(layout) {
  const bp = (layout && layout.grid && layout.grid.breakpoints) || {};
  const mobileMax = typeof bp.mobile_max === "number" ? bp.mobile_max : 640;
  const tabletMax = typeof bp.tablet_max === "number" ? bp.tablet_max : 1024;
  const w = window.innerWidth;
  if (w <= mobileMax) return "mobile";
  if (w <= tabletMax) return "tablet";
  return "desktop";
}

// Total pixel height a panel spanning `h` grid rows occupies, mirroring how
// CSS Grid would size an h-row span (h row-heights + (h-1) internal gaps).
// Used on mobile/tablet where panels get an explicit height instead of a
// gridRow span, since row math no longer means anything once column count
// changes out from under the desktop y-coordinates.
function panelHeightPx(h, rowHeight, gap) {
  return h * rowHeight + Math.max(0, h - 1) * gap;
}

// Panel types that render into a canvas/chart library instead of flowed
// DOM content (currently: spend_timeline's uPlot chart). Those can't
// self-size from their own content the way text/tables/cards can -- they
// need an explicit pixel height to lay a canvas into. Everything else gets
// no inline height on mobile so it sizes to content and the page is the
// only scroll surface (see .panel-body / .table-scroll overflow rules in
// style.css, which only apply the inner-scroll behavior above 640px).
const CHART_PANEL_TYPES = new Set(["spend_timeline", "usage_history"]);

// Which panels render, and in what order, for a given breakpoint. Desktop
// keeps declaration order (byte-for-byte unchanged). Mobile drops
// `mobile.hidden` panels and sorts by `mobile.order`, falling back to
// declaration order for panels that don't specify one. Tablet keeps
// declaration order (two-column reflow only, no reordering).
function panelsForBreakpoint(layout, bp) {
  const panels = layout.panels || [];
  const withIdx = panels.map((panel, idx) => ({ panel, idx }));
  if (bp !== "mobile") return withIdx;
  const visible = withIdx.filter(({ panel }) => !(panel.mobile && panel.mobile.hidden));
  visible.sort((a, b) => {
    const ao = a.panel.mobile && typeof a.panel.mobile.order === "number" ? a.panel.mobile.order : a.idx;
    const bo = b.panel.mobile && typeof b.panel.mobile.order === "number" ? b.panel.mobile.order : b.idx;
    return ao !== bo ? ao - bo : a.idx - b.idx;
  });
  return visible;
}

// Applies the per-panel placement for the current breakpoint onto `card`.
function applyPanelPlacement(card, panel, bp, gridOpts) {
  const { row_height, gap } = gridOpts;
  if (bp === "desktop") {
    // Unchanged from the original implementation.
    card.style.gridColumn = `${panel.x + 1} / span ${panel.w}`;
    card.style.gridRow = `${panel.y + 1} / span ${panel.h}`;
  } else if (bp === "tablet") {
    // Two columns. Panels declaring w >= 8 on desktop span both columns;
    // everything else takes a single column and flows (grid-auto-flow).
    card.style.gridColumn = panel.w >= 8 ? "1 / -1" : "auto / span 1";
    card.style.gridRow = "auto";
    card.style.height = `${panelHeightPx(panel.h, row_height, gap)}px`;
  } else {
    // Mobile: single column, panels flow in DOM order -- no inline
    // gridColumn spanning at all (the #grid element itself isn't even a
    // grid in this mode; see buildGrid). `mobile.h` is a hint honored only
    // by chart-type panels that need a real pixel height (see
    // CHART_PANEL_TYPES); everything else sizes to its own content.
    if (CHART_PANEL_TYPES.has(panel.type)) {
      const mh = panel.mobile && typeof panel.mobile.h === "number" ? panel.mobile.h : panel.h;
      card.style.height = `${panelHeightPx(mh, row_height, gap)}px`;
    } else {
      card.style.height = "";
    }
  }
}

// Disconnects per-panel resources (currently: uPlot's ResizeObserver in
// spend_timeline.js) before the DOM nodes they're attached to are discarded,
// so toggling breakpoints back and forth doesn't leak observers.
function teardownPanels() {
  for (const entry of Object.values(state.panelEls)) {
    if (entry.body && entry.body.__ro) {
      try {
        entry.body.__ro.disconnect();
      } catch (e) {
        // already disconnected
      }
      entry.body.__ro = null;
    }
  }
}

function buildGrid(layout) {
  teardownPanels();

  const wrap = document.getElementById("grid-wrap");
  const grid = document.getElementById("grid");
  grid.innerHTML = "";
  state.panelEls = {};

  const { columns = 12, row_height = 80, gap = 14 } = layout.grid || {};
  const bp = computeBreakpoint(layout);
  state.breakpoint = bp;
  grid.dataset.breakpoint = bp;

  if (bp === "desktop") {
    grid.style.display = "grid";
    grid.style.gridTemplateColumns = `repeat(${columns}, minmax(0, 1fr))`;
    grid.style.gridAutoRows = `${row_height}px`;
    grid.style.gap = `${gap}px`;
  } else if (bp === "tablet") {
    grid.style.display = "grid";
    grid.style.gridTemplateColumns = "repeat(2, minmax(0, 1fr))";
    grid.style.gridAutoRows = "auto";
    grid.style.gap = `${gap}px`;
  } else {
    // mobile: plain flow, no CSS grid at all -- panels stack in DOM order.
    grid.style.display = "flex";
    grid.style.flexDirection = "column";
    grid.style.gridTemplateColumns = "";
    grid.style.gridAutoRows = "";
    grid.style.gap = `${gap}px`;
  }

  document.getElementById("app-title").textContent = layout.title || "CritBoard Mission Control";

  const entries = panelsForBreakpoint(layout, bp);

  entries.forEach(({ panel, idx }) => {
    const card = el("div", { class: "panel", "data-panel-id": panel.id, "data-panel-idx": idx + 1 });
    applyPanelPlacement(card, panel, bp, { row_height, gap });

    const header = el("div", { class: "panel-header" }, [
      el("span", { class: "panel-title" }, panel.title || panel.type),
      idx < 9 ? el("span", { class: "panel-idx" }, String(idx + 1)) : null,
    ]);
    const body = el("div", { class: "panel-body" });
    card.appendChild(header);
    card.appendChild(body);
    grid.appendChild(card);

    const widget = getWidget(panel.type);
    state.panelEls[panel.id] = { el: card, body, panel, widget, idx };
  });

  wrap.setAttribute("aria-busy", "false");
}

function ctxFor(panel) {
  return {
    data: state.data,
    options: panel.options || {},
    panel,
    bus,
    window: state.selectedWindow,
    breakpoint: state.breakpoint,
    layout: state.layout,
  };
}

function renderPanel(id, { useUpdate = false } = {}) {
  const entry = state.panelEls[id];
  if (!entry) return;
  const { body, panel, widget, el: card } = entry;

  card.classList.remove("panel-error", "panel-placeholder");

  if (!widget) {
    card.classList.add("panel-placeholder");
    body.classList.add("no-pad");
    body.innerHTML = "";
    body.appendChild(
      el("div", {}, [
        el("div", { style: "font-size: 22px; margin-bottom: 6px;" }, "⚠"),
        el("div", {}, `Unknown widget type "${panel.type}"`),
        el("div", { class: "faint", style: "margin-top: 4px; font-size: 10px;" }, `panel id: ${panel.id}`),
      ])
    );
    return;
  }

  body.classList.remove("no-pad");

  try {
    const ctx = ctxFor(panel);
    if (useUpdate && typeof widget.update === "function") {
      widget.update(body, ctx);
    } else {
      body.innerHTML = "";
      widget.render(body, ctx);
    }
  } catch (err) {
    console.error(`[widget:${panel.type}]`, err);
    card.classList.add("panel-error");
    body.classList.remove("no-pad");
    body.innerHTML = "";
    body.appendChild(
      el("div", {}, [
        el("div", { style: "font-weight: 700; margin-bottom: 4px;" }, `Widget error: ${panel.type}`),
        el("div", {}, String((err && err.message) || err)),
      ])
    );
  }
}

function renderAllPanels() {
  for (const id of Object.keys(state.panelEls)) renderPanel(id);
  modal.refreshOpen();
}

function renderPanelsForKeys(keys) {
  for (const [id, entry] of Object.entries(state.panelEls)) {
    const deps = dependencies[entry.panel.type];
    if (deps === null || deps === undefined || deps.some((d) => keys.has(d))) {
      renderPanel(id, { useUpdate: true });
    }
  }
  modal.refreshOpen();
}

// ---------------- reload banner ----------------
// Backend adds `version: {"build": "<hash>", "started_at": "<iso>"}` to the
// snapshot and emits a dedicated `version` SSE event with the same shape
// whenever the build hash (contents of web/ + config/) changes. We record
// the first build/started_at we ever see as the baseline (no banner on
// first load) and only alert on a later, DIFFERENT value. Missing the
// field entirely means: do nothing, ever -- no spurious prompts.

function versionKey(v) {
  return v && v.build ? `${v.build}::${v.started_at || ""}` : null;
}

function showReloadBanner(v) {
  const cfg = state.theme?.reload_banner || {};
  if (cfg.enabled === false) return;
  const banner = document.getElementById("reload-banner");
  if (!banner) return;
  if (state.autoReloadTimer) {
    clearTimeout(state.autoReloadTimer);
    state.autoReloadTimer = null;
  }
  banner.dataset.versionKey = versionKey(v) || "";
  banner.classList.add("show");
  if (typeof cfg.auto_reload_after_s === "number" && cfg.auto_reload_after_s >= 0) {
    state.autoReloadTimer = setTimeout(() => location.reload(), cfg.auto_reload_after_s * 1000);
  }
}

function hideReloadBanner(dismiss) {
  const banner = document.getElementById("reload-banner");
  if (!banner) return;
  if (dismiss) state.dismissedVersionKey = banner.dataset.versionKey || null;
  banner.classList.remove("show");
  if (state.autoReloadTimer) {
    clearTimeout(state.autoReloadTimer);
    state.autoReloadTimer = null;
  }
}

function checkVersion(v) {
  if (!v || !v.build) return; // field absent -> never show anything
  if (state.knownBuild === null) {
    // first value ever seen: this is the baseline, not a "new" version.
    state.knownBuild = v.build;
    state.knownStartedAt = v.started_at || null;
    return;
  }
  const changed = v.build !== state.knownBuild || (v.started_at || null) !== state.knownStartedAt;
  if (!changed) return;
  state.knownBuild = v.build;
  state.knownStartedAt = v.started_at || null;
  const key = versionKey(v);
  if (key === state.dismissedVersionKey) return; // already dismissed this exact version
  showReloadBanner(v);
}

// Test/debug hook: lets Playwright (or manual console use) simulate a
// version change without a real backend, e.g.
//   window.__critdashSetVersion({build: "abc1234", started_at: "..."})
window.__critdashSetVersion = (v) => {
  if (state.data) state.data.version = v;
  checkVersion(v);
};

// ---------------- connection indicator ----------------

function setConn(status) {
  state.connStatus = status;
  const node = document.getElementById("conn-indicator");
  node.classList.remove("live", "reconnecting", "offline");
  node.classList.add(status);
  document.getElementById("conn-label").textContent =
    status === "live" ? "LIVE" : status === "reconnecting" ? "RECONNECTING" : "OFFLINE";
}

function tickClock() {
  const now = new Date();
  document.getElementById("clock").textContent = now.toISOString().replace("T", " ").slice(0, 19) + "Z";
  const prefixEl = document.getElementById("conn-age-prefix");
  const valueEl = document.getElementById("conn-age-value");
  if (state.lastGoodAt) {
    // Split so narrow viewports can drop the "snapshot " prefix and keep
    // just the relative age (still enough to tell the data is fresh).
    prefixEl.textContent = "snapshot ";
    valueEl.textContent = fmtRelTime(state.lastGoodAt.toISOString());
  } else {
    prefixEl.textContent = "";
    valueEl.textContent = "no snapshot yet";
  }
}
setInterval(tickClock, 1000);

// ---------------- data loading ----------------

async function fetchJSON(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  return res.json();
}

async function loadSnapshot() {
  const url = FIXTURE_MODE ? FIXTURE_PATH : SNAPSHOT_URL;
  const data = await fetchJSON(url);
  state.data = data;
  window.__critdashData = data;
  state.lastGoodAt = new Date();
  checkVersion(data.version);
  renderAllPanels();
}

async function loadConfig() {
  try {
    state.layout = await fetchJSON(LAYOUT_URL);
  } catch (e) {
    console.error("layout load failed, using minimal fallback", e);
    state.layout = { title: "CritBoard Mission Control", grid: { columns: 12, row_height: 80, gap: 14 }, panels: [] };
  }
  try {
    state.theme = await fetchJSON(THEME_URL);
  } catch (e) {
    console.error("theme load failed, using CSS defaults", e);
    state.theme = null;
  }
  applyTheme(state.theme);
  buildGrid(state.layout);
}

// ---------------- SSE ----------------

let es = null;
let backoffMs = 1000;
const BACKOFF_MAX = 30000;

function connectStream() {
  if (FIXTURE_MODE) return; // fixture mode has no backend to stream from
  try {
    es = new EventSource(STREAM_URL);
  } catch (e) {
    scheduleReconnect();
    return;
  }

  es.addEventListener("open", () => {
    backoffMs = 1000;
    state.reconnectAttempt = 0;
    setConn("live");
  });

  es.addEventListener("snapshot", (ev) => {
    try {
      state.data = JSON.parse(ev.data);
      window.__critdashData = state.data;
      state.lastGoodAt = new Date();
      setConn("live");
      checkVersion(state.data.version);
      renderAllPanels();
    } catch (e) {
      console.error("bad snapshot event", e);
    }
  });

  es.addEventListener("patch", (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      const paths = msg.paths || {};
      const touchedRoots = new Set();
      for (const [path, value] of Object.entries(paths)) {
        if (!state.data) state.data = {};
        setPath(state.data, path, value);
        touchedRoots.add(path.split(".")[0]);
      }
      window.__critdashData = state.data;
      state.lastGoodAt = new Date();
      setConn("live");
      if (touchedRoots.has("version")) checkVersion(state.data.version);
      renderPanelsForKeys(touchedRoots);
    } catch (e) {
      console.error("bad patch event", e);
    }
  });

  es.addEventListener("version", (ev) => {
    try {
      checkVersion(JSON.parse(ev.data));
    } catch (e) {
      console.error("bad version event", e);
    }
  });

  es.addEventListener("ping", () => {
    setConn("live");
  });

  es.addEventListener("error", () => {
    setConn("reconnecting");
    es.close();
    scheduleReconnect();
  });
}

function scheduleReconnect() {
  state.reconnectAttempt += 1;
  const delay = Math.min(BACKOFF_MAX, backoffMs);
  backoffMs = Math.min(BACKOFF_MAX, backoffMs * 2);
  setTimeout(async () => {
    if (state.reconnectAttempt > 3) setConn("offline");
    try {
      await loadSnapshot();
    } catch (e) {
      // still down; keep backing off
    }
    connectStream();
  }, delay);
}

// ---------------- window selector ----------------

function buildWindowSelector() {
  const wrap = document.getElementById("window-selector");
  wrap.innerHTML = "";
  const options = [
    ["today", "TODAY"],
    ["7d", "7D"],
    ["30d", "30D"],
  ];
  for (const [val, label] of options) {
    const btn = el("button", { class: val === state.selectedWindow ? "active" : "" }, label);
    btn.addEventListener("click", () => {
      state.selectedWindow = val;
      for (const b of wrap.children) b.classList.remove("active");
      btn.classList.add("active");
      bus.emit("window:change", val);
      renderAllPanels();
    });
    wrap.appendChild(btn);
  }
}

// ---------------- keyboard shortcuts ----------------

function isTypingTarget(t) {
  return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
}

function focusPanelByIndex(n) {
  document.querySelectorAll(".panel.panel-focused").forEach((p) => p.classList.remove("panel-focused"));
  const target = document.querySelector(`.panel[data-panel-idx="${n}"]`);
  if (target) {
    target.classList.add("panel-focused");
    target.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function setupKeyboard() {
  document.addEventListener("keydown", (e) => {
    if (isTypingTarget(e.target)) return;
    if (e.key === "?") {
      document.getElementById("help-overlay").classList.toggle("open");
    } else if (e.key === "Escape") {
      document.getElementById("help-overlay").classList.remove("open");
      hideReloadBanner(true);
    } else if (e.key === "r" || e.key === "R") {
      loadSnapshot().catch((err) => console.error("resnapshot failed", err));
    } else if (e.key === "f" || e.key === "F") {
      if (!document.fullscreenElement) document.documentElement.requestFullscreen?.();
      else document.exitFullscreen?.();
    } else if (/^[1-9]$/.test(e.key)) {
      focusPanelByIndex(Number(e.key));
    }
  });

  document.getElementById("help-close")?.addEventListener("click", () => {
    document.getElementById("help-overlay").classList.remove("open");
  });
  document.getElementById("help-overlay")?.addEventListener("click", (e) => {
    if (e.target.id === "help-overlay") e.currentTarget.classList.remove("open");
  });

  document.getElementById("reload-banner-go")?.addEventListener("click", () => location.reload());
  document.getElementById("reload-banner-dismiss")?.addEventListener("click", () => hideReloadBanner(true));
}

// ---------------- responsive: breakpoint changes on resize/orientation ----------------
// Debounced so a drag-resize doesn't rebuild the grid on every pixel; only
// rebuilds when the *breakpoint* actually changes (mobile/tablet/desktop),
// not on every resize event. Attached once in boot() -- never re-attached on
// a grid rebuild, so it can't leak duplicate listeners across re-renders.

let resizeDebounceTimer = null;

function onViewportChange() {
  if (resizeDebounceTimer) clearTimeout(resizeDebounceTimer);
  resizeDebounceTimer = setTimeout(() => {
    resizeDebounceTimer = null;
    if (!state.layout) return;
    const bp = computeBreakpoint(state.layout);
    if (bp === state.breakpoint) return;
    buildGrid(state.layout);
    renderAllPanels();
  }, 150);
}

function setupResponsive() {
  window.addEventListener("resize", onViewportChange);
  window.addEventListener("orientationchange", onViewportChange);
}

// ---------------- boot ----------------

async function boot() {
  setupKeyboard();
  setupResponsive();
  buildWindowSelector();
  setConn(FIXTURE_MODE ? "live" : "offline");

  await loadConfig();

  try {
    await loadSnapshot();
  } catch (e) {
    console.error("initial snapshot load failed", e);
    setConn("offline");
  }

  modal.resolveInitialHash();

  if (FIXTURE_MODE) {
    setConn("live");
    document.getElementById("conn-label").textContent = "FIXTURE";
  } else {
    connectStream();
  }

  tickClock();
}

boot();
