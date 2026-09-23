import { el, fmtCost } from "../utils.js";

export default {
  title: "Burn rate",
  minW: 2,
  minH: 2,
  render(container, { data }) {
    const burn = data?.usage?.burn;
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

    container.appendChild(wrap);
  },
};
