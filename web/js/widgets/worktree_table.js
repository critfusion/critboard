import { el, fmtRelTime, loadJSON, saveJSON, mobileLimit, renderWithShowAll } from "../utils.js";
import { openWorktreeModal } from "../detail.js";

// Two repo_roots (e.g. /home/user/repos and /home/user/src, per config/sources.json)
// legitimately hold distinct checkouts of the same repo name, which reads as
// duplicated data unless the root is shown alongside the name.
const HOME_ROOT_RE = /^(\/home\/[^/]+)(\/.*)?$/;

function shortRoot(root) {
  if (!root) return "-";
  const m = HOME_ROOT_RE.exec(root);
  if (m) return "~" + (m[2] || "");
  return root;
}

const ROOT_FILTERS = [
  { key: "all", label: "ALL ROOTS", match: () => true },
  { key: "home", label: "~/repos", match: (w) => HOME_ROOT_RE.test(w.root || "") },
];

const COLUMNS = [
  { key: "repo", label: "REPO", type: "text", num: false, get: (w) => w.repo || "" },
  { key: "host", label: "HOST", type: "text", num: false, get: (w) => w.host || "" },
  { key: "branch", label: "BRANCH", type: "text", num: false, get: (w) => w.branch || "" },
  { key: "dirty", label: "DIRTY", type: "number", num: true, get: (w) => (w.dirty ?? 0) + (w.untracked ?? 0) + (w.staged ?? 0) },
  { key: "ahead", label: "↑/↓", type: "number", num: true, get: (w) => w.ahead ?? 0 },
  { key: "last_commit", label: "LAST COMMIT", type: "date", num: false, get: (w) => (w.last_commit_at ? new Date(w.last_commit_at).getTime() : null) },
  { key: "agents", label: "AGENTS", type: "number", num: true, get: (w) => (w.agents || []).length },
  { key: "stale", label: "STALE", type: "number", num: true, get: (w) => w.stale_days ?? 0 },
];

const SEARCH_FIELDS = ["repo", "branch", "root", "host", "last_commit_msg", "last_commit_author"];

function storageKey(panelId) {
  return `critdash.table.${panelId}`;
}

function defaultStateFor(options) {
  const legacy = options?.sort;
  let sortKey = "repo";
  let sortDir = "asc";
  if (legacy === "dirty") {
    sortKey = "dirty";
    sortDir = "desc";
  } else if (legacy === "stale") {
    sortKey = "stale";
    sortDir = "desc";
  } else if (COLUMNS.some((c) => c.key === legacy)) {
    sortKey = legacy;
  }
  return { sortKey, sortDir, query: "", dirtyOnly: false, rootFilter: "all", hostFilter: "all" };
}

// Host list is dynamic (fleet hosts can be added under us) -- never a static
// array. Computed fresh from the current data each time the toolbar is built
// or synced.
function uniqueHosts(data) {
  const items = Array.isArray(data?.worktrees) ? data.worktrees : [];
  const set = new Set();
  for (const w of items) if (w.host) set.add(w.host);
  return Array.from(set).sort();
}

function compareValues(a, b, type) {
  if (a === null || a === undefined) return b === null || b === undefined ? 0 : -1;
  if (b === null || b === undefined) return 1;
  if (type === "number" || type === "date") return a - b;
  return String(a).toLowerCase().localeCompare(String(b).toLowerCase());
}

function computeRows(data, tstate) {
  const items = Array.isArray(data?.worktrees) ? data.worktrees : [];
  const total = items.length;
  const q = tstate.query.trim().toLowerCase();
  const rootFilter = ROOT_FILTERS.find((r) => r.key === tstate.rootFilter) || ROOT_FILTERS[0];

  let rows = items.filter((w) => {
    if (tstate.dirtyOnly && (w.dirty ?? 0) + (w.untracked ?? 0) + (w.staged ?? 0) === 0) return false;
    if (!rootFilter.match(w)) return false;
    if (tstate.hostFilter && tstate.hostFilter !== "all" && (w.host || "") !== tstate.hostFilter) return false;
    if (!q) return true;
    return SEARCH_FIELDS.some((f) => String(w[f] || "").toLowerCase().includes(q));
  });

  const col = COLUMNS.find((c) => c.key === tstate.sortKey) || COLUMNS[0];
  const dirMul = tstate.sortDir === "desc" ? -1 : 1;
  rows = rows.slice().sort((a, b) => dirMul * compareValues(col.get(a), col.get(b), col.type));

  return { rows, total };
}

// On mobile there are no column headers to click (card mode), so sort is a
// <select> + a direction toggle button instead.
function buildSortControl(tstate, onChange) {
  const sortSelect = el(
    "select",
    { class: "table-sort-select", "aria-label": "Sort worktrees by" },
    COLUMNS.map((c) => el("option", { value: c.key }, c.label))
  );
  sortSelect.value = tstate.sortKey;
  sortSelect.addEventListener("change", () => {
    tstate.sortKey = sortSelect.value;
    onChange();
  });

  const dirBtn = el(
    "button",
    { type: "button", class: "table-filter-toggle", "aria-label": "Toggle sort direction" },
    tstate.sortDir === "asc" ? "▲ ASC" : "▼ DESC"
  );
  dirBtn.addEventListener("click", () => {
    tstate.sortDir = tstate.sortDir === "asc" ? "desc" : "asc";
    onChange();
  });

  return { sortSelect, dirBtn };
}

function buildToolbar(tstate, onChange, data, isMobile) {
  const toolbar = el("div", { class: "table-toolbar" });

  const search = el("input", {
    class: "table-search",
    type: "search",
    placeholder: "Filter worktrees…",
    "aria-label": "Filter worktrees",
    value: tstate.query,
  });
  search.addEventListener("input", () => {
    tstate.query = search.value;
    onChange();
  });
  toolbar.appendChild(search);

  let sortSelect = null;
  let dirBtn = null;
  if (isMobile) {
    ({ sortSelect, dirBtn } = buildSortControl(tstate, onChange));
    toolbar.appendChild(sortSelect);
    toolbar.appendChild(dirBtn);
  }

  const dirtyBtn = el(
    "button",
    { type: "button", class: "table-filter-toggle" + (tstate.dirtyOnly ? " active" : ""), "aria-pressed": String(tstate.dirtyOnly) },
    "DIRTY ONLY"
  );
  dirtyBtn.addEventListener("click", () => {
    tstate.dirtyOnly = !tstate.dirtyOnly;
    onChange();
  });
  toolbar.appendChild(dirtyBtn);

  const rootWrap = el("div", { style: "display:flex; gap:5px;" });
  const rootBtns = [];
  for (const rf of ROOT_FILTERS) {
    const active = tstate.rootFilter === rf.key;
    const btn = el("button", { type: "button", class: "table-filter-toggle" + (active ? " active" : ""), "aria-pressed": String(active) }, rf.label);
    btn.addEventListener("click", () => {
      tstate.rootFilter = rf.key;
      onChange();
    });
    rootBtns.push(btn);
    rootWrap.appendChild(btn);
  }
  toolbar.appendChild(rootWrap);

  const hostSelect = el("select", { class: "table-filter-toggle", "aria-label": "Filter by host" }, [
    el("option", { value: "all" }, "ALL HOSTS"),
  ]);
  hostSelect.addEventListener("change", () => {
    tstate.hostFilter = hostSelect.value;
    onChange();
  });
  toolbar.appendChild(hostSelect);
  syncHostOptions(hostSelect, tstate, data);

  const countEl = el("span", { class: "table-count" }, "");
  const clearBtn = el("button", { type: "button", class: "table-clear-btn" }, "clear");
  clearBtn.addEventListener("click", () => {
    tstate.query = "";
    tstate.dirtyOnly = false;
    tstate.rootFilter = "all";
    tstate.hostFilter = "all";
    search.value = "";
    hostSelect.value = "all";
    onChange();
  });
  toolbar.appendChild(countEl);
  toolbar.appendChild(clearBtn);

  return { toolbar, search, countEl, dirtyBtn, rootBtns, hostSelect, sortSelect, dirBtn };
}

// Rebuilds the <option> list from current data without disturbing the
// current selection (unless that host has disappeared from the fleet).
function syncHostOptions(hostSelect, tstate, data) {
  const hosts = uniqueHosts(data);
  const current = tstate.hostFilter || "all";
  hostSelect.innerHTML = "";
  hostSelect.appendChild(el("option", { value: "all" }, "ALL HOSTS"));
  for (const h of hosts) {
    hostSelect.appendChild(el("option", { value: h }, h.toUpperCase()));
  }
  hostSelect.value = current === "all" || hosts.includes(current) ? current : "all";
  if (hostSelect.value !== current) tstate.hostFilter = hostSelect.value;
}

function buildHeaderRow(tstate, onChange) {
  const tr = el("tr", {});
  for (const c of COLUMNS) {
    const active = tstate.sortKey === c.key;
    const th = el(
      "th",
      {
        class: `sortable${c.num ? " num" : ""}`,
        tabindex: "0",
        role: "button",
        "aria-sort": active ? (tstate.sortDir === "asc" ? "ascending" : "descending") : "none",
      },
      [c.label, active ? el("span", { class: "sort-indicator" }, tstate.sortDir === "asc" ? "▲" : "▼") : null]
    );
    const activate = () => {
      if (tstate.sortKey === c.key) {
        tstate.sortDir = tstate.sortDir === "asc" ? "desc" : "asc";
      } else {
        tstate.sortKey = c.key;
        tstate.sortDir = c.num ? "desc" : "asc";
      }
      onChange();
    };
    th.addEventListener("click", activate);
    th.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        activate();
      }
    });
    tr.appendChild(th);
  }
  return tr;
}

function buildRow(w, data) {
  const dirtyTotal = (w.dirty ?? 0) + (w.untracked ?? 0) + (w.staged ?? 0);
  const dirtyColor = dirtyTotal > 5 ? "var(--color-status-crit)" : dirtyTotal > 0 ? "var(--color-status-warn)" : "var(--color-text-faint)";
  const staleColor = (w.stale_days ?? 0) > 14 ? "var(--color-status-crit)" : (w.stale_days ?? 0) > 3 ? "var(--color-status-warn)" : "var(--color-text-faint)";

  const tr = el("tr", { class: "clickable-row", tabindex: "0", role: "button", "aria-label": `Open detail for ${w.repo}`, title: w.path }, [
    el("td", { style: "display:flex; align-items:center; gap:6px; min-width:0; overflow:hidden;" }, [
      el("span", { class: "mono truncate", style: "flex:1; min-width:0;" }, w.repo),
      el("span", { class: "pill mono", style: "font-size:9px; padding:1px 6px; flex:none;" }, shortRoot(w.root)),
    ]),
    el("td", { class: "truncate dim mono", style: "font-size:10px;" }, w.host || "-"),
    el("td", { class: "truncate dim mono", style: "font-size:10px;" }, w.branch || "-"),
    el("td", { class: "num mono", style: `color:${dirtyColor};` }, String(dirtyTotal)),
    el("td", { class: "num mono faint" }, `${w.ahead ?? 0}/${w.behind ?? 0}`),
    el("td", { class: "truncate", title: w.last_commit_msg || "" }, [
      el("span", { class: "mono", style: "font-size:10px;" }, fmtRelTime(w.last_commit_at)),
      el("span", { class: "dim", style: "margin-left:6px; font-size:10px;" }, w.last_commit_msg || ""),
    ]),
    el("td", { class: "num mono" }, String((w.agents || []).length)),
    el("td", { class: "num mono", style: `color:${staleColor};` }, `${w.stale_days ?? 0}d`),
  ]);
  tr.addEventListener("click", () => openWorktreeModal(w, data, tr));
  tr.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openWorktreeModal(w, data, tr);
    }
  });
  return tr;
}

// Stacked-card layout for mobile: one card per worktree, repo as the
// heading, everything else a label:value pair.
function buildCard(w, data) {
  const dirtyTotal = (w.dirty ?? 0) + (w.untracked ?? 0) + (w.staged ?? 0);
  const dirtyColor = dirtyTotal > 5 ? "var(--color-status-crit)" : dirtyTotal > 0 ? "var(--color-status-warn)" : "var(--color-text-faint)";
  const staleColor = (w.stale_days ?? 0) > 14 ? "var(--color-status-crit)" : (w.stale_days ?? 0) > 3 ? "var(--color-status-warn)" : "var(--color-text-faint)";

  const card = el("div", {
    class: "table-card clickable-card",
    tabindex: "0",
    role: "button",
    "aria-label": `Open detail for ${w.repo}`,
    title: w.path,
  });
  card.addEventListener("click", () => openWorktreeModal(w, data, card));
  card.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openWorktreeModal(w, data, card);
    }
  });

  card.appendChild(
    el("div", { class: "table-card-heading" }, [
      el("span", { class: "truncate" }, w.repo),
      el("span", { class: "pill mono", style: "font-size:9px; padding:1px 6px; flex:none;" }, shortRoot(w.root)),
    ])
  );

  const fields = el("div", { class: "table-card-fields" });
  const fieldRows = [
    ["HOST", w.host || "-"],
    ["BRANCH", w.branch || "-"],
    ["DIRTY", [String(dirtyTotal), dirtyColor]],
    ["AHEAD/BEHIND", `${w.ahead ?? 0}/${w.behind ?? 0}`],
    ["LAST COMMIT", `${fmtRelTime(w.last_commit_at)}${w.last_commit_msg ? " — " + w.last_commit_msg : ""}`],
    ["AGENTS", String((w.agents || []).length)],
    ["STALE", [`${w.stale_days ?? 0}d`, staleColor]],
  ];
  for (const [label, value] of fieldRows) {
    const [text, color] = Array.isArray(value) ? value : [value, null];
    fields.appendChild(
      el("div", { class: "table-card-field" }, [
        el("span", { class: "table-card-field-label" }, label),
        el("span", { class: "table-card-field-value mono", style: color ? `color:${color};` : "" }, text),
      ])
    );
  }
  card.appendChild(fields);

  return card;
}

// On mobile, cards render capped to `ui.mobileLimit` with a "Show all (N)"
// expander -- the page is the only scroll surface there, so an unbounded
// filtered result set can't carry its own inner scroll the way the desktop
// table does.
function renderRowsAndCount(ui, data, tstate) {
  const { rows, total } = computeRows(data, tstate);
  ui.countEl.textContent = `showing ${rows.length} of ${total}`;

  if (ui.mode === "cards") {
    ui.listHost.innerHTML = "";
    if (rows.length === 0) {
      ui.listHost.appendChild(el("div", { class: "empty-state" }, "No matching worktrees."));
      return;
    }
    renderWithShowAll(ui.listHost, rows, ui.mobileLimit, (list) => {
      const wrap = el("div", { class: "table-cards" });
      for (const w of list) wrap.appendChild(buildCard(w, data));
      return wrap;
    });
    return;
  }

  ui.tbody.innerHTML = "";
  if (rows.length === 0) {
    ui.tbody.appendChild(el("tr", {}, el("td", { colspan: String(COLUMNS.length), class: "empty-state" }, "No matching worktrees.")));
    return;
  }
  for (const w of rows) ui.tbody.appendChild(buildRow(w, data));
}

function fullBuild(container, ctx) {
  container.classList.add("table-panel-body");
  container.innerHTML = "";

  const panelId = ctx.panel.id;
  const persisted = loadJSON(storageKey(panelId), null);
  const tstate = persisted && typeof persisted === "object" ? { ...defaultStateFor(ctx.options), ...persisted } : defaultStateFor(ctx.options);

  const items = Array.isArray(ctx.data?.worktrees) ? ctx.data.worktrees : [];
  if (items.length === 0) {
    container.appendChild(el("div", { class: "empty-state" }, "No worktrees found."));
    container.__worktreeTableUI = null;
    return;
  }

  const isMobile = ctx.breakpoint === "mobile";
  const persist = () => saveJSON(storageKey(panelId), tstate);

  const onToolbarChange = () => {
    persist();
    rebuildToolbarActiveStates();
    renderRowsAndCount(ui, ctx.data, tstate);
  };

  const { toolbar, countEl, dirtyBtn, rootBtns, hostSelect, sortSelect, dirBtn } = buildToolbar(tstate, onToolbarChange, ctx.data, isMobile);
  container.appendChild(toolbar);

  function rebuildToolbarActiveStates() {
    dirtyBtn.classList.toggle("active", tstate.dirtyOnly);
    dirtyBtn.setAttribute("aria-pressed", String(tstate.dirtyOnly));
    ROOT_FILTERS.forEach((rf, i) => {
      const active = tstate.rootFilter === rf.key;
      rootBtns[i].classList.toggle("active", active);
      rootBtns[i].setAttribute("aria-pressed", String(active));
    });
    syncHostOptions(hostSelect, tstate, ctx.data);
    if (sortSelect) sortSelect.value = tstate.sortKey;
    if (dirBtn) dirBtn.textContent = tstate.sortDir === "asc" ? "▲ ASC" : "▼ DESC";
  }

  let ui;
  const scrollWrap = el("div", { class: "table-scroll" });

  if (isMobile) {
    const listHost = el("div", {});
    scrollWrap.appendChild(listHost);
    container.appendChild(scrollWrap);
    ui = { toolbar, countEl, listHost, hostSelect, mode: "cards", mobileLimit: mobileLimit(ctx.panel, 15) };
    renderRowsAndCount(ui, ctx.data, tstate);
  } else {
    const table = el("table", { class: "dtable" });
    const thead = el("thead");
    const tbody = el("tbody");
    table.appendChild(thead);
    table.appendChild(tbody);
    scrollWrap.appendChild(table);
    container.appendChild(scrollWrap);

    ui = { toolbar, countEl, thead, tbody, hostSelect, mode: "table" };

    const rebuildHeader = () => {
      thead.innerHTML = "";
      thead.appendChild(buildHeaderRow(tstate, onHeaderChange));
    };
    function onHeaderChange() {
      persist();
      rebuildHeader();
      renderRowsAndCount(ui, ctx.data, tstate);
    }
    rebuildHeader();
    renderRowsAndCount(ui, ctx.data, tstate);
  }

  container.__worktreeTableUI = { ui, tstate, breakpoint: ctx.breakpoint };
}

export default {
  title: "Worktrees",
  minW: 4,
  minH: 3,
  render(container, ctx) {
    fullBuild(container, ctx);
  },
  update(container, ctx) {
    const stored = container.__worktreeTableUI;
    const items = Array.isArray(ctx.data?.worktrees) ? ctx.data.worktrees : [];
    if (!stored || items.length === 0 || stored.breakpoint !== ctx.breakpoint) {
      fullBuild(container, ctx);
      return;
    }
    syncHostOptions(stored.ui.hostSelect, stored.tstate, ctx.data);
    renderRowsAndCount(stored.ui, ctx.data, stored.tstate);
  },
};
