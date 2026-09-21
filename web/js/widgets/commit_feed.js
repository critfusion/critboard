import { el, fmtRelTime, mobileLimit, renderWithShowAll } from "../utils.js";
import { openWorktreeModal } from "../detail.js";

function buildRow(w, data) {
  const row = el("div", {
    class: "clickable-row",
    tabindex: "0",
    role: "button",
    "aria-label": `Open detail for commit in ${w.repo}`,
    style: "padding:5px 2px; border-bottom:1px solid var(--color-grid-line);",
  });
  row.addEventListener("click", () => openWorktreeModal(w, data, row));
  row.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openWorktreeModal(w, data, row);
    }
  });
  row.appendChild(
    el("div", { style: "display:flex; justify-content:space-between; gap:8px;" }, [
      el("span", { class: "mono truncate", style: "font-size:11px;" }, w.last_commit_msg || "(no message)"),
      el("span", { class: "faint mono", style: "font-size:9px; flex:none;" }, fmtRelTime(w.last_commit_at)),
    ])
  );
  row.appendChild(
    el("div", { class: "dim mono", style: "font-size:9px;" },
      `${w.repo} @ ${(w.head || "").slice(0, 7)} · ${w.last_commit_author || "unknown"}`)
  );
  return row;
}

// Derived from worktrees[] (each carries its last commit) -- the snapshot
// has no separate commits[] array, so this is the most-recent-commit-per-
// worktree view, newest first.
export default {
  title: "Commits",
  minW: 3,
  minH: 2,
  render(container, { data, options, panel, breakpoint }) {
    const maxItems = options?.max_items ?? 15;
    const worktrees = Array.isArray(data?.worktrees) ? data.worktrees.slice() : [];
    const withCommits = worktrees.filter((w) => w.last_commit_at);
    withCommits.sort((a, b) => new Date(b.last_commit_at) - new Date(a.last_commit_at));
    const list = withCommits.slice(0, maxItems);

    if (list.length === 0) {
      container.appendChild(el("div", { class: "empty-state" }, "No commits found."));
      return;
    }

    const buildWrap = (items) => {
      const wrap = el("div", { style: "display:flex; flex-direction:column; gap:1px;" });
      for (const w of items) wrap.appendChild(buildRow(w, data));
      return wrap;
    };

    const limit = breakpoint === "mobile" ? mobileLimit(panel, 10) : null;
    renderWithShowAll(container, list, limit, buildWrap);
  },
};
