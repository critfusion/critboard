import { el, fmtTokens, fmtCost, fmtPct } from "../utils.js";

const BAR_COLORS = ["--color-accent-cyan", "--color-accent-amber", "--color-status-ok", "--color-severity-info", "--color-status-crit"];

export default {
  title: "Model split",
  minW: 3,
  minH: 2,
  render(container, { data, window: selectedWindow }) {
    const w = selectedWindow || "today";
    const rows = (data?.usage?.by_model || []).filter((r) => r.window === w);

    if (rows.length === 0) {
      container.appendChild(el("div", { class: "empty-state" }, `No model usage for window "${w}".`));
      return;
    }

    const withTotals = rows.map((r) => ({ ...r, tokens: (r.input || 0) + (r.output || 0) + (r.cache_read || 0) + (r.cache_write || 0) }));
    const tokenSum = withTotals.reduce((s, r) => s + r.tokens, 0) || 1;
    const costSum = withTotals.reduce((s, r) => s + (r.cost_usd || 0), 0) || 1;
    withTotals.sort((a, b) => b.cost_usd - a.cost_usd);

    const wrap = el("div", { style: "display:flex; flex-direction:column; gap:9px;" });
    wrap.appendChild(
      el("div", { class: "faint mono", style: "font-size:9px; display:flex; justify-content:space-between;" }, [
        el("span", {}, `window: ${w}`),
        el("span", {}, "tokens · cost"),
      ])
    );

    withTotals.forEach((r, i) => {
      const color = `var(${BAR_COLORS[i % BAR_COLORS.length]})`;
      const tokPct = r.tokens / tokenSum;
      const costPct = r.cost_usd / costSum;

      const row = el("div", { style: "display:flex; flex-direction:column; gap:2px;" });
      row.appendChild(
        el("div", { class: "kv-row" }, [
          el("span", { class: "mono truncate", style: `color:${color}; font-size:11px; font-weight:600;` }, r.model),
          el("span", { class: "mono tabular-nums dim kv-value", style: "font-size:10px;" },
            `${fmtTokens(r.tokens)} tok (${fmtPct(tokPct)}) · ${fmtCost(r.cost_usd)} (${fmtPct(costPct)})`),
        ])
      );

      const barTrack = el("div", { style: "display:flex; gap:3px; height:6px;" });
      barTrack.appendChild(el("div", { style: `flex:${Math.max(tokPct, 0.01)}; background:${color}; opacity:.55; border-radius:3px;`, title: "token share" }));
      barTrack.appendChild(el("div", { style: `flex:${Math.max(costPct, 0.01)}; background:${color}; border-radius:3px;`, title: "cost share" }));
      row.appendChild(barTrack);
      wrap.appendChild(row);
    });

    wrap.appendChild(
      el("div", { class: "faint", style: "font-size:9px; margin-top:2px;" },
        "left bar = token share, right bar = cost share (rates differ up to 10x by model)")
    );

    container.appendChild(wrap);
  },
};
