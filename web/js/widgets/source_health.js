import { el, fmtRelTime } from "../utils.js";

export default {
  title: "Source health",
  minW: 3,
  minH: 1,
  render(container, { data }) {
    const sources = data?.sources || {};
    const keys = Object.keys(sources);

    if (keys.length === 0) {
      container.appendChild(el("div", { class: "empty-state" }, "No collectors reporting."));
      return;
    }

    const wrap = el("div", { style: "display:flex; flex-wrap:wrap; gap:6px; align-items:flex-start;" });
    for (const key of keys) {
      const s = sources[key] || {};
      let cls = "pill-ok";
      if (s.ok === false) cls = "pill-crit";
      else if (s.stale) cls = "pill-warn";

      const title = s.error
        ? `${key}: ${s.error}`
        : `${key}: ok, last run ${fmtRelTime(s.last_run)}, ${s.duration_ms ?? "?"}ms`;

      wrap.appendChild(
        el("span", { class: `pill ${cls}`, title }, [
          el("span", { class: "pill-dot" }),
          el("span", {}, key),
        ])
      );
    }
    container.appendChild(wrap);
  },
};
