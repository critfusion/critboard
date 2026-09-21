import { el, fmtTokens, fmtCost, fmtRelTime, fmtDuration, escapeHtml } from "../utils.js";
import { openAgentModal } from "../detail.js";

const KIND_LABEL = { claude: "CLAUDE", codex: "CODEX", grok: "GROK" };
const STATUS_ORDER = { working: 0, idle: 1, done: 2, unknown: 3 };

function agentCard(a, data) {
  const since = a.status_since ? (Date.now() - new Date(a.status_since).getTime()) / 1000 : null;
  const card = el("div", {
    class: "agent-card clickable-card" + (a.status === "working" ? " agent-glow" : ""),
    tabindex: "0",
    role: "button",
    "aria-label": `Open detail for ${a.label || a.title || a.id}`,
    style: [
      "border:1px solid var(--color-border);",
      "border-radius:var(--radius-card);",
      "padding:10px;",
      "display:flex;",
      "flex-direction:column;",
      "gap:6px;",
      "background:var(--color-bg-elevated);",
      "min-width:0;",
    ].join(""),
  });
  card.addEventListener("click", () => openAgentModal(a, data, card));
  card.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openAgentModal(a, data, card);
    }
  });

  const heading = a.label || a.title || a.repo || a.id;
  const head = el("div", { style: "display:flex; align-items:center; gap:7px; min-width:0;", title: a.title || "" }, [
    el("span", { class: `status-dot ${a.status}` }),
    el("span", { class: "mono truncate", style: "font-weight:700; flex:1; min-width:0;" }, heading),
    a.host ? el("span", { class: "pill mono", style: "font-size:9px; padding:1px 6px;" }, a.host) : null,
    el("span", { class: "pill mono", style: "font-size:9px; padding:1px 6px;" }, KIND_LABEL[a.kind] || a.kind),
  ]);

  const cwdRow = el("div", { class: "faint mono truncate", style: "font-size:10px;" }, a.cwd_short || a.cwd || "-");

  const sub = el("div", { class: "dim mono truncate", style: "font-size:10px;" },
    `${a.repo || "-"}${a.branch ? " @ " + a.branch : ""}`);

  const beadRow = el("div", { class: "dim mono truncate", style: "font-size:10px;" },
    a.bead ? `bead ${a.bead}` : "no active bead");

  const statusRow = el("div", { style: "display:flex; justify-content:space-between; font-size:10px;" }, [
    el("span", { class: "dim" }, (a.status || "unknown").toUpperCase()),
    el("span", { class: "faint mono" }, since !== null ? fmtDuration(since) : "--"),
  ]);

  const metrics = el("div", { style: "display:flex; justify-content:space-between; align-items:baseline; margin-top:2px;" }, [
    el("span", { class: "mono tabular-nums", style: "font-size:12px;" }, fmtTokens(a.tokens_today?.total ?? 0) + " tok"),
    el("span", { class: "mono tabular-nums", style: "font-size:12px; color:var(--color-accent-amber);" }, fmtCost(a.cost_today_usd ?? 0)),
  ]);

  const footer = el("div", { style: "display:flex; justify-content:space-between; font-size:9px;" }, [
    el("span", { class: "faint mono" }, a.model || "-"),
    el("span", { class: "faint mono" }, `${a.subagents_active ?? 0} sub · ${fmtRelTime(a.last_activity)}`),
  ]);

  card.appendChild(head);
  card.appendChild(cwdRow);
  card.appendChild(sub);
  card.appendChild(beadRow);
  card.appendChild(statusRow);
  card.appendChild(metrics);
  card.appendChild(footer);
  return card;
}

export default {
  title: "Fleet",
  minW: 3,
  minH: 2,
  render(container, { data, options }) {
    const agents = Array.isArray(data?.agents) ? data.agents.slice() : [];
    const showIdle = options?.show_idle !== false;
    const sort = options?.sort || "status";

    let list = showIdle ? agents : agents.filter((a) => a.status !== "idle");

    if (sort === "status") {
      list.sort((a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) || (a.repo || "").localeCompare(b.repo || ""));
    } else if (sort === "cost") {
      list.sort((a, b) => (b.cost_today_usd ?? 0) - (a.cost_today_usd ?? 0));
    }

    if (list.length === 0) {
      container.appendChild(el("div", { class: "empty-state" }, "No agents reporting."));
      return;
    }

    const grid = el("div", {
      style: "display:grid; grid-template-columns:repeat(auto-fill, minmax(210px, 1fr)); gap:8px;",
    });
    for (const a of list) grid.appendChild(agentCard(a, data));
    container.appendChild(grid);
  },
};
