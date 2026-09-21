import { el, fmtCost, fmtPct, fmtRelTime } from "../utils.js";

function ring(pct, size = 64) {
  const r = size / 2 - 5;
  const c = 2 * Math.PI * r;
  const color = pct > 0.9 ? "var(--color-status-crit)" : pct > 0.7 ? "var(--color-status-warn)" : "var(--color-accent-cyan)";
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("viewBox", `0 0 ${size} ${size}`);

  const bg = document.createElementNS(svgNS, "circle");
  bg.setAttribute("cx", size / 2);
  bg.setAttribute("cy", size / 2);
  bg.setAttribute("r", r);
  bg.setAttribute("fill", "none");
  bg.setAttribute("stroke", "var(--color-border)");
  bg.setAttribute("stroke-width", 5);
  svg.appendChild(bg);

  const fg = document.createElementNS(svgNS, "circle");
  fg.setAttribute("cx", size / 2);
  fg.setAttribute("cy", size / 2);
  fg.setAttribute("r", r);
  fg.setAttribute("fill", "none");
  fg.setAttribute("stroke", color);
  fg.setAttribute("stroke-width", 5);
  fg.setAttribute("stroke-linecap", "round");
  fg.setAttribute("stroke-dasharray", `${c * Math.min(1, pct)} ${c}`);
  fg.setAttribute("transform", `rotate(-90 ${size / 2} ${size / 2})`);
  svg.appendChild(fg);

  return svg;
}

export default {
  title: "Burn rate",
  minW: 2,
  minH: 2,
  render(container, { data }) {
    const burn = data?.usage?.burn;
    const block = data?.usage?.block;
    if (!burn) {
      container.appendChild(el("div", { class: "empty-state" }, "No burn data."));
      return;
    }

    const wrap = el("div", { style: "display:flex; flex-direction:column; gap:10px; height:100%;" });

    // flex-wrap + a min-width on each side lets the two halves drop to
    // separate lines instead of clipping/overlapping when the panel is
    // too narrow for both cost strings side by side (phone widths, or a
    // large future all-time total).
    const top = el("div", { style: "display:flex; flex-wrap:wrap; justify-content:space-between; align-items:baseline; gap:4px 12px;" }, [
      el("div", { style: "min-width:120px;" }, [
        el("div", { class: "faint mono", style: "font-size:9px;" }, "NOW / 24H AVG"),
        el("div", { class: "mono tabular-nums", style: "font-size:18px; font-weight:700; color:var(--color-accent-amber);" },
          `${fmtCost(burn.usd_per_hour_1h)}/hr`),
        el("div", { class: "dim mono tabular-nums", style: "font-size:10px;" }, `${fmtCost(burn.usd_per_hour_24h)}/hr avg`),
      ]),
      el("div", { style: "min-width:110px;" }, [
        el("div", { class: "faint mono", style: "font-size:9px; text-align:right;" }, "PROJECTED MONTH"),
        el("div", { class: "mono tabular-nums", style: "font-size:15px; text-align:right;" }, fmtCost(burn.projected_month_usd)),
      ]),
    ]);
    wrap.appendChild(top);

    if (block) {
      const pct = block.pct_elapsed ?? 0;
      const row = el("div", { style: "display:flex; align-items:center; gap:12px;" }, [
        ring(pct),
        el("div", { style: "flex:1; min-width:0;" }, [
          el("div", { class: "faint mono", style: "font-size:9px;" }, "5H RATE-LIMIT BLOCK"),
          el("div", { class: "mono tabular-nums", style: "font-size:13px;" }, `${fmtPct(pct)} elapsed`),
          el("div", { class: "dim mono tabular-nums", style: "font-size:10px;" }, `${fmtCost(block.cost_usd)} used`),
          el("div", { class: "faint mono", style: "font-size:9px;" }, `ends ${fmtRelTime(block.ends_at)}`),
        ]),
      ]);
      wrap.appendChild(row);
    }

    container.appendChild(wrap);
  },
};
