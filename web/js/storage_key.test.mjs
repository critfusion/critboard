// Plain-node test for the shared widget storage-key helper in utils.js.
// Run: node web/js/storage_key.test.mjs  (server/tests/test_dedup.py runs it under pytest).
// The keys must stay byte-identical: a changed key drops every user's saved
// table sort/filter and usage-history choices.

import assert from "node:assert/strict";
import { panelStorageKey } from "./utils.js";

assert.equal(panelStorageKey("table", "beads"), "critdash.table.beads");
assert.equal(panelStorageKey("table", "worktrees-2"), "critdash.table.worktrees-2");
assert.equal(panelStorageKey("usage_history", "u1"), "critdash.usage_history.u1");
console.log("3 passed");
