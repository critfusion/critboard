import { el, fmtCost, fmtPct, clamp } from "../utils.js";

function threshold(pct) {
  if (pct >= 1) return { color: "var(--color-status-crit)", label: "CRITICAL" };
  if (pct >= 0.8) return { color: "var(--color-accent-amber)", label: "HOT" };
  if (pct >= 0.5) return { color: "var(--color-status-warn)", label: "WARNING" };
  return { color: "var(--color-status-ok)", label: "NORMAL" };
}

export default {
  title: "Monthly budget",
  minW: 4,
  minH: 1,
  render(container, { data }) {
    const b = data?.usage?.budget;
    if (!b || !b.monthly_usd) {
      container.appendChild(el("div", { class: "empty-state" }, "No budget configured."));
      return;
    }

    const pct = b.pct ?? (b.spent_mtd_usd / b.monthly_usd);
    const projectedPct = b.projected_month_usd / b.monthly_usd;
    const t = threshold(pct);
    const fillWidth = clamp(pct, 0, 1) * 100;
    const markerLeft = clamp(projectedPct, 0, 1) * 100;

    const wrap = el("div", { style: "display:flex; flex-direction:column; gap:6px; height:100%; justify-content:center;" });

    wrap.appendChild(
      el("div", { style: "display:flex; justify-content:space-between; align-items:baseline;" }, [
        el("div", { class: "mono tabular-nums", style: "font-size:15px;" }, [
          el("span", { style: `color:${t.color}; font-weight:700;` }, fmtCost(b.spent_mtd_usd)),
          el("span", { class: "faint" }, ` / ${fmtCost(b.monthly_usd)} MTD`),
        ]),
        el("div", { class: "mono tabular-nums dim", style: "font-size:11px;" },
          `projected ${fmtCost(b.projected_month_usd)} (${fmtPct(projectedPct)})`),
        el("span", { class: `pill`, style: `color:${t.color}; border-color:${t.color};` }, [
          el("span", { class: "pill-dot" }),
          t.label,
        ]),
      ])
    );

    const track = el("div", {
      style: "position:relative; height:16px; border-radius:8px; background:var(--color-bg-elevated); border:1px solid var(--color-border); overflow:visible;",
    });
    track.appendChild(
      el("div", {
        style: `position:absolute; inset:0; width:${fillWidth}%; background:${t.color}; border-radius:8px; transition:width var(--motion-transition-med);`,
      })
    );
    track.appendChild(
      el("div", {
        title: `projected month-end: ${fmtCost(b.projected_month_usd)} (${fmtPct(projectedPct)})`,
        style: `position:absolute; top:-3px; left:${markerLeft}%; width:2px; height:22px; background:var(--color-text); box-shadow:0 0 4px var(--color-text); transform:translateX(-1px);`,
      })
    );
    // 100% reference tick
    track.appendChild(
      el("div", { style: "position:absolute; top:0; right:0; width:1px; height:100%; background:var(--color-border-bright);" })
    );
    wrap.appendChild(track);

    wrap.appendChild(
      el("div", { class: "faint mono", style: "font-size:9px;" }, `${fmtPct(pct)} of monthly budget consumed · white marker = projected month-end`)
    );

    container.appendChild(wrap);
  },
};
