import { el } from "../utils.js";
import { buildDispatchPausedBanner, summarizeRouting, ROUTING_LABELS, ROUTING_PILL_CLASS, DEFAULT_HUMAN_LABELS } from "../bead_routing.js";

// Beads this summary counts: unclaimed beads in the "ready" lane -- the
// ones dispatch would actually act on next. Matches the scope of the "N
// idle beads" investigation this panel exists to explain (in_progress/
// blocked/review/closed beads aren't waiting on routing).
function readyUnclaimed(data) {
  const items = Array.isArray(data?.beads?.items) ? data.beads.items : [];
  const readyIds = new Set(data?.beads?.lanes?.ready || []);
  return items.filter((b) => readyIds.has(b.id) && !b.assignee);
}

function summaryRow(counts) {
  const row = el("div", { class: "routing-summary", title: "Ready, unclaimed beads only -- the ones dispatch would act on next" });
  for (const state of ["routable", "owner", "unroutable"]) {
    row.appendChild(
      el(
        "span",
        { class: `pill ${ROUTING_PILL_CLASS[state]} routing-summary-count`, "data-routing-state": state },
        [el("span", { class: "pill-dot" }), `${ROUTING_LABELS[state]} ${counts[state]}`]
      )
    );
  }
  row.appendChild(el("span", { class: "faint mono routing-summary-total", "data-routing-total": String(counts.total) }, `= ${counts.total} ready & unclaimed`));
  return row;
}

export default {
  title: "Dispatch routes",
  minW: 3,
  minH: 1,
  render(container, { data, layout }) {
    const dispatch = data?.dispatch;
    if (!dispatch) {
      container.appendChild(el("div", { class: "empty-state" }, "No dispatch data."));
      return;
    }

    const wrap = el("div", { style: "display:flex; flex-direction:column; gap:8px;" });

    const banner = buildDispatchPausedBanner(dispatch);
    if (banner) wrap.appendChild(banner);

    const routes = dispatch.routes || [];
    if (routes.length === 0) {
      wrap.appendChild(el("div", { class: "empty-state", style: "padding:6px 0;" }, "No routes configured."));
    } else {
      const list = el("div", { style: "display:flex; flex-wrap:wrap; gap:5px;" });
      for (const r of routes) {
        const cls = r.paused ? "pill-warn" : "pill-ok";
        list.appendChild(
          el(
            "span",
            {
              class: `pill ${cls}`,
              title: [r.paused ? "paused" : "live", r.precheck ? `precheck: ${r.precheck}` : null].filter(Boolean).join(" -- "),
            },
            [el("span", { class: "pill-dot" }), el("span", {}, r.label), el("span", { class: "faint" }, `→${r.kind}`)]
          )
        );
      }
      wrap.appendChild(list);
    }

    const humanLabels = layout?.human_labels || DEFAULT_HUMAN_LABELS;
    const counts = summarizeRouting(readyUnclaimed(data), dispatch, humanLabels);
    wrap.appendChild(summaryRow(counts));

    container.appendChild(wrap);
  },
};
