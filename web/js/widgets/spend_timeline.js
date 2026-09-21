import { el, fmtTokens } from "../utils.js";

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export default {
  title: "Spend timeline",
  minW: 4,
  minH: 3,
  render(container, { data, breakpoint }) {
    const timeline = data?.usage?.timeline || [];
    const isMobile = breakpoint === "mobile";
    const isTablet = breakpoint === "tablet";

    if (!window.uPlot) {
      container.appendChild(el("div", { class: "empty-state" }, "uPlot failed to load (web/vendor/uplot.js)."));
      return;
    }
    if (timeline.length === 0) {
      container.appendChild(el("div", { class: "empty-state" }, "No timeline data."));
      return;
    }

    if (container.__ro) {
      container.__ro.disconnect();
      container.__ro = null;
    }
    if (container.__roFrame != null) {
      cancelAnimationFrame(container.__roFrame);
      container.__roFrame = null;
    }
    container.classList.add("chart-panel-body");

    const xs = timeline.map((p) => Math.floor(new Date(p.t).getTime() / 1000));
    const zero = new Array(timeline.length).fill(0);
    const cumInput = timeline.map((p) => p.input || 0);
    const cumOutput = cumInput.map((v, i) => v + (timeline[i].output || 0));
    const cumWrite = cumOutput.map((v, i) => v + (timeline[i].cache_write || 0));
    const cumRead = cumWrite.map((v, i) => v + (timeline[i].cache_read || 0));

    const cCyan = cssVar("--color-accent-cyan") || "#3fe0e0";
    const cAmber = cssVar("--color-accent-amber") || "#f2a93b";
    const cOk = cssVar("--color-status-ok") || "#3fe08a";
    const cDim = cssVar("--color-text-dim") || "#8296a8";
    const textFaint = cssVar("--color-text-faint") || "#546575";
    const gridLine = cssVar("--color-grid-line") || "#131a22";

    // uPlot's built-in legend inherits body text color and can end up below
    // the fold if it doesn't fit the panel's fixed height. Use a small
    // static legend row instead -- always visible, no scroll required.
    const legendSpecs = [
      ["input", cCyan],
      ["output", cAmber],
      ["cache_write", cOk],
      ["cache_read", cDim],
    ];
    const legend = el(
      "div",
      { style: "display:flex; flex-wrap:wrap; gap:10px 14px; padding:0 4px 6px; flex:none;" },
      legendSpecs.map(([label, color]) =>
        el("span", { style: "display:flex; align-items:center; gap:5px; font-size:9px; font-family:var(--font-mono); color:var(--color-text-dim);" }, [
          el("span", { style: `width:8px; height:8px; border-radius:2px; background:${color}; flex:none;` }),
          el("span", {}, label),
        ])
      )
    );

    const chartHost = el("div", { style: "width:100%; flex:1; min-height:0;" });
    const wrap = el("div", { style: "display:flex; flex-direction:column; width:100%; height:100%;" }, [legend, chartHost]);
    container.appendChild(wrap);

    // Measure the chart host's own laid-out box rather than guessing the
    // panel's chrome. `chartHost` is `flex:1` inside `wrap` (which is
    // `height:100%` of `container`), so the browser has already subtracted
    // the legend's real height and the panel's padding/border for us by the
    // time this runs (post-appendChild, so layout has flushed). Flooring
    // (not rounding) guarantees the requested canvas size never exceeds the
    // box that's actually available, which is what produced the permanent
    // 2px overflow before: the old `- 10` fudge under-subtracted the real
    // chrome by 2px at every width.
    const sizeFor = () => {
      const rect = chartHost.getBoundingClientRect();
      return {
        width: Math.max(200, Math.floor(rect.width)),
        height: Math.max(100, Math.floor(rect.height)),
      };
    };

    const opts = {
      ...sizeFor(),
      padding: [8, 8, 0, 8],
      legend: { show: false },
      cursor: { points: { show: false } },
      scales: { x: { time: true } },
      axes: [
        {
          stroke: textFaint,
          grid: { stroke: gridLine, width: 1 },
          ticks: { stroke: gridLine },
          // Thin ticks out on narrow viewports so time labels don't collide.
          space: isMobile ? 70 : isTablet ? 55 : 40,
          size: isMobile ? 28 : 34,
        },
        {
          stroke: textFaint,
          grid: { stroke: gridLine, width: 1 },
          ticks: { stroke: gridLine },
          values: (u, vals) => vals.map((v) => fmtTokens(v)),
          space: isMobile ? 46 : 32,
          size: isMobile ? 42 : 50,
        },
      ],
      series: [
        {},
        { show: false, label: "zero" },
        { label: "input", stroke: cCyan, width: 1.5, fill: "transparent" },
        { label: "output", stroke: cAmber, width: 1.5, fill: "transparent" },
        { label: "cache_write", stroke: cOk, width: 1.5, fill: "transparent" },
        { label: "cache_read", stroke: cDim, width: 1.5, fill: "transparent" },
      ],
      // `dir: 1` is required -- uPlot's band `dir` defaults to -1, which
      // does not paint a fill for this ascending cumulative-series stack
      // (verified empirically: with the default, none of these bands ever
      // render a pixel; every other option here was already correct).
      bands: [
        { series: [1, 2], fill: cCyan + "33", dir: 1 },
        { series: [2, 3], fill: cAmber + "33", dir: 1 },
        { series: [3, 4], fill: cOk + "33", dir: 1 },
        { series: [4, 5], fill: cDim + "26", dir: 1 },
      ],
    };

    const chart = new window.uPlot(opts, [xs, zero, cumInput, cumOutput, cumWrite, cumRead], chartHost);

    // Guard against the resize feedback loop: a `setSize()` call resizes the
    // canvas inside the observed container, which can itself queue another
    // ResizeObserver notification. Three layers stop that from looping:
    //   1. `roApplying` -- true only for the synchronous duration of the
    //      setSize() call, so a notification that fires re-entrantly out of
    //      that same call is dropped instead of recursing.
    //   2. `lastSize` + a 2px threshold -- sub-pixel layout noise (the kind
    //      that caused this bug) never reaches setSize() at all.
    //   3. requestAnimationFrame coalescing -- a burst of notifications in
    //      one frame collapses into a single resize.
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
  },
};
