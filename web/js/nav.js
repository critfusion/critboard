// Pure logic for the top-level view navigation (Live / Infrastructure /
// Workflow / Knowledge). No DOM access, so it runs under plain `node` (see
// nav.test.mjs).
//
// Live is always there. The other views exist only when the server lists
// them in GET /api/modules, which it does only for modules a person enabled
// in config/sources.json. With none enabled there is one view, and the page
// shows no navigation at all: it is the plain board.
//
// The current view lives in the `view` query parameter, never in the hash:
// the hash belongs to detail deep links (#bead/<id>, see modal.js).

export const VIEW_ORDER = ["live", "infrastructure", "workflow", "knowledge"];
export const VIEW_LABELS = {
  live: "Live",
  infrastructure: "Infrastructure",
  workflow: "Workflow",
  knowledge: "Knowledge",
};

/** `enabled` is the "enabled" array from /api/modules (any junk -> Live only). */
export function availableViews(enabled) {
  const on = new Set(Array.isArray(enabled) ? enabled : []);
  return VIEW_ORDER.filter((v) => v === "live" || on.has(v));
}

/** The nav bar exists only when there is something besides Live to go to. */
export function showNav(views) {
  return views.length > 1;
}

/** A requested view that is not available falls back to Live. */
export function resolveView(requested, views) {
  return views.includes(requested) ? requested : "live";
}

export function viewFromSearch(search) {
  return new URLSearchParams(search).get("view") || "live";
}

/** New query string for `view`, keeping every other parameter (?fixture=...).
 * Live is the default and carries no parameter. */
export function searchWithView(search, view) {
  const p = new URLSearchParams(search);
  if (view === "live") p.delete("view");
  else p.set("view", view);
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** Arrow-key target inside the nav; null when the key is not an arrow/Home/End. */
export function neighbourView(views, current, key) {
  const i = views.indexOf(current);
  if (i === -1) return null;
  if (key === "ArrowRight") return views[(i + 1) % views.length];
  if (key === "ArrowLeft") return views[(i - 1 + views.length) % views.length];
  if (key === "Home") return views[0];
  if (key === "End") return views[views.length - 1];
  return null;
}
