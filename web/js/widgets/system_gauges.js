import { el, fmtPct } from "../utils.js";

function bar(label, used, total, unitFmt) {
  const pct = total > 0 ? used / total : 0;
  const color = pct > 0.9 ? "var(--color-status-crit)" : pct > 0.7 ? "var(--color-status-warn)" : "var(--color-status-ok)";
  const row = el("div", { style: "display:flex; flex-direction:column; gap:3px;" });
  row.appendChild(
    el("div", { style: "display:flex; justify-content:space-between; font-size:10px;" }, [
      el("span", { class: "dim mono" }, label),
      el("span", { class: "mono tabular-nums" }, unitFmt ? unitFmt(used, total) : `${fmtPct(pct)}`),
    ])
  );
  row.appendChild(
    el("div", { style: "height:6px; border-radius:3px; background:var(--color-bg-elevated); border:1px solid var(--color-border); overflow:hidden;" },
      el("div", { style: `height:100%; width:${Math.min(100, pct * 100)}%; background:${color};` })
    )
  );
  return row;
}

export default {
  title: "System",
  minW: 2,
  minH: 2,
  render(container, { data }) {
    const sys = data?.system;
    if (!sys) {
      container.appendChild(el("div", { class: "empty-state" }, "No system data."));
      return;
    }

    const wrap = el("div", { style: "display:flex; flex-direction:column; gap:9px; height:100%;" });

    const loadRow = el("div", { style: "display:flex; justify-content:space-between;" }, [
      el("span", { class: "dim mono", style: "font-size:10px;" }, `LOAD (${sys.cpu_count ?? "-"} cpu)`),
      el("span", { class: "mono tabular-nums", style: "font-size:11px;" }, `${(sys.load1 ?? 0).toFixed(2)} / ${(sys.load5 ?? 0).toFixed(2)} / ${(sys.load15 ?? 0).toFixed(2)}`),
    ]);
    wrap.appendChild(loadRow);

    wrap.appendChild(bar("MEM", sys.mem_used_gb ?? 0, sys.mem_total_gb ?? 0, (u, t) => `${u.toFixed(1)} / ${t.toFixed(1)} GB`));

    for (const d of sys.disks || []) {
      wrap.appendChild(bar(d.mount, d.used_gb ?? 0, d.total_gb ?? 0, (u, t) => `${u.toFixed(0)} / ${t.toFixed(0)} GB`));
    }

    container.appendChild(wrap);
  },
};
