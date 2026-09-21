import { el, fmtInt, fmtPct, fmtRelTime } from "../utils.js";
import { openErrorModal } from "../detail.js";

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

function sectionTitle(text, first) {
  return el(
    "div",
    { class: "faint mono", style: `font-size:10px; letter-spacing:.06em; text-transform:uppercase; margin:${first ? "12px" : "16px"} 0 6px;` },
    text
  );
}

function emptyNote(text) {
  return el("div", { class: "empty-state", style: "padding:8px 0; text-align:left;" }, text);
}

function topErrorsTable(items, data) {
  if (!items.length) return emptyNote("No errors in this window.");
  const table = el("table", { class: "dtable" });
  table.appendChild(
    el("thead", {}, el("tr", {}, [el("th", {}, "KIND"), el("th", {}, "TOOL"), el("th", { class: "num" }, "COUNT"), el("th", { class: "num" }, "SHARE"), el("th", {}, "LAST SEEN")]))
  );
  const tbody = el("tbody");
  for (const e of items) {
    const tr = el(
      "tr",
      { class: "clickable-row", tabindex: "0", role: "button", "aria-label": `Open detail for ${e.kind} / ${e.tool || "-"}` },
      [
        el("td", { class: "mono truncate" }, e.kind),
        el("td", { class: "dim mono truncate" }, e.tool || "-"),
        el("td", { class: "num mono tabular-nums" }, fmtInt(e.count)),
        el("td", { class: "num mono tabular-nums" }, fmtPct(e.pct)),
        el("td", { class: "faint mono", style: "font-size:10px;" }, fmtRelTime(e.last_seen)),
      ]
    );
    const open = () => openErrorModal(e, data, tr);
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        open();
      }
    });
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

function byToolTable(rows) {
  if (!rows.length) return emptyNote("No tool call data in this window.");
  const sorted = rows.slice().sort((a, b) => (b.error_rate || 0) - (a.error_rate || 0));
  const table = el("table", { class: "dtable" });
  table.appendChild(
    el("thead", {}, el("tr", {}, [el("th", {}, "TOOL"), el("th", { class: "num" }, "CALLS"), el("th", { class: "num" }, "ERRORS"), el("th", {}, "ERROR RATE")]))
  );
  const tbody = el("tbody");
  for (const r of sorted) {
    tbody.appendChild(
      el("tr", {}, [
        el("td", { class: "mono truncate" }, r.tool),
        el("td", { class: "num mono tabular-nums" }, fmtInt(r.calls)),
        el("td", { class: "num mono tabular-nums", style: (r.errors || 0) > 0 ? "color:var(--color-status-warn);" : "" }, fmtInt(r.errors || 0)),
        el("td", {}, rateBar(r.error_rate)),
      ])
    );
  }
  table.appendChild(tbody);
  return table;
}

function troubleFilesTable(items) {
  if (!items.length) return emptyNote("No trouble files in this window.");
  const table = el("table", { class: "dtable" });
  table.appendChild(el("thead", {}, el("tr", {}, [el("th", {}, "PATH"), el("th", { class: "num" }, "ERRORS"), el("th", {}, "TOOLS")])));
  const tbody = el("tbody");
  for (const f of items) {
    tbody.appendChild(
      el("tr", {}, [
        el("td", { class: "mono truncate", title: f.path }, f.path),
        el("td", { class: "num mono tabular-nums" }, fmtInt(f.errors)),
        el("td", { class: "dim mono truncate" }, (f.tools || []).join(", ")),
      ])
    );
  }
  table.appendChild(tbody);
  return table;
}

export default {
  title: "Error patterns",
  minW: 6,
  minH: 4,
  render(container, { data }) {
    const errors = data?.analytics?.errors;
    if (!errors) {
      container.appendChild(el("div", { class: "empty-state" }, "No error analytics available."));
      return;
    }

    const wrap = el("div", { style: "display:flex; flex-direction:column;" });

    wrap.appendChild(
      el("div", { style: "display:flex; justify-content:space-between; align-items:baseline;" }, [
        el(
          "div",
          { class: "mono tabular-nums", style: "font-size:20px; font-weight:700; color:var(--color-status-crit);" },
          `${fmtInt(errors.total_errors ?? 0)} errors`
        ),
        el("span", { class: "faint mono", style: "font-size:10px;" }, `window: ${errors.window || "-"}`),
      ])
    );

    wrap.appendChild(sectionTitle("TOP FAILURE KINDS", true));
    wrap.appendChild(topErrorsTable(errors.top_errors || [], data));

    wrap.appendChild(sectionTitle("ERROR RATE BY TOOL"));
    wrap.appendChild(byToolTable(errors.by_tool || []));

    wrap.appendChild(sectionTitle("TROUBLE FILES"));
    wrap.appendChild(troubleFilesTable(errors.trouble_files || []));

    if (errors.api_errors && errors.api_errors.length) {
      wrap.appendChild(sectionTitle("API ERRORS"));
      const apiWrap = el("div", { style: "display:flex; flex-wrap:wrap; gap:6px;" });
      for (const a of errors.api_errors) {
        apiWrap.appendChild(
          el("span", { class: "pill pill-warn", title: `last seen ${a.last_seen || "-"}` }, [el("span", { class: "pill-dot" }), `${a.kind} ×${fmtInt(a.count)}`])
        );
      }
      wrap.appendChild(apiWrap);
    }

    container.appendChild(wrap);
  },
};
