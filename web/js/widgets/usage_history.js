import { el, fmtTokens, fmtCost, loadJSON, saveJSON } from "../utils.js";

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const WINDOWS = [
  ["today", "TODAY"],
  ["7d", "7D"],
  ["30d", "30D"],
  ["90d", "90D"],
];
const GROUP_BYS = [
  ["model", "MODEL"],
  ["provider", "PROVIDER"],
  ["host", "HOST"],
];
const METRICS = [
  ["tokens", "TOKENS"],
  ["spend", "SPEND"],
];

// Cycled by each series' rank (largest total first) so colors stay distinct
// across whatever models/providers/hosts show up, without hardcoding a
// palette per key. [theme var, hardcoded fallback] pairs, same pattern as
// spend_timeline's cCyan/cAmber/... fallbacks.
const PALETTE = [
  ["--color-accent-cyan", "#3fe0e0"],
  ["--color-accent-amber", "#f2a93b"],
  ["--color-status-ok", "#3fe08a"],
  ["--color-severity-info", "#5aa8f2"],
  ["--color-status-crit", "#f2503b"],
  ["--color-agent-glow", "#4d9fff"],
  ["--color-accent-amber-dim", "#7a5a22"],
  ["--color-text-dim", "#8296a8"],
];

function paletteColor(idx) {
  const [varName, fallback] = PALETTE[((idx % PALETTE.length) + PALETTE.length) % PALETTE.length];
  return cssVar(varName) || fallback;
}

function storageKey(panelId) {
  return `critdash.usage_history.${panelId}`;
}

function loadState(panelId) {
  const saved = loadJSON(storageKey(panelId), null);
  const state = { window: "today", groupBy: "model", metric: "tokens" };
  if (saved && typeof saved === "object") {
    if (WINDOWS.some(([k]) => k === saved.window)) state.window = saved.window;
    if (GROUP_BYS.some(([k]) => k === saved.groupBy)) state.groupBy = saved.groupBy;
    if (METRICS.some(([k]) => k === saved.metric)) state.metric = saved.metric;
  }
  return state;
}

function saveState(panelId, state) {
  saveJSON(storageKey(panelId), { window: state.window, groupBy: state.groupBy, metric: state.metric });
}

async function fetchHistory(windowKey, groupBy) {
  const bucket = windowKey === "today" ? "hour" : "day";
  const url = `/api/history/usage?window=${encodeURIComponent(windowKey)}&bucket=${bucket}&group_by=${encodeURIComponent(groupBy)}`;
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status} from ${url}`);
  const json = await res.json();
  // The contract is an object with buckets[]/series[]/coverage -- the old
  // shape (and the current live backend, pre-group_by) is a plain array.
  // Treat anything that doesn't match as "not ready yet" rather than
  // crashing on undefined reads further down.
  if (!json || typeof json !== "object" || Array.isArray(json) || !Array.isArray(json.buckets) || !Array.isArray(json.series)) {
    throw new Error("history/usage response doesn't match the group_by contract yet -- backend not updated.");
  }
  return json;
}

// Kimi bills on a subscription quota with no per-token cost -- its cost_usd
// values come back null, not 0. A series is "cost unknown" when every
// bucket's cost is null; never coerce that to a zero band, it would read as
// free.
function isCostUnknown(s) {
  return Array.isArray(s.cost_usd) && s.cost_usd.length > 0 && s.cost_usd.every((v) => v === null || v === undefined);
}

function seriesValues(s, metric) {
  const arr = (metric === "spend" ? s.cost_usd : s.tokens) || [];
  return arr.map((v) => v || 0);
}

function seriesTotal(s, metric) {
  if (metric === "spend" && isCostUnknown(s)) return null;
  return seriesValues(s, metric).reduce((sum, v) => sum + v, 0);
}

function parseXs(buckets) {
  const parsed = buckets.map((b) => Date.parse(b));
  const isTime = parsed.length > 0 && parsed.every((t) => !Number.isNaN(t));
  return isTime ? { xs: parsed.map((t) => Math.floor(t / 1000)), isTime: true } : { xs: buckets.map((_, i) => i), isTime: false };
}

function seriesLabel(s, groupBy) {
  if (groupBy === "model" && s.provider) return `${s.key} (${s.provider})`;
  return s.key;
}

function buildToolbar(state, isMobile, onChange) {
  const toolbar = el("div", { class: "table-toolbar" });
  const groups = [
    ["window", WINDOWS],
    ["groupBy", GROUP_BYS],
    ["metric", METRICS],
  ];
  const controls = {};
  groups.forEach(([stateKey, opts], gi) => {
    if (isMobile) {
      const select = el(
        "select",
        { class: "table-sort-select", "aria-label": stateKey },
        opts.map(([v, label]) => el("option", { value: v }, label))
      );
      select.value = state[stateKey];
      select.addEventListener("change", () => {
        state[stateKey] = select.value;
        onChange();
      });
      toolbar.appendChild(select);
      controls[stateKey] = { select };
    } else {
      const divider = gi < groups.length - 1;
      const group = el("div", {
        style: `display:flex; gap:4px;${divider ? " padding-right:8px; margin-right:4px; border-right:1px solid var(--color-border);" : ""}`,
      });
      const btns = opts.map(([v, label]) => {
        const btn = el(
          "button",
          {
            type: "button",
            class: "table-filter-toggle" + (state[stateKey] === v ? " active" : ""),
            "aria-pressed": String(state[stateKey] === v),
          },
          label
        );
        btn.addEventListener("click", () => {
          if (state[stateKey] === v) return;
          state[stateKey] = v;
          onChange();
        });
        group.appendChild(btn);
        return { v, btn };
      });
      toolbar.appendChild(group);
      controls[stateKey] = { btns };
    }
  });
  return { toolbar, controls };
}

function refreshToolbarActive(controls, state) {
  for (const key of Object.keys(controls)) {
    const c = controls[key];
    if (c.select) {
      c.select.value = state[key];
    } else if (c.btns) {
      for (const { v, btn } of c.btns) {
        const active = state[key] === v;
        btn.classList.toggle("active", active);
        btn.setAttribute("aria-pressed", String(active));
      }
    }
  }
}

function buildLegendChip(label, color, total, metric, { excluded = false, dimmed = false, onClick = null } = {}) {
  const valueText = excluded ? "no cost data" : total === null || total === undefined ? "--" : metric === "spend" ? fmtCost(total) : fmtTokens(total);
  const chip = el(
    "span",
    {
      class: "legend-chip" + (dimmed ? " dimmed" : "") + (excluded ? " excluded" : ""),
      style: "font-size:9px;",
      tabindex: excluded ? null : "0",
      role: excluded ? null : "button",
      "aria-pressed": excluded ? null : String(!dimmed),
      title: excluded ? "excluded from spend -- no per-token cost data" : "click to toggle this series",
    },
    [
      el("span", { style: `width:8px; height:8px; border-radius:2px; background:${color}; flex:none;${dimmed ? " opacity:.4;" : ""}` }),
      el("span", { class: "mono truncate", style: "max-width:150px; color:var(--color-text-dim);" }, label),
      el("span", { class: "mono tabular-nums faint" }, valueText),
    ]
  );
  if (onClick) {
    chip.addEventListener("click", onClick);
    chip.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onClick();
      }
    });
  }
  return chip;
}

// Rebuilds the legend, coverage/cost notices, and the uPlot chart itself for
// the given already-fetched `json`. Called both after a fresh fetch and
// synchronously from legend-click toggles (no re-fetch needed for that).
function drawChart(container, refs, json, state, hiddenKeys) {
  const { chartHost, legendHost, noticeHost } = refs;

  // Tear down the previous chart instance/observer before rebuilding --
  // same guard spend_timeline.js uses at the top of render(), needed here
  // too since redraws happen from in-panel control changes, not just a
  // fresh app.js render() pass.
  if (container.__ro) {
    container.__ro.disconnect();
    container.__ro = null;
  }
  if (container.__roFrame != null) {
    cancelAnimationFrame(container.__roFrame);
    container.__roFrame = null;
  }
  if (container.__chart) {
    container.__chart.destroy();
    container.__chart = null;
  }

  chartHost.innerHTML = "";
  legendHost.innerHTML = "";
  noticeHost.innerHTML = "";

  const buckets = json.buckets || [];
  const allSeries = json.series || [];
  const metric = state.metric;

  if (!window.uPlot) {
    chartHost.appendChild(el("div", { class: "empty-state" }, "uPlot failed to load (web/vendor/uplot.js)."));
    return;
  }
  if (buckets.length === 0 || allSeries.length === 0) {
    chartHost.appendChild(el("div", { class: "empty-state" }, "No usage data for this window." ));
    return;
  }

  // Rank every series by its metric total so the biggest series gets the
  // first palette color and legend order is stable. Cost-unknown series
  // rank by token total instead (they never enter the spend stack, but
  // still need a sensible legend position).
  const ranked = allSeries
    .map((s) => ({ s, unknown: isCostUnknown(s), total: seriesTotal(s, metric) }))
    .sort((a, b) => (b.total ?? seriesTotal(b.s, "tokens") ?? 0) - (a.total ?? seriesTotal(a.s, "tokens") ?? 0));

  const colorForKey = (key) => {
    const idx = ranked.findIndex((r) => r.s.key === key);
    return paletteColor(idx < 0 ? 0 : idx);
  };

  // ---- legend (every series gets an entry; excluded ones are marked, never zeroed) ----
  const excludedLabels = [];
  ranked.forEach(({ s, unknown, total }) => {
    const color = colorForKey(s.key);
    const excludedFromStack = metric === "spend" && unknown;
    if (excludedFromStack) excludedLabels.push(seriesLabel(s, state.groupBy));
    const dimmed = hiddenKeys.has(s.key);
    const chip = buildLegendChip(seriesLabel(s, state.groupBy), color, total, metric, {
      excluded: excludedFromStack,
      dimmed,
      onClick: excludedFromStack
        ? null
        : () => {
            if (hiddenKeys.has(s.key)) hiddenKeys.delete(s.key);
            else hiddenKeys.add(s.key);
            drawChart(container, refs, json, state, hiddenKeys);
          },
    });
    legendHost.appendChild(chip);
  });

  if (excludedLabels.length > 0) {
    noticeHost.appendChild(
      el(
        "div",
        { style: "font-size:9px; color:var(--color-status-warn); font-family:var(--font-mono); padding:1px 0;" },
        `${excludedLabels.join(", ")}: subscription billing, no per-token cost data -- excluded from spend, not free.`
      )
    );
  }

  const coverage = json.coverage || {};
  const fleetCompleteFromMs = coverage.fleet_complete_from ? Date.parse(coverage.fleet_complete_from) : NaN;
  const { xs, isTime } = parseXs(buckets);

  const stackable = ranked.filter(({ s, unknown }) => !hiddenKeys.has(s.key) && !(metric === "spend" && unknown));

  if (stackable.length === 0) {
    chartHost.appendChild(
      el(
        "div",
        { class: "empty-state" },
        metric === "spend" ? "No series with cost data in this window." : "All series hidden -- click a legend entry to show one."
      )
    );
    return;
  }

  const zero = new Array(xs.length).fill(0);
  let cum = zero;
  const cumArrays = [];
  for (const { s } of stackable) {
    const vals = seriesValues(s, metric);
    cum = cum.map((c, i) => c + (vals[i] || 0));
    cumArrays.push(cum.slice());
  }

  const textFaint = cssVar("--color-text-faint") || "#546575";
  const gridLine = cssVar("--color-grid-line") || "#131a22";
  const warnColor = cssVar("--color-status-warn") || "#f2a93b";

  const { isMobile, isTablet } = state;

  // Same sizing discipline as spend_timeline.js: measure the chart host's
  // real laid-out box, never a hand-tuned fudge constant.
  const sizeFor = () => {
    const rect = chartHost.getBoundingClientRect();
    return { width: Math.max(200, Math.floor(rect.width)), height: Math.max(100, Math.floor(rect.height)) };
  };

  const opts = {
    ...sizeFor(),
    padding: [8, 8, 0, 8],
    legend: { show: false },
    cursor: { points: { show: false } },
    scales: { x: { time: isTime } },
    axes: [
      {
        stroke: textFaint,
        grid: { stroke: gridLine, width: 1 },
        ticks: { stroke: gridLine },
        space: isMobile ? 70 : isTablet ? 55 : 40,
        size: isMobile ? 28 : 34,
        values: isTime ? undefined : (u, vals) => vals.map((v) => buckets[Math.round(v)] ?? ""),
      },
      {
        stroke: textFaint,
        grid: { stroke: gridLine, width: 1 },
        ticks: { stroke: gridLine },
        values: (u, vals) => vals.map((v) => (metric === "spend" ? fmtCost(v) : fmtTokens(v))),
        space: isMobile ? 46 : 32,
        size: isMobile ? 54 : 58,
      },
    ],
    series: [
      {},
      { show: false, label: "zero" },
      ...stackable.map(({ s }) => ({ label: seriesLabel(s, state.groupBy), stroke: colorForKey(s.key), width: 1.5, fill: "transparent" })),
    ],
    // dir: 1 is required -- uPlot's band `dir` defaults to -1, which does
    // not paint a fill for this ascending cumulative-series stack (see the
    // same fix in spend_timeline.js).
    bands: stackable.map((r, i) => ({ series: [i + 1, i + 2], fill: colorForKey(r.s.key) + "33", dir: 1 })),
    hooks: {
      // Shade the region before fleet_complete_from -- not every host's
      // history reaches back that far, so a fleet total there is a partial
      // total, not a real dip/spike. drawClear fires before series are
      // painted, so the shading sits behind the bands, not on top.
      drawClear: [
        (u) => {
          try {
            if (Number.isNaN(fleetCompleteFromMs) || !isTime) return;
            const covSec = fleetCompleteFromMs / 1000;
            const xMin = u.scales.x.min;
            const xMax = u.scales.x.max;
            if (xMin == null || xMax == null || covSec <= xMin) return;
            const xEnd = Math.min(covSec, xMax);
            const leftPx = u.valToPos(xMin, "x", true);
            const endPx = u.valToPos(xEnd, "x", true);
            const ctx = u.ctx;
            ctx.save();
            ctx.fillStyle = warnColor + "1a";
            ctx.fillRect(leftPx, u.bbox.top, Math.max(0, endPx - leftPx), u.bbox.height);
            ctx.restore();
          } catch (e) {
            // never let a cosmetic shading pass break the chart
          }
        },
      ],
    },
  };

  const chart = new window.uPlot(opts, [xs, zero, ...cumArrays], chartHost);
  container.__chart = chart;

  if (!Number.isNaN(fleetCompleteFromMs) && isTime && fleetCompleteFromMs / 1000 > xs[0]) {
    const dateStr = String(coverage.fleet_complete_from).slice(0, 10);
    noticeHost.appendChild(
      el(
        "div",
        { style: "font-size:9px; color:var(--color-status-warn); font-family:var(--font-mono); padding:1px 0;" },
        `shaded: before ${dateStr} not every fleet host was reporting yet -- totals there undercount the fleet, they are not a real drop.`
      )
    );
  }

  // ---- resize discipline, copied from spend_timeline.js: re-entrancy
  // guard + 2px threshold + rAF coalescing, observing the panel body
  // (container), not the inner chart host -- matches app.js's
  // teardownPanels(), which only looks for `entry.body.__ro`. ----
  let lastSize = { width: opts.width, height: opts.height };
  let roApplying = false;
  const RESIZE_THRESHOLD_PX = 2;

  container.__ro = new ResizeObserver(() => {
    if (!container.isConnected) {
      container.__ro?.disconnect();
      return;
    }
    if (roApplying) return;
    if (container.__roFrame != null) return;
    container.__roFrame = requestAnimationFrame(() => {
      container.__roFrame = null;
      if (!container.isConnected) return;
      const next = sizeFor();
      const dw = Math.abs(next.width - lastSize.width);
      const dh = Math.abs(next.height - lastSize.height);
      if (dw < RESIZE_THRESHOLD_PX && dh < RESIZE_THRESHOLD_PX) return;
      lastSize = next;
      roApplying = true;
      try {
        chart.setSize(next);
      } finally {
        roApplying = false;
      }
    });
  });
  container.__ro.observe(container);
}

function mount(container, ctx) {
  // Tear down whatever the previous render pass (or this widget's own last
  // internal redraw) left attached to this exact container node before
  // wiping it -- container/body elements persist across snapshot resyncs
  // (app.js only clears innerHTML, it doesn't recreate the node), so these
  // custom properties can outlive a render() call.
  if (container.__ro) {
    container.__ro.disconnect();
    container.__ro = null;
  }
  if (container.__roFrame != null) {
    cancelAnimationFrame(container.__roFrame);
    container.__roFrame = null;
  }
  if (container.__chart) {
    container.__chart.destroy();
    container.__chart = null;
  }
  container.innerHTML = "";
  container.classList.add("chart-panel-body");

  if (!window.uPlot) {
    container.appendChild(el("div", { class: "empty-state" }, "uPlot failed to load (web/vendor/uplot.js)."));
    return;
  }

  const isMobile = ctx.breakpoint === "mobile";
  const isTablet = ctx.breakpoint === "tablet";
  const panelId = ctx.panel.id;
  const state = loadState(panelId);
  const hiddenKeys = new Set();

  const legendHost = el("div", { style: "display:flex; flex-wrap:wrap; gap:4px 10px; padding:4px 4px 0; flex:none;" });
  const noticeHost = el("div", { style: "display:flex; flex-direction:column; padding:2px 4px 0; flex:none;" });
  const chartHost = el("div", { style: "width:100%; flex:1; min-height:0;" });

  let loadSeq = 0;

  async function load() {
    const mySeq = ++loadSeq;
    chartHost.innerHTML = "";
    chartHost.appendChild(el("div", { class: "empty-state" }, "Loading usage history…"));
    legendHost.innerHTML = "";
    noticeHost.innerHTML = "";
    try {
      const json = await fetchHistory(state.window, state.groupBy);
      if (mySeq !== loadSeq || !chartHost.isConnected) return;
      drawChart(container, { chartHost, legendHost, noticeHost }, json, { ...state, isMobile, isTablet }, hiddenKeys);
    } catch (err) {
      if (mySeq !== loadSeq || !chartHost.isConnected) return;
      chartHost.innerHTML = "";
      chartHost.appendChild(
        el("div", { class: "empty-state", style: "color:var(--color-status-crit);" }, [
          el("div", {}, "Could not load usage history."),
          el("div", { class: "faint", style: "margin-top:4px; font-size:10px;" }, String((err && err.message) || err)),
        ])
      );
    }
  }

  const refresh = () => {
    saveState(panelId, state);
    refreshToolbarActive(controls, state);
    load();
  };

  const { toolbar, controls } = buildToolbar(state, isMobile, refresh);

  const wrap = el("div", { style: "display:flex; flex-direction:column; width:100%; height:100%;" }, [toolbar, legendHost, noticeHost, chartHost]);
  container.appendChild(wrap);

  load();
}

export default {
  title: "Usage history",
  minW: 6,
  minH: 4,
  render(container, ctx) {
    mount(container, ctx);
  },
};
