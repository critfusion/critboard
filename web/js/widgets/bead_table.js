import {
  el,
  fmtDuration,
  priorityColorVar,
  loadJSON,
  saveJSON,
  mobileLimit,
  renderWithShowAll,
  sourceIssueNotice,
} from "../utils.js";
import { openBeadModal } from "../detail.js";
import {
  classifyBeadRouting,
  idleReason,
  routingTooltip,
  buildDispatchPausedBanner,
  truncateLabel,
  ROUTING_LABELS,
  ROUTING_PILL_CLASS,
  DEFAULT_HUMAN_LABELS,
} from "../bead_routing.js";

const STATUS_PILL = { open: "pill-idle", ready: "pill-idle", in_progress: "pill-ok", blocked: "pill-crit", review: "pill-warn", closed: "pill-idle" };

const QUICK_FILTERS = [
  { key: "ready", label: "READY" },
  { key: "in_progress", label: "IN PROGRESS" },
  { key: "blocked", label: "BLOCKED" },
  { key: "review", label: "REVIEW" },
  { key: "closed", label: "CLOSED" },
];

// Routability toggles: an OR-together group, same mechanics as QUICK_FILTERS
// above but answering a different question ("will this ever wake an agent"
// rather than "what lane is it in"), so it gets its own group + wrapper
// rather than being folded into QUICK_FILTERS.
const ROUTING_FILTERS = [
  { key: "routable", label: ROUTING_LABELS.routable },
  { key: "owner", label: ROUTING_LABELS.owner },
  { key: "unroutable", label: ROUTING_LABELS.unroutable },
];

const COLUMNS = [
  { key: "id", label: "ID", type: "text", num: false, get: (b) => b.id },
  { key: "title", label: "TITLE", type: "text", num: false, get: (b) => b.title || "" },
  { key: "status", label: "STATUS", type: "text", num: false, get: (b) => b.status || "" },
  { key: "priority", label: "PRI", type: "number", num: true, get: (b) => (b.priority === null || b.priority === undefined ? null : Number(b.priority)) },
  { key: "assignee", label: "ASSIGNEE", type: "text", num: false, get: (b) => b.assignee || "" },
  { key: "routing", label: "ROUTE", type: "text", num: false, get: (b, data, humanLabels) => classifyBeadRouting(b, data?.dispatch, humanLabels).state },
  { key: "repo", label: "REPO", type: "text", num: false, get: (b) => b.repo || "" },
  { key: "age", label: "AGE", type: "number", num: true, get: (b) => (b.age_s === null || b.age_s === undefined ? null : Number(b.age_s)) },
];

const SEARCH_FIELDS = ["id", "title", "status", "assignee", "repo"];

function storageKey(panelId) {
  return `critdash.table.${panelId}`;
}

function defaultStateFor(options) {
  const legacy = options?.sort;
  let sortKey = "priority";
  let sortDir = "asc";
  if (legacy === "age") {
    sortKey = "age";
    sortDir = "desc";
  } else if (COLUMNS.some((c) => c.key === legacy)) {
    sortKey = legacy;
  }
  // Closed beads are hidden by default. `options.show_closed: true` in
  // config/layout.json can flip the default for a given panel; the code
  // default (no config key) matches the requested behaviour: hidden.
  const showClosed = options?.show_closed === true;
  return { sortKey, sortDir, query: "", statusFilters: [], showClosed, routingFilters: [] };
}

function beadInLane(bead, laneKey, lanes) {
  if (laneKey === "closed") return bead.status === "closed";
  const ids = lanes?.[laneKey];
  return Array.isArray(ids) && ids.includes(bead.id);
}

function compareValues(a, b, type) {
  if (a === null || a === undefined) return b === null || b === undefined ? 0 : -1;
  if (b === null || b === undefined) return 1;
  if (type === "number") return a - b;
  return String(a).toLowerCase().localeCompare(String(b).toLowerCase());
}

function computeRows(data, options, tstate, humanLabels) {
  const items = Array.isArray(data?.beads?.items) ? data.beads.items : [];
  const lanes = data?.beads?.lanes || {};
  const dispatch = data?.dispatch;
  // `total` (M in "showing N of M") is the full universe, closed included --
  // it's what the panel would show with SHOW CLOSED on, so it never implies
  // the queue is smaller than it is. `closedHidden` makes the SHOW CLOSED
  // exclusion visible in the count rather than silent.
  const total = items.length;
  const closedHidden = tstate.showClosed ? 0 : items.filter((b) => b.status === "closed").length;

  const q = tstate.query.trim().toLowerCase();
  let rows = items.filter((b) => {
    // SHOW CLOSED is an explicit exclusion gate, separate from the inclusive
    // lane pills: off means closed beads never show, even if a lane pill
    // (e.g. the CLOSED pill itself) would otherwise include them.
    if (!tstate.showClosed && b.status === "closed") return false;
    if (tstate.statusFilters.length > 0 && !tstate.statusFilters.some((f) => beadInLane(b, f, lanes))) return false;
    if (tstate.routingFilters.length > 0 && !tstate.routingFilters.includes(classifyBeadRouting(b, dispatch, humanLabels).state)) return false;
    if (!q) return true;
    return SEARCH_FIELDS.some((f) => String(b[f] || "").toLowerCase().includes(q));
  });

  const col = COLUMNS.find((c) => c.key === tstate.sortKey) || COLUMNS[3];
  const dirMul = tstate.sortDir === "desc" ? -1 : 1;
  rows = rows.slice().sort((a, b) => dirMul * compareValues(col.get(a, data, humanLabels), col.get(b, data, humanLabels), col.type));

  return { rows, total, closedHidden };
}

// On mobile there are no column headers to click (card mode), so sort is a
// <select> + a direction toggle button instead.
function buildSortControl(tstate, onChange) {
  const sortSelect = el(
    "select",
    { class: "table-sort-select", "aria-label": "Sort beads by" },
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

function buildToolbar(container, panelId, tstate, onChange, isMobile, options) {
  const toolbar = el("div", { class: "table-toolbar" });

  const search = el("input", {
    class: "table-search",
    type: "search",
    placeholder: "Filter beads…",
    "aria-label": "Filter beads",
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

  const toggles = el("div", { style: "display:flex; flex-wrap:wrap; gap:5px;" });
  for (const qf of QUICK_FILTERS) {
    const active = tstate.statusFilters.includes(qf.key);
    const btn = el(
      "button",
      { type: "button", class: "table-filter-toggle" + (active ? " active" : ""), "data-qf": qf.key, "aria-pressed": String(active) },
      qf.label
    );
    btn.addEventListener("click", () => {
      const idx = tstate.statusFilters.indexOf(qf.key);
      if (idx === -1) tstate.statusFilters.push(qf.key);
      else tstate.statusFilters.splice(idx, 1);
      onChange();
    });
    toggles.appendChild(btn);
  }
  toolbar.appendChild(toggles);

  // Explicit exclusion gate for closed beads -- deliberately NOT part of the
  // `toggles` group above. The lane pills are inclusive (OR-together which
  // lanes to show); this is a separate "hide closed no matter what" switch,
  // so it gets its own wrapper with a divider and a distinct (amber, not
  // cyan) active style to avoid reading as a fifth lane filter.
  const showClosedWrap = el("div", { class: "table-show-closed" });
  const showClosedBtn = el(
    "button",
    {
      type: "button",
      class: "table-show-closed-toggle" + (tstate.showClosed ? " active" : ""),
      "aria-pressed": String(tstate.showClosed),
      title: "Closed beads are hidden by default; toggle to include them",
    },
    "SHOW CLOSED"
  );
  showClosedBtn.addEventListener("click", () => {
    tstate.showClosed = !tstate.showClosed;
    onChange();
  });
  showClosedWrap.appendChild(showClosedBtn);
  toolbar.appendChild(showClosedWrap);

  // Routability filter: answers "will this ever wake an agent", not "what
  // lane is it in" -- a separate question from the QUICK_FILTERS lane pills
  // above, so it gets its own wrapper + divider (same visual pattern as
  // SHOW CLOSED) rather than being mixed into that group. This is what lets
  // Bryan list the unroutable beads directly.
  const routingWrap = el("div", { class: "table-routing-filter" });
  const routingBtns = {};
  for (const rf of ROUTING_FILTERS) {
    const active = tstate.routingFilters.includes(rf.key);
    const btn = el(
      "button",
      {
        type: "button",
        class: "table-filter-toggle routing-toggle" + (active ? " active" : ""),
        "data-routing": rf.key,
        "aria-pressed": String(active),
        title: rf.key === "unroutable" ? "No label maps to any dispatch route -- nobody will ever be woken for these." : "",
      },
      rf.label
    );
    btn.addEventListener("click", () => {
      const idx = tstate.routingFilters.indexOf(rf.key);
      if (idx === -1) tstate.routingFilters.push(rf.key);
      else tstate.routingFilters.splice(idx, 1);
      onChange();
    });
    routingBtns[rf.key] = btn;
    routingWrap.appendChild(btn);
  }
  toolbar.appendChild(routingWrap);

  const countEl = el("span", { class: "table-count" }, "");
  const clearBtn = el("button", { type: "button", class: "table-clear-btn" }, "clear");
  clearBtn.addEventListener("click", () => {
    tstate.query = "";
    tstate.statusFilters = [];
    tstate.routingFilters = [];
    tstate.showClosed = defaultStateFor(options).showClosed;
    search.value = "";
    onChange();
  });
  toolbar.appendChild(countEl);
  toolbar.appendChild(clearBtn);

  container.appendChild(toolbar);
  return { toolbar, search, countEl, sortSelect, dirBtn, showClosedBtn, routingBtns };
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

// Routability pill shared by the desktop cell and the mobile card field --
// three visually distinct states (color + label), always with a tooltip
// explaining the "why", since that's the whole point of this column.
// `withLabel`: the mobile card field has room for "→route-name"; the fixed
// -width desktop table column (8 equal columns) does not -- there the state
// alone plus the tooltip is what fits without spilling into the next cell.
function routingPill(b, dispatch, humanLabels, withLabel = false) {
  const cls = classifyBeadRouting(b, dispatch, humanLabels);
  const suffix = withLabel && cls.label ? ` →${truncateLabel(cls.label)}` : "";
  return el("span", { class: `pill ${ROUTING_PILL_CLASS[cls.state]}`, title: routingTooltip(cls) }, [
    el("span", { class: "pill-dot" }),
    `${ROUTING_LABELS[cls.state]}${suffix}`,
  ]);
}

// AGE cell/field content: a plain duration normally, plus a small
// warn-colored reason marker when the bead is sitting idle for a reason
// that isn't "it just hasn't been picked up yet" -- an unroutable label or
// a paused dispatcher. Never marks a human-owned or already-claimed bead
// this way (see idleReason()).
function ageContent(b, dispatch, humanLabels) {
  const reason = idleReason(b, dispatch, humanLabels);
  const text = document.createTextNode(fmtDuration(b.age_s));
  if (!reason) return [text];
  return [
    text,
    el(
      "span",
      {
        class: "age-idle-flag",
        style: "margin-left:4px; color:var(--color-status-warn);",
        title: `Idle because ${reason.text} -- ${reason.detail}`,
      },
      reason.key === "paused" ? "⏸" : "⚠"
    ),
  ];
}

function buildRow(b, data, humanLabels) {
  const dispatch = data?.dispatch;
  const priDot = el("span", { class: "badge-priority", style: `background:var(${priorityColorVar(b.priority)}); margin-right:5px;` });
  const tr = el(
    "tr",
    { class: "clickable-row", tabindex: "0", role: "button", "aria-label": `Open detail for ${b.title || b.id}`, title: "Click for detail" },
    [
      el("td", { class: "mono faint truncate" }, b.id.split("-").slice(-1)[0]),
      el("td", { class: "truncate" }, [
        b.blocked_by && b.blocked_by.length
          ? el("span", { class: "mono", style: "color:var(--color-status-crit); margin-right:4px;", title: `blocked by ${b.blocked_by.join(", ")}` }, "⛔")
          : null,
        document.createTextNode(b.title || ""),
      ]),
      el("td", {}, el("span", { class: `pill ${STATUS_PILL[b.status] || "pill-idle"}` }, [el("span", { class: "pill-dot" }), b.status])),
      el("td", { class: "num" }, [priDot, document.createTextNode(String(b.priority ?? "-"))]),
      el("td", { class: "truncate dim" }, b.assignee || "-"),
      el("td", { class: "truncate" }, routingPill(b, dispatch, humanLabels)),
      el("td", { class: "truncate dim mono", style: "font-size:10px;" }, b.repo || "-"),
      el("td", { class: "num mono faint" }, ageContent(b, dispatch, humanLabels)),
    ]
  );
  tr.addEventListener("click", () => openBeadModal(b, data, tr));
  tr.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openBeadModal(b, data, tr);
    }
  });
  return tr;
}

// Stacked-card layout for mobile: one card per bead, primary identifier
// (title) as the heading, everything else a label:value pair.
function buildCard(b, data, humanLabels) {
  const dispatch = data?.dispatch;
  const card = el("div", {
    class: "table-card clickable-card",
    tabindex: "0",
    role: "button",
    "aria-label": `Open detail for ${b.title || b.id}`,
    style: `border-left:3px solid var(${priorityColorVar(b.priority)});`,
  });
  card.addEventListener("click", () => openBeadModal(b, data, card));
  card.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openBeadModal(b, data, card);
    }
  });

  card.appendChild(
    el("div", { class: "table-card-heading" }, [
      el("span", { class: "truncate" }, b.title || b.id),
      el("span", { class: `pill ${STATUS_PILL[b.status] || "pill-idle"}`, style: "flex:none;" }, [
        el("span", { class: "pill-dot" }),
        b.status || "-",
      ]),
    ])
  );

  const fields = el("div", { class: "table-card-fields" });
  const fieldRows = [
    ["ID", b.id],
    ["PRIORITY", String(b.priority ?? "-")],
    ["ASSIGNEE", b.assignee || "-"],
    ["ROUTE", routingPill(b, dispatch, humanLabels, true)],
    ["REPO", b.repo || "-"],
    ["AGE", ageContent(b, dispatch, humanLabels)],
  ];
  if (b.blocked_by && b.blocked_by.length) fieldRows.push(["BLOCKED BY", `${b.blocked_by.length} bead(s)`]);
  for (const [label, value] of fieldRows) {
    fields.appendChild(
      el("div", { class: "table-card-field" }, [
        el("span", { class: "table-card-field-label" }, label),
        el("span", { class: "table-card-field-value mono" }, value),
      ])
    );
  }
  card.appendChild(fields);

  return card;
}

// On mobile, cards render capped to `ui.mobileLimit` with a "Show all (N)"
// expander -- the page is the only scroll surface there, so an unbounded
// filtered result set can't carry its own inner scroll the way the desktop
// table does. Expansion is local to a single render pass (like the other
// capped-list widgets): a fresh search/sort/SSE update recomputes rows and
// re-caps, which is fine since typing into the filter already changes what
// "all" means.
function renderRowsAndCount(ui, data, options, tstate, humanLabels) {
  const { rows, total, closedHidden } = computeRows(data, options, tstate, humanLabels);
  ui.countEl.textContent =
    closedHidden > 0 ? `showing ${rows.length} of ${total} (${closedHidden} closed hidden)` : `showing ${rows.length} of ${total}`;

  if (ui.banner) {
    ui.banner.innerHTML = "";
    const banner = buildDispatchPausedBanner(data?.dispatch);
    if (banner) ui.banner.appendChild(banner);
  }

  if (ui.mode === "cards") {
    ui.listHost.innerHTML = "";
    if (rows.length === 0) {
      ui.listHost.appendChild(el("div", { class: "empty-state" }, "No matching beads."));
      return;
    }
    renderWithShowAll(ui.listHost, rows, ui.mobileLimit, (list) => {
      const wrap = el("div", { class: "table-cards" });
      for (const b of list) wrap.appendChild(buildCard(b, data, humanLabels));
      return wrap;
    });
    return;
  }

  ui.tbody.innerHTML = "";
  if (rows.length === 0) {
    const tr = el("tr", {}, el("td", { colspan: String(COLUMNS.length), class: "empty-state" }, "No matching beads."));
    ui.tbody.appendChild(tr);
    return;
  }
  for (const b of rows) ui.tbody.appendChild(buildRow(b, data, humanLabels));
}

function fullBuild(container, ctx) {
  container.classList.add("table-panel-body");
  container.innerHTML = "";

  const panelId = ctx.panel.id;
  const persisted = loadJSON(storageKey(panelId), null);
  const base = defaultStateFor(ctx.options);
  const tstate = persisted && typeof persisted === "object" ? { ...base, ...persisted } : { ...base };
  if (!Array.isArray(tstate.statusFilters)) tstate.statusFilters = [];
  // Migrate state saved by pre-toggle code: it has no `showClosed` key at
  // all, so the spread above already left tstate.showClosed at the new
  // default (hidden, or the config override) -- that's what makes "absent
  // means off" reach an existing user on first refresh. The one exception:
  // if they had explicitly selected the CLOSED lane pill, they'd already
  // asked to see closed beads, so honor that instead of silently hiding them.
  if (persisted && typeof persisted === "object" && !("showClosed" in persisted) && tstate.statusFilters.includes("closed")) {
    tstate.showClosed = true;
  }

  const items = Array.isArray(ctx.data?.beads?.items) ? ctx.data.beads.items : [];
  if (items.length === 0) {
    const issue = sourceIssueNotice(ctx.data?.sources, "beads", "Beads");
    container.appendChild(issue || el("div", { class: "empty-state" }, "No beads."));
    container.__beadTableUI = null;
    return;
  }

  const humanLabels = ctx.layout?.human_labels || DEFAULT_HUMAN_LABELS;
  const isMobile = ctx.breakpoint === "mobile";
  const persist = () => saveJSON(storageKey(panelId), tstate);

  const onChange = () => {
    persist();
    rebuildToolbarActiveStates();
    renderRowsAndCount(ui, ctx.data, ctx.options, tstate, humanLabels);
  };

  // Banner goes above the toolbar -- top of the panel, unmissable, not part
  // of the filter chrome underneath it.
  const bannerHost = el("div", { class: "bead-panel-banner" });
  container.appendChild(bannerHost);

  const { toolbar, countEl, sortSelect, dirBtn, showClosedBtn, routingBtns } = buildToolbar(container, panelId, tstate, onChange, isMobile, ctx.options);

  function rebuildToolbarActiveStates() {
    toolbar.querySelectorAll(".table-filter-toggle[data-qf]").forEach((btn) => {
      const active = tstate.statusFilters.includes(btn.getAttribute("data-qf"));
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", String(active));
    });
    for (const [key, btn] of Object.entries(routingBtns)) {
      const active = tstate.routingFilters.includes(key);
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", String(active));
    }
    showClosedBtn.classList.toggle("active", tstate.showClosed);
    showClosedBtn.setAttribute("aria-pressed", String(tstate.showClosed));
    if (sortSelect) sortSelect.value = tstate.sortKey;
    if (dirBtn) dirBtn.textContent = tstate.sortDir === "asc" ? "▲ ASC" : "▼ DESC";
  }

  let ui;
  const scrollWrap = el("div", { class: "table-scroll" });

  if (isMobile) {
    const listHost = el("div", {});
    scrollWrap.appendChild(listHost);
    container.appendChild(scrollWrap);
    ui = { toolbar, countEl, listHost, mode: "cards", mobileLimit: mobileLimit(ctx.panel, 15), banner: bannerHost };
  } else {
    const table = el("table", { class: "dtable" });
    const thead = el("thead");
    const tbody = el("tbody");
    table.appendChild(thead);
    table.appendChild(tbody);
    scrollWrap.appendChild(table);
    container.appendChild(scrollWrap);
    ui = { toolbar, countEl, thead, tbody, mode: "table", banner: bannerHost };

    const rebuildHeader = () => {
      thead.innerHTML = "";
      thead.appendChild(buildHeaderRow(tstate, onHeaderChange));
    };
    function onHeaderChange() {
      persist();
      rebuildHeader();
      renderRowsAndCount(ui, ctx.data, ctx.options, tstate, humanLabels);
    }
    rebuildHeader();
  }

  container.__beadTableUI = { ui, tstate, ctxOptions: ctx.options, breakpoint: ctx.breakpoint, humanLabels };

  renderRowsAndCount(ui, ctx.data, ctx.options, tstate, humanLabels);
}

export default {
  title: "Bead queue",
  minW: 4,
  minH: 3,
  render(container, ctx) {
    fullBuild(container, ctx);
  },
  update(container, ctx) {
    const stored = container.__beadTableUI;
    if (!stored) {
      fullBuild(container, ctx);
      return;
    }
    const items = Array.isArray(ctx.data?.beads?.items) ? ctx.data.beads.items : [];
    if (items.length === 0 || stored.breakpoint !== ctx.breakpoint) {
      fullBuild(container, ctx);
      return;
    }
    stored.ctxOptions = ctx.options;
    renderRowsAndCount(stored.ui, ctx.data, ctx.options, stored.tstate, stored.humanLabels);
  },
};
