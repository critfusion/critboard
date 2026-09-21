import { el, fmtCost, fmtTokens } from "../utils.js";

const WINDOWS = [
  ["today", "TODAY"],
  ["7d", "7 DAYS"],
  ["30d", "30 DAYS"],
  ["all", "ALL TIME"],
];

function sumRange(timeline, fromMs, toMs) {
  let cost = 0;
  for (const p of timeline || []) {
    const t = new Date(p.t).getTime();
    if (t >= fromMs && t < toMs) cost += p.cost_usd || 0;
  }
  return cost;
}

export default {
  title: "Spend",
  minW: 3,
  minH: 2,
  render(container, { data, window: selectedWindow }) {
    const totals = data?.usage?.totals || {};
    const timeline = data?.usage?.timeline || [];

    const now = timeline.length ? new Date(timeline[timeline.length - 1].t).getTime() + 3600_000 : Date.now();
    const last24 = sumRange(timeline, now - 86400_000, now);
    const prev24 = sumRange(timeline, now - 172800_000, now - 86400_000);
    const delta = last24 - prev24;
    const deltaPct = prev24 > 0 ? delta / prev24 : null;

    const grid = el("div", { class: "tile-row", style: "height:100%;" });

    for (const [key, label] of WINDOWS) {
      const w = totals[key] || {};
      const tile = el("div", {
        style: [
          "border:1px solid var(--color-border);",
          "border-radius:var(--radius-card);",
          "padding:8px 10px;",
          "display:flex; flex-direction:column; gap:3px; justify-content:center;",
          key === selectedWindow ? "border-color:var(--color-accent-cyan);" : "",
        ].join(""),
      });
      tile.appendChild(el("div", { class: "faint mono", style: "font-size:9px; letter-spacing:.06em;" }, label));
      tile.appendChild(el("div", { class: "mono tabular-nums", style: "font-size:17px; color:var(--color-accent-amber); font-weight:700;" }, fmtCost(w.cost_usd ?? 0)));
      tile.appendChild(el("div", { class: "dim mono tabular-nums", style: "font-size:10px;" }, fmtTokens(w.total ?? 0) + " tok"));
      if (key === "today") {
        const sign = delta >= 0 ? "+" : "";
        const color = delta > 0 ? "var(--color-status-crit)" : delta < 0 ? "var(--color-status-ok)" : "var(--color-text-faint)";
        tile.appendChild(
          el("div", { class: "mono tabular-nums", style: `font-size:9px; color:${color};` },
            deltaPct === null ? "vs y/day: n/a" : `${sign}${(deltaPct * 100).toFixed(0)}% vs y/day`)
        );
      }
      grid.appendChild(tile);
    }

    container.appendChild(grid);
  },
};
