import { el, fmtCost, fmtPct } from "../utils.js";

function statTile(label, value, colorVar) {
  return el(
    "div",
    { style: "border:1px solid var(--color-border); border-radius:var(--radius-card); padding:8px 10px; display:flex; flex-direction:column; gap:3px;" },
    [
      el("div", { class: "faint mono", style: "font-size:9px; letter-spacing:.06em;" }, label),
      el("div", { class: "mono tabular-nums", style: `font-size:16px; font-weight:700; color:var(${colorVar});` }, value),
    ]
  );
}

export default {
  title: "Subagent cost",
  minW: 5,
  minH: 4,
  render(container, { data }) {
    const sub = data?.analytics?.subagents;
    if (!sub) {
      container.appendChild(el("div", { class: "empty-state" }, "No subagent cost analytics available."));
      return;
    }

    const totals = sub.totals || {};
    const main = totals.main_cost_usd ?? 0;
    const side = totals.sidechain_cost_usd ?? 0;
    const share = totals.sidechain_share ?? 0;

    const wrap = el("div", { style: "display:flex; flex-direction:column; gap:10px;" });

    wrap.appendChild(
      el("div", { class: "tile-row tile-row-wide" }, [
        statTile("MAIN SPEND", fmtCost(main), "--color-accent-cyan"),
        statTile("DELEGATED (SIDECHAIN)", fmtCost(side), "--color-accent-amber"),
        statTile("DELEGATED SHARE", fmtPct(share, 1), "--color-text"),
      ])
    );

    if (!side && !share) {
      wrap.appendChild(
        el(
          "div",
          { class: "detail-note-warn", style: "margin:0;" },
          "No sidechain-attributed delegation detected. This fleet's subagents write their own separate session files instead of Claude Code's native sidechain rows, so no cost is attributable to delegation through that mechanism -- verified, not a bug."
        )
      );
    } else {
      wrap.appendChild(
        el("div", { style: "display:flex; height:10px; border-radius:4px; overflow:hidden; border:1px solid var(--color-border);" }, [
          el("div", { style: `flex:${Math.max(main, 0.0001)}; background:var(--color-accent-cyan);`, title: `main ${fmtCost(main)}` }),
          el("div", { style: `flex:${Math.max(side, 0.0001)}; background:var(--color-accent-amber);`, title: `sidechain ${fmtCost(side)}` }),
        ])
      );
    }

    const sessions = Array.isArray(sub.sessions) ? sub.sessions : [];
    wrap.appendChild(
      el(
        "div",
        { class: "faint mono", style: "font-size:10px; letter-spacing:.06em; text-transform:uppercase; margin-top:4px;" },
        `SESSIONS (${sessions.length}) -- window ${sub.window || "-"}`
      )
    );

    if (sessions.length === 0) {
      wrap.appendChild(el("div", { class: "empty-state", style: "padding:8px 0; text-align:left;" }, "No session breakdown available."));
    } else {
      const sorted = sessions.slice().sort((a, b) => (b.main_cost_usd || 0) - (a.main_cost_usd || 0));
      const table = el("table", { class: "dtable" });
      table.appendChild(
        el(
          "thead",
          {},
          el("tr", {}, [
            el("th", {}, "PROJECT"),
            el("th", {}, "HOST"),
            el("th", { class: "num" }, "MAIN"),
            el("th", { class: "num" }, "SIDECHAIN"),
            el("th", { class: "num" }, "SHARE"),
            el("th", {}, "MODELS"),
          ])
        )
      );
      const tbody = el("tbody");
      for (const s of sorted) {
        tbody.appendChild(
          el("tr", {}, [
            el("td", { class: "mono truncate" }, s.project || s.session_id),
            el("td", { class: "dim mono" }, s.host || "-"),
            el("td", { class: "num mono tabular-nums" }, fmtCost(s.main_cost_usd ?? 0)),
            el("td", { class: "num mono tabular-nums dim" }, fmtCost(s.sidechain_cost_usd ?? 0)),
            el("td", { class: "num mono tabular-nums dim" }, fmtPct(s.sidechain_share ?? 0, 1)),
            el("td", { class: "faint mono truncate", style: "font-size:10px;" }, (s.model_mix || []).join(", ")),
          ])
        );
      }
      table.appendChild(tbody);
      wrap.appendChild(table);
    }

    container.appendChild(wrap);
  },
};
