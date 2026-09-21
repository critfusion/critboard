// Shared formatting + small DOM helpers used by app.js and widgets.
// Not a widget itself -- widgets may import this, but never each other.

export function fmtTokens(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  const abs = Math.abs(n);
  if (abs < 1000) return String(Math.round(n));
  if (abs < 1_000_000) return (n / 1_000).toFixed(1).replace(/\.0$/, "") + "K";
  if (abs < 1_000_000_000) return (n / 1_000_000).toFixed(1) + "M";
  return (n / 1_000_000_000).toFixed(2) + "B";
}

export function fmtInt(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  return Math.round(n).toLocaleString("en-US");
}

export function fmtCost(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  const sign = n < 0 ? "-" : "";
  return sign + "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function fmtPct(x, digits = 0) {
  if (x === null || x === undefined || Number.isNaN(x)) return "--";
  return (x * 100).toFixed(digits) + "%";
}

export function fmtRelTime(iso, nowMs) {
  if (!iso) return "--";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "--";
  const now = nowMs ?? Date.now();
  let diff = Math.round((now - t) / 1000);
  const future = diff < 0;
  diff = Math.abs(diff);
  let out;
  if (diff < 5) out = "now";
  else if (diff < 60) out = diff + "s";
  else if (diff < 3600) out = Math.floor(diff / 60) + "m";
  else if (diff < 86400) out = Math.floor(diff / 3600) + "h";
  else out = Math.floor(diff / 86400) + "d";
  return future ? "in " + out : out + " ago";
}

export function fmtAbsTime(iso) {
  if (!iso) return "--";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--";
  return d.toISOString().replace("T", " ").slice(0, 19) + "Z";
}

export function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "--";
  seconds = Math.max(0, Math.round(seconds));
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

export function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c === null || c === undefined) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

// Get a value out of an object by dotted path, e.g. get(data, "usage.burn.usd_per_hour_1h")
export function get(obj, path, fallback) {
  if (!path) return fallback;
  const parts = path.split(".");
  let cur = obj;
  for (const p of parts) {
    if (cur === null || cur === undefined) return fallback;
    cur = cur[p];
  }
  return cur === undefined ? fallback : cur;
}

// Set a value at a dotted path, replacing (not deep-merging) at that path,
// creating intermediate objects as needed. Used for SSE patch application.
export function setPath(obj, path, value) {
  const parts = path.split(".");
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    const p = parts[i];
    if (typeof cur[p] !== "object" || cur[p] === null) cur[p] = {};
    cur = cur[p];
  }
  cur[parts[parts.length - 1]] = value;
}

export function severityColorVar(sev) {
  return { info: "--color-severity-info", warn: "--color-severity-warn", crit: "--color-severity-crit" }[sev] || "--color-text-dim";
}

export function statusColorVar(status) {
  return {
    working: "--color-status-working",
    idle: "--color-status-idle",
    done: "--color-status-done",
    unknown: "--color-text-faint",
  }[status] || "--color-text-faint";
}

export function priorityColorVar(p) {
  return `--color-priority-${Math.min(3, Math.max(0, p ?? 3))}`;
}

export function clamp(n, lo, hi) {
  return Math.max(lo, Math.min(hi, n));
}

// localStorage helpers -- wrapped in try/catch so a private window or
// blocked storage degrades to "nothing persisted" instead of breaking
// rendering. Used by table widgets to remember per-panel sort/filter state.
export function loadJSON(key, fallback) {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return fallback;
    const parsed = JSON.parse(raw);
    return parsed === null || parsed === undefined ? fallback : parsed;
  } catch (e) {
    return fallback;
  }
}

export function saveJSON(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch (e) {
    // ignore -- private window, quota exceeded, or storage blocked
  }
}

// Mobile row-cap for genuinely unbounded lists (activity feed, commit feed,
// bead/worktree tables, kanban lanes). `panel.mobile.limit` in
// config/layout.json overrides `fallback`; both must be a positive number
// or the fallback wins.
export function mobileLimit(panel, fallback) {
  const v = panel && panel.mobile ? panel.mobile.limit : undefined;
  return typeof v === "number" && v > 0 ? v : fallback;
}

// Renders `items` capped to `limit`, plus a "Show all (N)" button that
// expands to the full list in place when there are more than `limit`.
// `renderList(list)` must build and return a fresh DOM node containing
// exactly `list.length` rows/cards; it is called once up front with the
// capped slice, and again with the full array if the button is clicked.
// No-op wrapper (just calls renderList(items) once) when items already fit.
export function renderWithShowAll(container, items, limit, renderList) {
  const total = items.length;
  if (!limit || total <= limit) {
    container.appendChild(renderList(items));
    return;
  }
  let host = renderList(items.slice(0, limit));
  container.appendChild(host);
  const btn = el("button", { type: "button", class: "show-all-btn" }, `Show all (${total})`);
  btn.addEventListener("click", () => {
    const fullHost = renderList(items);
    host.replaceWith(fullHost);
    host = fullHost;
    btn.remove();
  });
  container.appendChild(btn);
}
