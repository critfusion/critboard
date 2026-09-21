import { el, fmtTokens, fmtCost, fmtRelTime, fmtPct, fmtInt } from "../utils.js";

function hostTile(h, share, windowLabel) {
  const failed = h.ok === false || !!h.error || h.reachable === false;
  const borderColor = failed ? "var(--color-status-crit)" : "var(--color-border)";
  const tile = el("div", {
    style:
      `border:1px solid ${borderColor}; border-radius:var(--radius-card); padding:10px; display:flex; flex-direction:column; gap:6px; background:var(--color-bg-elevated); min-width:0;` +
      (failed ? " box-shadow:0 0 0 1px var(--color-status-crit);" : ""),
  });

  tile.appendChild(
    el("div", { style: "display:flex; justify-content:space-between; align-items:center; gap:6px;" }, [
      el("span", { class: "mono truncate", style: "font-weight:700;" }, h.name || "-"),
      el("span", { class: `pill ${failed ? "pill-crit" : "pill-ok"}` }, [el("span", { class: "pill-dot" }), failed ? "DOWN" : "OK"]),
    ])
  );

  tile.appendChild(
    el("div", { class: "faint mono", style: "font-size:10px;" }, `${h.mode || "-"} · reachable: ${h.reachable === false ? "no" : "yes"}`)
  );

  if (failed && h.error) {
    tile.appendChild(
      el(
        "div",
        {
          style:
            "color:var(--color-status-crit); font-family:var(--font-mono); font-size:11px; background:rgba(242,80,59,0.1); border:1px solid var(--color-status-crit); border-radius:4px; padding:6px 8px; word-break:break-word;",
        },
        h.error
      )
    );
  }

  tile.appendChild(el("div", { class: "dim mono", style: "font-size:10px;" }, `last ok: ${fmtRelTime(h.last_ok)}`));

  tile.appendChild(
    el("div", { style: "display:flex; justify-content:space-between; font-size:11px;" }, [
      el("span", { class: "mono tabular-nums" }, `${fmtInt(h.agents ?? 0)} agents`),
      el("span", { class: "mono tabular-nums" }, `${fmtInt(h.worktrees ?? 0)} wt`),
    ])
  );

  tile.appendChild(
    el("div", { style: "display:flex; justify-content:space-between; align-items:baseline; margin-top:2px;" }, [
      el("span", { class: "mono tabular-nums", style: "font-size:12px;" }, fmtTokens(h.tokens_today ?? 0) + " tok"),
      el("span", { class: "mono tabular-nums", style: "font-size:12px; color:var(--color-accent-amber);" }, fmtCost(h.cost_today_usd ?? 0)),
    ])
  );

  tile.appendChild(
    el(
      "div",
      { class: "faint mono", style: "font-size:9px;" },
      share !== null ? `${fmtPct(share, 1)} of fleet spend (${windowLabel})` : "spend share: n/a"
    )
  );

  return tile;
}

export default {
  title: "Host health",
  minW: 6,
  minH: 3,
  render(container, { data, window: selectedWindow }) {
    const hosts = Array.isArray(data?.hosts) ? data.hosts : [];
    if (hosts.length === 0) {
      container.appendChild(el("div", { class: "empty-state" }, "No hosts reporting."));
      return;
    }

    const w = selectedWindow || "today";
    const byHost = (data?.usage?.by_host || []).filter((r) => r.window === w);
    const fleetSpend = byHost.reduce((s, r) => s + (r.cost_usd || 0), 0);

    const grid = el("div", { style: "display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:10px;" });

    const sorted = hosts.slice().sort((a, b) => (a.name || "").localeCompare(b.name || ""));
    for (const h of sorted) {
      const spendRow = byHost.find((r) => r.host === h.name);
      const hostCost = spendRow ? spendRow.cost_usd || 0 : 0;
      const share = fleetSpend > 0 ? hostCost / fleetSpend : null;
      grid.appendChild(hostTile(h, share, w));
    }

    container.appendChild(grid);
  },
};
