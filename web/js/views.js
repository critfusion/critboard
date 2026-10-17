// Top-level view switching. Live is the existing board (#grid-wrap); the
// other views are opt-in modules the server reports in /api/modules.
//
// With no module enabled this file adds nothing to the page: no nav, no
// extra containers shown, no behavior change.
//
// Switching views only toggles the `hidden` attribute. It never rebuilds the
// Live grid and never touches the detail modal, so an unsaved bead reply
// draft, a typed search and the scroll state of each view survive a switch.

import { mountInfrastructure } from "./infrastructure_view.js";
import { mountKnowledge } from "./knowledge_view.js";
import {
  VIEW_LABELS, availableViews, neighbourView, resolveView, searchWithView, showNav, viewFromSearch,
} from "./nav.js";
import { el } from "./utils.js";

const mounted = {}; // view -> { section, onShow }

function mountView(view, section) {
  if (view === "infrastructure") return mountInfrastructure(section);
  if (view === "knowledge") return mountKnowledge(section);
  // Reserved names: switchable, no data source in this version.
  section.appendChild(el("p", { class: "view-placeholder" },
    `${VIEW_LABELS[view]} is enabled, but this version has no data for it yet.`));
  return {};
}

async function fetchEnabled() {
  try {
    const resp = await fetch("/api/modules", { cache: "no-store" });
    if (!resp.ok) return [];
    return (await resp.json()).enabled;
  } catch (e) {
    return []; // an older server or a dead one: the plain board
  }
}

export async function setupViews() {
  const views = availableViews(await fetchEnabled());
  if (!showNav(views)) {
    // Drop a stale ?view= (bookmark from when a module was on) without adding history.
    if (new URLSearchParams(location.search).has("view")) {
      history.replaceState(null, "", (searchWithView(location.search, "live") || location.pathname) + location.hash);
    }
    return;
  }

  const nav = document.getElementById("view-nav");
  const host = document.getElementById("view-host");
  const gridWrap = document.getElementById("grid-wrap");
  const windowSel = document.getElementById("window-selector");

  const links = {};
  for (const view of views) {
    const a = el("a", { href: searchWithView(location.search, view) || location.pathname, "data-view": view }, VIEW_LABELS[view]);
    a.addEventListener("click", (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; // let "open in new tab" work
      e.preventDefault();
      show(view, { push: true });
    });
    a.addEventListener("keydown", (e) => {
      const next = neighbourView(views, a.dataset.view, e.key);
      if (next) {
        e.preventDefault();
        links[next].focus();
      }
    });
    links[view] = a;
    nav.appendChild(a);
    if (view !== "live") {
      const section = el("section", { id: `view-${view}`, class: "view-section", hidden: "" });
      section.setAttribute("aria-label", VIEW_LABELS[view]);
      host.appendChild(section);
      mounted[view] = { section, ...mountView(view, section) };
    }
  }
  nav.hidden = false;

  function show(view, { push }) {
    if (push) history.pushState(null, "", (searchWithView(location.search, view) || location.pathname) + location.hash);
    const live = view === "live";
    gridWrap.hidden = !live;
    windowSel.hidden = !live; // TODAY/7D/30D filters Live metrics only
    host.hidden = live;
    for (const [v, m] of Object.entries(mounted)) m.section.hidden = v !== view;
    for (const [v, a] of Object.entries(links)) {
      if (v === view) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
    if (!live) mounted[view].onShow?.();
  }

  const current = () => resolveView(viewFromSearch(location.search), views);
  window.addEventListener("popstate", () => show(current(), { push: false }));
  const initial = current();
  if (viewFromSearch(location.search) !== initial && new URLSearchParams(location.search).has("view")) {
    // ?view= names a view that is not enabled: drop it, no history entry.
    history.replaceState(null, "", (searchWithView(location.search, initial) || location.pathname) + location.hash);
  }
  show(initial, { push: false });
}
