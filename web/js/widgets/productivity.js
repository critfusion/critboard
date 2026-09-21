import { el, fmtInt } from "../utils.js";

function stat(label, value) {
  return el(
    "div",
    { style: "border:1px solid var(--color-border); border-radius:var(--radius-card); padding:8px 10px; display:flex; flex-direction:column; gap:3px;" },
    [
      el("div", { class: "faint mono", style: "font-size:9px; letter-spacing:.06em;" }, label),
      el("div", { class: "mono tabular-nums", style: "font-size:16px; font-weight:700;" }, value),
    ]
  );
}

function divergingBar(added, removed) {
  const total = Math.max(added + removed, 1);
  const addPct = added / total;
  const remPct = removed / total;
  return el("div", { style: "display:flex; height:10px; border-radius:4px; overflow:hidden; border:1px solid var(--color-border);" }, [
    el("div", { style: `flex:${Math.max(addPct, 0.0001)}; background:var(--color-status-ok);`, title: `+${fmtInt(added)}` }),
    el("div", { style: `flex:${Math.max(remPct, 0.0001)}; background:var(--color-status-crit);`, title: `-${fmtInt(removed)}` }),
  ]);
}

export default {
  title: "Productivity",
  minW: 5,
  minH: 4,
  render(container, { data }) {
    const p = data?.analytics?.productivity;
    if (!p) {
      container.appendChild(el("div", { class: "empty-state" }, "No productivity analytics available."));
      return;
    }

    const wrap = el("div", { style: "display:flex; flex-direction:column; gap:10px;" });

    wrap.appendChild(
      el("div", { class: "tile-row" }, [
        stat("COMMITS 7D", fmtInt(p.commits_7d ?? 0)),
        stat("COMMITS 30D", fmtInt(p.commits_30d ?? 0)),
        stat("FILES CHANGED 7D", fmtInt(p.files_changed_7d ?? 0)),
      ])
    );

    const added = p.lines_added_7d ?? 0;
    const removed = p.lines_removed_7d ?? 0;
    wrap.appendChild(
      el("div", { style: "display:flex; flex-direction:column; gap:4px;" }, [
        el("div", { style: "display:flex; justify-content:space-between; font-size:10px;" }, [
          el("span", { class: "mono", style: "color:var(--color-status-ok);" }, `+${fmtInt(added)} added`),
          el("span", { class: "mono", style: "color:var(--color-status-crit);" }, `-${fmtInt(removed)} removed`),
        ]),
        divergingBar(added, removed),
      ])
    );

    const repos = Array.isArray(p.by_repo) ? p.by_repo.slice().sort((a, b) => (b.commits || 0) - (a.commits || 0)) : [];
    wrap.appendChild(
      el(
        "div",
        { class: "faint mono", style: "font-size:10px; letter-spacing:.06em; text-transform:uppercase; margin-top:4px;" },
        `BY REPO (7D)${p.truncated ? " -- truncated" : ""}`
      )
    );

    if (repos.length === 0) {
      wrap.appendChild(el("div", { class: "empty-state", style: "padding:8px 0; text-align:left;" }, "No per-repo activity."));
    } else {
      const table = el("table", { class: "dtable" });
      table.appendChild(
        el("thead", {}, el("tr", {}, [el("th", {}, "REPO"), el("th", { class: "num" }, "COMMITS"), el("th", { class: "num" }, "+LINES"), el("th", { class: "num" }, "-LINES")]))
      );
      const tbody = el("tbody");
      for (const r of repos) {
        tbody.appendChild(
          el("tr", {}, [
            el("td", { class: "mono truncate" }, r.repo),
            el("td", { class: "num mono tabular-nums" }, fmtInt(r.commits)),
            el("td", { class: "num mono tabular-nums", style: "color:var(--color-status-ok);" }, "+" + fmtInt(r.lines_added)),
            el("td", { class: "num mono tabular-nums", style: "color:var(--color-status-crit);" }, "-" + fmtInt(r.lines_removed)),
          ])
        );
      }
      table.appendChild(tbody);
      wrap.appendChild(table);
    }

    container.appendChild(wrap);
  },
};
