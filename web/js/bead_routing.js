import { el, fmtDuration } from "./utils.js";

// Shared "why is this bead idle" logic, used by bead_table.js, bead_board.js
// and dispatch_routes.js so the three panels agree on one answer instead of
// three different heuristics.
//
// Background: fleet dispatch wakes an agent for a bead only when (1) global
// dispatch isn't paused, (2) the bead carries a label that matches a
// configured route, and (3) that route isn't itself paused. A bead can also
// carry a label that means "a human owns this on purpose" -- that is not
// stuck, it is working as designed. Age alone can't tell these apart; this
// module can, from `dispatch.routes[]` + `dispatch.paused_all` +
// `beads.items[].labels` alone (no backend change).
//
// Three per-bead states:
//   "owner"      -- a label marks this as human-owned (a route with
//                    kind:"human" in dispatch.routes, e.g. the fixture's
//                    "owner" route, OR a label listed in the deployment's
//                    configured `human_labels`, e.g. a fleet lead's personal
//                    label -- see config/layout.json's top-level
//                    `human_labels` and fleet-flow rules: human-labeled beads
//                    belong to that person. Never claim one; never wake
//                    anyone for one."). Waiting on a person is
//                    correct, not stale.
//   "routable"   -- a label matches a live (unpaused, non-human) route.
//                    Names the route.
//   "unroutable" -- no label maps to any route that will ever wake an
//                    agent, whether the matching route is paused or there
//                    is no matching route at all.
//
// Deliberately independent of `dispatch.paused_all`: that's a separate,
// global "nothing is happening right now" fact (see pausedDuration() /
// the dispatch-paused banner). A bead stays "routable" during a global
// pause -- it's a structural property, not a snapshot of this instant.

export const DEFAULT_HUMAN_LABELS = [];

export function humanRouteLabels(dispatch) {
  const routes = dispatch?.routes || [];
  return new Set(routes.filter((r) => r.kind === "human").map((r) => r.label));
}

export function classifyBeadRouting(bead, dispatch, humanLabels = DEFAULT_HUMAN_LABELS) {
  const labels = bead?.labels || [];
  const routes = dispatch?.routes || [];
  const humanSet = humanRouteLabels(dispatch);
  for (const l of humanLabels || []) humanSet.add(l);

  for (const l of labels) {
    if (humanSet.has(l)) return { state: "owner", label: l, reason: null };
  }

  const live = routes.find((r) => r.kind !== "human" && !r.paused && labels.includes(r.label));
  if (live) return { state: "routable", label: live.label, reason: null };

  const paused = routes.find((r) => r.kind !== "human" && r.paused && labels.includes(r.label));
  if (paused) return { state: "unroutable", label: paused.label, reason: "route-paused" };

  return { state: "unroutable", label: null, reason: "no-route" };
}

export function summarizeRouting(beads, dispatch, humanLabels = DEFAULT_HUMAN_LABELS) {
  const out = { routable: 0, owner: 0, unroutable: 0, total: 0 };
  for (const b of beads || []) {
    out.total += 1;
    out[classifyBeadRouting(b, dispatch, humanLabels).state] += 1;
  }
  return out;
}

// Route/label names are operator-chosen and unbounded ("needs-opencode",
// "sample-client-weekly", ...); pills are laid out in tight, fixed-width
// contexts (a table column, a ~210px kanban card). Truncate defensively so a
// long name degrades to "…" instead of spilling into the next cell -- the
// full name is always still available in the tooltip.
export function truncateLabel(label, max = 16) {
  if (!label) return label;
  return label.length > max ? `${label.slice(0, max - 1)}…` : label;
}

export const ROUTING_LABELS = { routable: "ROUTABLE", owner: "HUMAN", unroutable: "UNROUTABLE" };
export const ROUTING_PILL_CLASS = { routable: "pill-ok", owner: "pill-info", unroutable: "pill-warn" };

export function routingTooltip(cls) {
  if (cls.state === "owner") return `Owned by a human via label "${cls.label}" -- correct to wait, not stale.`;
  if (cls.state === "routable") return `Routes to "${cls.label}" -- an agent will be woken for this when dispatch runs.`;
  if (cls.reason === "route-paused") return `Label "${cls.label}" matches a route, but that route is paused right now.`;
  return "No label on this bead maps to any configured dispatch route. Nobody will ever be woken for it -- it needs a routing label.";
}

// Why a specific, currently-unclaimed bead is sitting idle, in priority
// order: a human-owned bead is never "idle" in the alarming sense; a global
// pause explains ALL of it, even for otherwise-routable beads; only then
// does "unroutable" apply. Claimed beads (in progress) aren't a routing
// question at all. Returns null when there's nothing noteworthy to say
// (routable, unpaused, plain old age).
export function idleReason(bead, dispatch, humanLabels = DEFAULT_HUMAN_LABELS) {
  if (bead?.assignee) return null;
  const cls = classifyBeadRouting(bead, dispatch, humanLabels);
  if (cls.state === "owner") return null;
  if (dispatch?.paused_all) {
    return { key: "paused", text: "dispatch paused", detail: "Fleet dispatch is off. No agent will be woken while it stays paused, routable or not." };
  }
  if (cls.state === "unroutable") {
    return { key: "unroutable", text: "unroutable", detail: routingTooltip(cls) };
  }
  return null;
}

// How long dispatch has been continuously paused, derived from the trailing
// run of "PAUSED(ALL)" lines in dispatch.recent (the fleet-dispatch.log
// tail, capped server-side -- currently 50 lines / ~25h at the 30-minute
// tick this log uses). When the run fills the whole array, that's a LOWER
// BOUND on the true pause start, not the true start time -- `lowerBound:
// true` tells the caller to word it as "at least".
export function pausedDuration(dispatch, nowMs = Date.now()) {
  const recent = dispatch?.recent || [];
  if (!dispatch?.paused_all || recent.length === 0) return null;
  let sinceIso = null;
  let reachedStart = true;
  for (let i = recent.length - 1; i >= 0; i--) {
    const line = recent[i]?.line || "";
    if (!/PAUSED\(ALL\)/.test(line)) {
      reachedStart = false;
      break;
    }
    sinceIso = recent[i].t;
  }
  if (!sinceIso) return null;
  const sinceMs = new Date(sinceIso).getTime();
  if (Number.isNaN(sinceMs)) return null;
  return { sinceIso, ms: Math.max(0, nowMs - sinceMs), lowerBound: reachedStart };
}

// Prominent, hard-to-miss banner for the top of a bead panel, shown only
// while `dispatch.paused_all` is true. Not a pill -- a wall-display-legible
// block, because a small status pill is exactly what let 149 idle beads
// read as "stale queue" instead of "dispatcher switched off". Returns null
// (render nothing) when dispatch isn't paused.
export function buildDispatchPausedBanner(dispatch) {
  if (!dispatch?.paused_all) return null;
  const dur = pausedDuration(dispatch);
  let sub = "Beads below are not stale -- they are waiting on the dispatcher, not forgotten.";
  if (dur) {
    const durText = `${dur.lowerBound ? "at least " : ""}${fmtDuration(dur.ms / 1000)}`;
    const since = dur.sinceIso ? dur.sinceIso.replace("T", " ").slice(0, 16) + " UTC" : null;
    sub = `Paused for ${durText}${since ? ` (since ${since})` : ""}. ${sub}`;
  }
  return el("div", { class: "dispatch-paused-banner", role: "alert" }, [
    el("span", { class: "dispatch-paused-banner-icon", "aria-hidden": "true" }, "⏸"),
    el("div", { class: "dispatch-paused-banner-text" }, [
      el("div", { class: "dispatch-paused-banner-title" }, "DISPATCH IS PAUSED — no agent will be woken for any bead"),
      el("div", { class: "dispatch-paused-banner-sub" }, sub),
    ]),
  ]);
}
