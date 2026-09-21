import { el, fmtInt, fmtPct } from "../utils.js";

function rateBar(rate) {
  const pct = Math.max(0, Math.min(1, rate || 0));
  const color = pct > 0.15 ? "var(--color-status-crit)" : pct > 0.05 ? "var(--color-status-warn)" : "var(--color-status-ok)";
  return el("div", { style: "display:flex; align-items:center; gap:6px; min-width:80px;" }, [
    el(
      "div",
      { style: "flex:1; height:6px; border-radius:3px; background:var(--color-bg-elevated); border:1px solid var(--color-border); overflow:hidden;" },
      el("div", { style: `height:100%; width:${(pct * 100).toFixed(1)}%; background:${color};` })
    ),
    el("span", { class: "mono tabular-nums", style: `font-size:11px; color:${color}; flex:none;` }, fmtPct(rate, 1)),
  ]);
}

export default {
  title: "Tool usage",
  minW: 4,
  minH: 4,
  render(container, { data }) {
    const tools = data?.analytics?.tools;
    if (!tools || !Array.isArray(tools.usage) || tools.usage.length === 0) {
      container.appendChild(el("div", { class: "empty-state" }, "No tool usage analytics available."));
      return;
    }

    const wrap = el("div", { style: "display:flex; flex-direction:column; gap:10px;" });
    wrap.appendChild(el("div", { class: "faint mono", style: "font-size:9px; text-transform:uppercase;" }, `window: ${tools.window || "-"}`));

    const rows = tools.usage.slice().sort((a, b) => (b.calls || 0) - (a.calls || 0));
    const table = el("table", { class: "dtable" });
    table.appendChild(
      el(
        "thead",
        {},
        el("tr", {}, [el("th", {}, "TOOL"), el("th", { class: "num" }, "CALLS"), el("th", { class: "num" }, "SHARE"), el("th", { class: "num" }, "ERRORS"), el("th", {}, "ERROR RATE")])
      )
    );
    const tbody = el("tbody");
    for (const r of rows) {
      const rate = r.calls > 0 ? (r.errors || 0) / r.calls : 0;
      tbody.appendChild(
        el("tr", {}, [
          el("td", { class: "mono truncate" }, r.tool),
          el("td", { class: "num mono tabular-nums" }, fmtInt(r.calls)),
          el("td", { class: "num mono tabular-nums dim" }, fmtPct(r.pct)),
          el("td", { class: "num mono tabular-nums", style: (r.errors || 0) > 0 ? "color:var(--color-status-warn);" : "" }, fmtInt(r.errors || 0)),
          el("td", {}, rateBar(rate)),
        ])
      );
    }
    table.appendChild(tbody);
    wrap.appendChild(table);

    const decisions = tools.decisions;
    if (decisions && (decisions.total ?? 0) > 0) {
      wrap.appendChild(
        el("div", { class: "faint mono", style: "font-size:10px; letter-spacing:.06em; text-transform:uppercase; margin-top:4px;" }, "DECISIONS")
      );
      wrap.appendChild(
        el("div", { style: "display:flex; gap:14px; flex-wrap:wrap;" }, [
          el("span", { class: "mono tabular-nums" }, [el("span", { class: "dim" }, "accepted "), el("span", { style: "color:var(--color-status-ok);" }, fmtInt(decisions.accepted))]),
          el("span", { class: "mono tabular-nums" }, [el("span", { class: "dim" }, "rejected "), el("span", { style: "color:var(--color-status-crit);" }, fmtInt(decisions.rejected))]),
          el("span", { class: "mono tabular-nums dim" }, `total ${fmtInt(decisions.total)}`),
        ])
      );
    }
    // decisions.total === 0 -> render nothing for it; the data is honestly
    // unavailable, not zero-and-worth-showing.

    container.appendChild(wrap);
  },
};
