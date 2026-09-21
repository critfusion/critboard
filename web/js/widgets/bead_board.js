import { el, fmtDuration, priorityColorVar, mobileLimit, renderWithShowAll, sourceIssueNotice } from "../utils.js";
import { openBeadModal } from "../detail.js";
import {
  classifyBeadRouting,
  routingTooltip,
  buildDispatchPausedBanner,
  truncateLabel,
  ROUTING_LABELS,
  ROUTING_PILL_CLASS,
  DEFAULT_HUMAN_LABELS,
} from "../bead_routing.js";

const LANE_LABELS = { ready: "READY", in_progress: "IN PROGRESS", blocked: "BLOCKED", review: "REVIEW" };

function beadCard(bead, data, humanLabels) {
  const card = el("div", {
    class: "clickable-card",
    tabindex: "0",
    role: "button",
    "aria-label": `Open detail for ${bead.title || bead.id}`,
    style: [
      "background:var(--color-bg-elevated);",
      "border:1px solid var(--color-border);",
      `border-left:3px solid var(${priorityColorVar(bead.priority)});`,
      "border-radius:var(--radius-card);",
      "padding:7px 9px;",
      "display:flex; flex-direction:column; gap:4px;",
    ].join(""),
  });
  card.addEventListener("click", () => openBeadModal(bead, data, card));
  card.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openBeadModal(bead, data, card);
    }
  });

  card.appendChild(el("div", { class: "mono truncate", style: "font-size:11px; font-weight:600;" }, bead.title || bead.id));
  card.appendChild(el("div", { class: "faint mono truncate", style: "font-size:9px;" }, bead.id));

  const metaRow = el("div", { style: "display:flex; justify-content:space-between; font-size:9px;" }, [
    el("span", { class: "dim" }, bead.assignee || "unassigned"),
    el("span", { class: "faint mono" }, fmtDuration(bead.age_s)),
  ]);
  card.appendChild(metaRow);

  // Routability: does any label on this card map to a live dispatch route?
  // Same three-state classification as the bead queue table -- see
  // bead_routing.js for why "unclaimed and old" isn't the same question as
  // "will anything ever pick this up".
  const cls = classifyBeadRouting(bead, data?.dispatch, humanLabels);
  card.appendChild(
    el(
      "span",
      {
        class: `pill ${ROUTING_PILL_CLASS[cls.state]}`,
        style: "align-self:flex-start; max-width:100%; overflow:hidden; text-overflow:ellipsis; font-size:8px; padding:1px 6px;",
        title: routingTooltip(cls),
      },
      [el("span", { class: "pill-dot" }), `${ROUTING_LABELS[cls.state]}${cls.label ? ` →${truncateLabel(cls.label)}` : ""}`]
    )
  );

  if (bead.labels && bead.labels.length) {
    const labels = el("div", { style: "display:flex; flex-wrap:wrap; gap:3px;" });
    for (const l of bead.labels.slice(0, 4)) {
      labels.appendChild(el("span", { class: "pill", style: "font-size:8px; padding:1px 6px;" }, l));
    }
    card.appendChild(labels);
  }

  if (bead.blocked_by && bead.blocked_by.length) {
    card.appendChild(
      el("div", { class: "pill pill-crit", style: "font-size:8px; align-self:flex-start;" },
        [el("span", { class: "pill-dot" }), `blocked by ${bead.blocked_by.length}`])
    );
  }

  return card;
}

// Selected-lane-per-panel is remembered for the browser session (not across
// browser restarts) -- sessionStorage, wrapped since a private window or
// blocked storage should degrade to "just use the first lane" instead of
// breaking the board.
function tabStorageKey(panelId) {
  return `critdash.board.${panelId}.tab`;
}
function loadActiveTab(panelId, laneOrder) {
  try {
    const v = window.sessionStorage.getItem(tabStorageKey(panelId));
    if (v && laneOrder.includes(v)) return v;
  } catch (e) {
    // ignore -- private window or storage blocked
  }
  return laneOrder[0];
}
function saveActiveTab(panelId, lane) {
  try {
    window.sessionStorage.setItem(tabStorageKey(panelId), lane);
  } catch (e) {
    // ignore
  }
}

function beadsForLane(lane, lanes, byId) {
  const ids = lanes[lane] || [];
  return ids.map((id) => byId.get(id)).filter(Boolean);
}

function cardsList(beads, data, humanLabels) {
  const cardsWrap = el("div", { style: "display:flex; flex-direction:column; gap:6px;" });
  for (const b of beads) cardsWrap.appendChild(beadCard(b, data, humanLabels));
  return cardsWrap;
}

function laneCards(lane, lanes, byId, data, humanLabels) {
  const beads = beadsForLane(lane, lanes, byId);
  if (beads.length === 0) {
    return el("div", { class: "empty-state", style: "padding:10px 4px;" }, "empty");
  }
  return cardsList(beads, data, humanLabels);
}

// Four side-by-side lanes cannot work at phone width -- one column of cards
// a few characters wide isn't readable. Mobile gets one lane at a time
// (full width) behind a tab bar, count-in-label, remembered per session.
// Each lane is capped to `limit` cards with a "Show all (N)" expander --
// the page is the only scroll surface on mobile, so an unbounded lane can't
// carry its own inner scroll the way it does on desktop/tablet.
function renderTabbed(container, laneOrder, lanes, byId, data, panelId, limit, humanLabels) {
  const wrap = el("div", { style: "display:flex; flex-direction:column;" });
  const banner = buildDispatchPausedBanner(data?.dispatch);
  if (banner) container.appendChild(banner);
  const tabs = el("div", { class: "kanban-tabs", role: "tablist" });
  const paneHost = el("div", {});
  wrap.appendChild(tabs);
  wrap.appendChild(paneHost);
  container.appendChild(wrap);

  let active = loadActiveTab(panelId, laneOrder);

  function renderTabsBar() {
    tabs.innerHTML = "";
    for (const lane of laneOrder) {
      const count = (lanes[lane] || []).length;
      const isActive = lane === active;
      const btn = el(
        "button",
        {
          type: "button",
          class: "kanban-tab" + (isActive ? " active" : ""),
          role: "tab",
          "aria-selected": String(isActive),
        },
        [document.createTextNode(LANE_LABELS[lane] || lane.toUpperCase()), el("span", { class: "kanban-tab-count" }, String(count))]
      );
      btn.addEventListener("click", () => {
        if (active === lane) return;
        active = lane;
        saveActiveTab(panelId, active);
        renderTabsBar();
        renderPane();
      });
      tabs.appendChild(btn);
    }
  }

  function renderPane() {
    paneHost.innerHTML = "";
    const beads = beadsForLane(active, lanes, byId);
    if (beads.length === 0) {
      paneHost.appendChild(el("div", { class: "empty-state", style: "padding:10px 4px;" }, "empty"));
      return;
    }
    renderWithShowAll(paneHost, beads, limit, (list) => cardsList(list, data, humanLabels));
  }

  renderTabsBar();
  renderPane();
}

function renderColumns(container, laneOrder, lanes, byId, data, humanLabels) {
  const banner = buildDispatchPausedBanner(data?.dispatch);
  if (banner) container.appendChild(banner);

  const board = el("div", {
    style: `display:grid; grid-template-columns:repeat(${laneOrder.length}, minmax(0,1fr)); gap:10px; height:100%;`,
  });

  for (const lane of laneOrder) {
    const col = el("div", { style: "display:flex; flex-direction:column; min-width:0;" });
    col.appendChild(
      el("div", { style: "display:flex; justify-content:space-between; margin-bottom:6px;" }, [
        el("span", { class: "mono", style: "font-size:10px; letter-spacing:.06em; color:var(--color-text-dim);" }, LANE_LABELS[lane] || lane.toUpperCase()),
        el("span", { class: "mono faint", style: "font-size:10px;" }, String((lanes[lane] || []).length)),
      ])
    );
    const cardsWrap = laneCards(lane, lanes, byId, data, humanLabels);
    cardsWrap.style.overflow = "auto";
    col.appendChild(cardsWrap);
    board.appendChild(col);
  }

  container.appendChild(board);
}

export default {
  title: "Beads",
  minW: 4,
  minH: 3,
  render(container, { data, options, panel, breakpoint, layout }) {
    const items = Array.isArray(data?.beads?.items) ? data.beads.items : [];
    const lanes = data?.beads?.lanes || {};
    const laneOrder = options?.lanes || ["ready", "in_progress", "blocked", "review"];
    const byId = new Map(items.map((b) => [b.id, b]));
    const humanLabels = layout?.human_labels || DEFAULT_HUMAN_LABELS;

    // No beads at all, and the collector says why -- show one explanation
    // instead of four empty lanes each silently saying "empty".
    if (items.length === 0) {
      const issue = sourceIssueNotice(data?.sources, "beads", "Beads");
      if (issue) {
        container.appendChild(issue);
        return;
      }
    }

    if (breakpoint === "mobile") {
      const limit = mobileLimit(panel, 12);
      renderTabbed(container, laneOrder, lanes, byId, data, panel.id, limit, humanLabels);
    } else {
      renderColumns(container, laneOrder, lanes, byId, data, humanLabels);
    }
  },
};
