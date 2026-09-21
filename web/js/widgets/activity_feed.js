import { el, fmtRelTime, severityColorVar, mobileLimit, renderWithShowAll } from "../utils.js";
import { openActivityModal } from "../detail.js";

const KIND_ICON = {
  bead_created: "+",
  bead_status: "→",
  bead_closed: "✓",
  bead_claimed: "●",
  agent_status: "◆",
  commit: "⌘",
  dispatch: "↯",
  alert: "!",
};

function buildRow(ev, data) {
  const color = `var(${severityColorVar(ev.severity)})`;
  const row = el("div", {
    class: "clickable-row",
    tabindex: "0",
    role: "button",
    "aria-label": `Open detail for event: ${ev.text || ev.kind || "event"}`,
    style: "display:flex; gap:8px; padding:5px 2px; border-bottom:1px solid var(--color-grid-line); align-items:flex-start;",
  });
  row.addEventListener("click", () => openActivityModal(ev, data, row));
  row.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openActivityModal(ev, data, row);
    }
  });
  row.appendChild(el("span", { class: "mono", style: `color:${color}; width:14px; text-align:center; flex:none;` }, KIND_ICON[ev.kind] || "•"));
  const body = el("div", { style: "flex:1; min-width:0; display:flex; flex-direction:column; gap:1px;" });
  body.appendChild(el("div", { style: "font-size:11px; line-height:1.35;" }, ev.text || ""));
  const meta = el("div", { class: "faint mono", style: "font-size:9px; display:flex; gap:8px;" });
  const tSpan = el("span", { "data-t": ev.t }, fmtRelTime(ev.t));
  meta.appendChild(tSpan);
  if (ev.ref) meta.appendChild(el("span", {}, ev.ref));
  body.appendChild(meta);
  row.appendChild(body);
  return row;
}

export default {
  title: "Activity",
  minW: 3,
  minH: 3,
  render(container, { data, options, panel, breakpoint }) {
    if (container.__tickTimer) {
      clearInterval(container.__tickTimer);
      container.__tickTimer = null;
    }

    const maxItems = options?.max_items ?? 60;
    const events = Array.isArray(data?.events) ? data.events.slice() : [];
    events.sort((a, b) => new Date(b.t) - new Date(a.t));
    const list = events.slice(0, maxItems);

    if (list.length === 0) {
      container.appendChild(el("div", { class: "empty-state" }, "No activity."));
      return;
    }

    const buildWrap = (items) => {
      const wrap = el("div", { style: "display:flex; flex-direction:column; gap:1px;" });
      for (const ev of items) wrap.appendChild(buildRow(ev, data));
      return wrap;
    };

    const limit = breakpoint === "mobile" ? mobileLimit(panel, 15) : null;
    renderWithShowAll(container, list, limit, buildWrap);

    container.__tickTimer = setInterval(() => {
      if (!container.isConnected) {
        clearInterval(container.__tickTimer);
        return;
      }
      container.querySelectorAll("[data-t]").forEach((n) => {
        n.textContent = fmtRelTime(n.getAttribute("data-t"));
      });
    }, 15000);
  },
};
