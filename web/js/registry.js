// Maps a panel "type" string (from config/layout.json) to its widget module.
// Adding a widget type: create web/js/widgets/<type>.js exporting the widget
// contract (see README), then add one line here. That is the only JS edit
// ever required -- panels themselves are pure data (config/layout.json).

import agent_grid from "./widgets/agent_grid.js";
import bead_board from "./widgets/bead_board.js";
import bead_table from "./widgets/bead_table.js";
import spend_summary from "./widgets/spend_summary.js";
import spend_timeline from "./widgets/spend_timeline.js";
import model_split from "./widgets/model_split.js";
import worktree_table from "./widgets/worktree_table.js";
import activity_feed from "./widgets/activity_feed.js";
import system_gauges from "./widgets/system_gauges.js";
import source_health from "./widgets/source_health.js";
import stat_row from "./widgets/stat_row.js";
import burn_gauge from "./widgets/burn_gauge.js";
import dispatch_routes from "./widgets/dispatch_routes.js";
import commit_feed from "./widgets/commit_feed.js";
import markdown_note from "./widgets/markdown_note.js";
import budget_bar from "./widgets/budget_bar.js";
import error_patterns from "./widgets/error_patterns.js";
import tool_usage from "./widgets/tool_usage.js";
import subagent_cost from "./widgets/subagent_cost.js";
import productivity from "./widgets/productivity.js";
import host_health from "./widgets/host_health.js";
import usage_history from "./widgets/usage_history.js";
import provider_quota from "./widgets/provider_quota.js";

const registry = {
  agent_grid,
  bead_board,
  bead_table,
  spend_summary,
  spend_timeline,
  model_split,
  worktree_table,
  activity_feed,
  system_gauges,
  source_health,
  stat_row,
  burn_gauge,
  dispatch_routes,
  commit_feed,
  markdown_note,
  budget_bar,
  error_patterns,
  tool_usage,
  subagent_cost,
  productivity,
  host_health,
  usage_history,
  provider_quota,
};

// Which top-level snapshot keys each widget type reads. Used by the SSE
// patch handler to decide which panels to re-render when a dotted patch
// path lands. Conservative (over-inclusive) is fine; missing an entry just
// means that widget re-renders on every patch, which is harmless.
export const dependencies = {
  agent_grid: ["agents"],
  bead_board: ["beads", "dispatch"],
  bead_table: ["beads", "dispatch"],
  spend_summary: ["usage"],
  spend_timeline: ["usage"],
  model_split: ["usage"],
  worktree_table: ["worktrees", "agents"],
  activity_feed: ["events"],
  system_gauges: ["system"],
  source_health: ["sources"],
  stat_row: null, // null = depends on everything (arbitrary dotted paths via options)
  burn_gauge: ["usage"],
  dispatch_routes: ["dispatch", "beads"],
  commit_feed: ["worktrees"],
  markdown_note: [],
  budget_bar: ["usage"],
  error_patterns: ["analytics"],
  tool_usage: ["analytics"],
  subagent_cost: ["analytics"],
  productivity: ["analytics"],
  host_health: ["hosts", "usage"],
  // usage_history fetches its own data from /api/history/usage (not the
  // snapshot), so an SSE patch never has anything for it to react to. It
  // still gets a full render() on every snapshot resync (~60s), which is
  // the "re-render retries the fetch" behaviour called for in the build
  // contract.
  usage_history: [],
  // provider_quota fetches its own data from /api/quota (never the
  // snapshot, and GET /api/quota never makes a live provider call -- see
  // web/js/widgets/provider_quota.js). Same "no patch dependency, re-render
  // retries the fetch on snapshot resync" contract as usage_history above.
  provider_quota: [],
};

export function getWidget(type) {
  return registry[type] || null;
}

export default registry;
