"""analytics.productivity (briefing Task 2d): commit/line-churn counters from
git log across the same worktrees WorktreesCollector already found.

This runs as its OWN collector on its OWN (slower) interval rather than being
folded into WorktreesCollector.collect(), for one reason: the briefing calls
out that the worktree collector's status/log scan already measured 38s once
and "must not regress" -- adding a second git subprocess per repo (numstat
history, not just current status) to that same pass risks exactly that
regression. Keeping it separate means WorktreesCollector's own duration is
provably unaffected by this file; see the delivery report for both
collectors' measured durations.

Cross-collector key sharing: AnalyticsCollector is the sole writer of the
`analytics` snapshot key (the collector framework replaces a whole top-level
key per collector run -- two collectors writing "analytics" would stomp each
other, exactly the failure RemoteCollector's docstring warns about for
agents/worktrees/usage). So this collector does NOT return "analytics"
itself: it computes its result onto `ctx.latest_productivity` and returns no
top-level keys; AnalyticsCollector reads that shared field on every one of
ITS OWN (faster) polls and folds it in under analytics.productivity, up to
`productivity_interval_s`-stale. Same pattern as ctx.latest_agents /
ctx.usage_by_session / ctx.remote_hosts elsewhere in this codebase.

Fast-retry after an empty worktrees list (delivery fix): on every service
restart this collector's first tick can beat WorktreesCollector's first scan,
so `ctx.latest_worktrees` is still `[]`. Returning early and waiting the full
(600s) `interval_s` before trying again left the productivity panel blank for
ten minutes after every restart. Instead, an empty worktrees list is treated
as "not ready yet": no git subprocess is spawned (a bare list comprehension
over zero worktrees would be a no-op anyway, but skipping the whole aggregate
avoids computing zeros that briefly overwrite `ctx.latest_productivity`'s
previous last-known-good value), and `self.interval_s` is set to a short,
doubling backoff (3s, 6s, 12s, ... capped at the configured steady interval)
so the very next scheduler tick retries soon. As soon as worktrees are
available, `self.interval_s` is restored to the steady interval and the
backoff counter resets, so the expensive `git log --numstat` sweep still only
ever runs on its slow cadence once it has data -- exactly the "don't just
shorten the interval globally" requirement.

Mirrored-repo dedupe: the same repository often exists checked out at more
than one path on this host (e.g. under both ~/repos and a second repo root --
genuine mirrors of the same history,
kept deliberately visible as separate rows in the *worktrees* table so
divergence between them is spottable). Counting both paths' commits into the
productivity rollup double-counts every commit in that shared history. This
collector computes a stable identity per checkout path (root commit SHA,
falling back to the normalized origin URL, falling back to the path itself)
and, when two or more local worktrees share an identity, sweeps only ONE of
them -- the one with the newer HEAD commit -- crediting that repo's commits
once. The identity is cached per path for the collector's lifetime: it never
changes for a given checkout, so it is computed once, not on every sweep.
This dedupe applies ONLY to these aggregate counters, never to the worktrees
table itself (WorktreesCollector is untouched).

Dedup eligibility is gated on `.git` being a real directory, i.e. a
standalone, independent checkout -- NOT a linked `git worktree add` checkout
(whose `.git` is a small text file pointing at the main repo's real gitdir).
This distinction is load-bearing, discovered by running the dedupe against a
fleet with a couple hundred worktrees: the vast majority of a busy fleet's
worktrees are linked `git worktree` checkouts (one per agent/branch, e.g.
`~/repos/some-project/wt-gate`), and ALL linked worktrees of one repo
share that repo's root commit by construction -- root-SHA identity would
collapse dozens of agents' genuinely distinct, branch-scoped commits into a
single row, which is the opposite of what this fix is for. Only real,
independent clones (verified against a handful of repos checked out at more
than one root) are compared by identity; every linked worktree keeps its own
path-scoped identity and is
never merged with anything.

Fixture-repo split: synthetic repos matching configurable glob patterns can
generate commit volume unrelated to product work. Repos matching these patterns
(configured via config/sources.json:excluded_repos as a list of glob patterns,
e.g. `["scratch-*"]`) are reported separately, not silently dropped.
Rather than silently subtracting them from the headline
numbers, this collector reports three views of the same shape: the top-level
fields (unchanged, everything included -- backward compatible), plus
`excluding_fixtures` (product repos only) and `fixtures` (only the matched
repos), so a panel can show product work distinctly without losing the
excluded data.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
from datetime import UTC, datetime, timedelta

from . import BaseCollector

_SEP = "\x1f"
_LOG_FORMAT = f"COMMIT{_SEP}%H{_SEP}%ct{_SEP}%an"
_BY_REPO_N = 30

_RETRY_INITIAL_S = 3.0
_DEFAULT_EXCLUDED_REPOS = ()


async def _git_log_numstat(path: str, since: str, timeout: float) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", path, "--no-optional-locks", "log",
        f"--since={since}", "--numstat", f"--format={_LOG_FORMAT}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    return stdout.decode(errors="replace")


async def _git_simple(path: str, args: list[str], timeout: float) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", path, "--no-optional-locks", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    return stdout.decode(errors="replace").strip()


def _normalize_origin_url(url: str) -> str:
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[: -len(".git")]
    return u.lower()


async def compute_repo_identity(path: str, timeout: float) -> str:
    """Stable identity for a git checkout, used only to dedupe mirrored
    worktrees in the productivity aggregate (see module docstring). The root
    commit SHA is the reliable key -- two clones of the same repo share it
    regardless of remote config -- taking the LAST line of `git rev-list
    --max-parents=0 HEAD` since a history can (rarely) have more than one
    root. Falls back to the normalized origin URL, then to the path itself
    when neither git call succeeds (e.g. no commits yet, or no remote
    configured) -- a path-keyed identity never collides with any other
    checkout's identity, so that repo is simply never deduped, which is the
    safe failure mode."""
    roots = await _git_simple(path, ["rev-list", "--max-parents=0", "HEAD"], timeout)
    if roots:
        last_line = roots.splitlines()[-1].strip()
        if last_line:
            return f"sha:{last_line}"
    origin = await _git_simple(path, ["remote", "get-url", "origin"], timeout)
    if origin:
        normalized = _normalize_origin_url(origin)
        if normalized:
            return f"origin:{normalized}"
    return f"path:{path}"


def is_standalone_checkout(path: str) -> bool:
    """True for a real, independent git checkout (`.git` is a directory) --
    false for a linked `git worktree add` checkout (`.git` is a small text
    file pointing at the main repo's real gitdir). See the module docstring:
    only standalone checkouts are eligible for identity-based dedup."""
    return os.path.isdir(os.path.join(path, ".git"))


def pick_representative(worktrees: list[dict]) -> dict:
    """Among worktrees sharing one repo identity, prefer the one with the
    newer HEAD commit (`last_commit_at`, ISO 8601 -- safe to compare
    lexicographically). Missing timestamps sort lowest; ties break on path
    for a deterministic choice."""
    return max(worktrees, key=lambda w: (w.get("last_commit_at") or "", w["path"]))


def parse_log_numstat(output: str) -> list[dict]:
    """Parse `git log --numstat --format=COMMIT<sep>%H<sep>%ct<sep>%an` output
    into one dict per commit: {sha, epoch_s, author, lines_added,
    lines_removed, files_changed}. A binary file's numstat line uses "-" for
    both counts (per git's documented format) -- counted as a changed file
    but contributes 0 to the line totals, since "-" isn't a number."""
    commits: list[dict] = []
    current: dict | None = None
    for line in output.splitlines():
        if line.startswith("COMMIT" + _SEP):
            parts = line.split(_SEP)
            if len(parts) != 4:
                continue
            _, sha, epoch_s, author = parts
            try:
                epoch = int(epoch_s)
            except ValueError:
                continue
            current = {
                "sha": sha, "epoch_s": epoch, "author": author,
                "lines_added": 0, "lines_removed": 0, "files_changed": 0,
            }
            commits.append(current)
            continue
        if current is None or "\t" not in line:
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        added, removed, _path = fields[0], fields[1], fields[2]
        current["files_changed"] += 1
        if added.isdigit():
            current["lines_added"] += int(added)
        if removed.isdigit():
            current["lines_removed"] += int(removed)
    return commits


def aggregate_commits(commits_by_repo: dict[str, list[dict]], now: datetime) -> dict:
    cutoff_7d = (now - timedelta(days=7)).timestamp()
    cutoff_30d = (now - timedelta(days=30)).timestamp()

    commits_7d = commits_30d = 0
    lines_added_7d = lines_removed_7d = files_changed_7d = 0
    by_repo: dict[str, dict] = {}

    for repo, commits in commits_by_repo.items():
        repo_agg = by_repo.setdefault(repo, {"commits": 0, "lines_added": 0, "lines_removed": 0})
        for c in commits:
            epoch = c["epoch_s"]
            if epoch >= cutoff_30d:
                commits_30d += 1
            if epoch >= cutoff_7d:
                commits_7d += 1
                lines_added_7d += c["lines_added"]
                lines_removed_7d += c["lines_removed"]
                files_changed_7d += c["files_changed"]
                repo_agg["commits"] += 1
                repo_agg["lines_added"] += c["lines_added"]
                repo_agg["lines_removed"] += c["lines_removed"]

    by_repo_list = sorted(
        ({"repo": r, **agg} for r, agg in by_repo.items() if agg["commits"] > 0),
        key=lambda r: r["commits"], reverse=True,
    )
    truncated = len(by_repo_list) > _BY_REPO_N
    by_repo_list = by_repo_list[:_BY_REPO_N]

    return {
        "commits_7d": commits_7d,
        "commits_30d": commits_30d,
        "lines_added_7d": lines_added_7d,
        "lines_removed_7d": lines_removed_7d,
        "files_changed_7d": files_changed_7d,
        "by_repo": by_repo_list,
        "truncated": truncated,
    }


def merge_by_repo(local: dict, remote_rows) -> dict:
    """Fold each remote host's pre-aggregated 7d by_repo rows (from
    remote_productivity_buckets, day-granularity, summed over the window by
    the caller's store query) into the local aggregate's commits_7d /
    lines_*_7d / by_repo. commits_30d has no remote contribution -- the
    remote probe only ships a 7d-equivalent window (see remote_probe.py) to
    keep the probe cheap; documented in the delivery report."""
    merged = {**local, "by_repo": [dict(r) for r in local["by_repo"]]}
    by_repo_idx = {r["repo"]: r for r in merged["by_repo"]}
    for r in remote_rows:
        repo = r["repo"]
        merged["commits_7d"] += r["commits"]
        merged["lines_added_7d"] += r["lines_added"]
        merged["lines_removed_7d"] += r["lines_removed"]
        merged["files_changed_7d"] += r["files_changed"]
        if repo in by_repo_idx:
            by_repo_idx[repo]["commits"] += r["commits"]
            by_repo_idx[repo]["lines_added"] += r["lines_added"]
            by_repo_idx[repo]["lines_removed"] += r["lines_removed"]
        else:
            entry = {"repo": repo, "commits": r["commits"], "lines_added": r["lines_added"],
                      "lines_removed": r["lines_removed"]}
            merged["by_repo"].append(entry)
            by_repo_idx[repo] = entry
    merged["by_repo"] = sorted(merged["by_repo"], key=lambda r: r["commits"], reverse=True)
    if len(merged["by_repo"]) > _BY_REPO_N:
        merged["truncated"] = True
        merged["by_repo"] = merged["by_repo"][:_BY_REPO_N]
    return merged


def is_excluded_repo(repo: str, patterns) -> bool:
    return any(fnmatch.fnmatch(repo, pat) for pat in patterns)


def split_commits_by_exclusion(
    commits_by_repo: dict[str, list[dict]], patterns
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Partition a repo->commits map into (included, excluded) using the
    excluded_repos glob patterns. Excluded repos are never dropped, just
    reported separately -- see module docstring."""
    included: dict[str, list[dict]] = {}
    excluded: dict[str, list[dict]] = {}
    for repo, commits in commits_by_repo.items():
        (excluded if is_excluded_repo(repo, patterns) else included)[repo] = commits
    return included, excluded


def split_remote_rows_by_exclusion(remote_rows, patterns) -> tuple[list, list]:
    included = [r for r in remote_rows if not is_excluded_repo(r["repo"], patterns)]
    excluded = [r for r in remote_rows if is_excluded_repo(r["repo"], patterns)]
    return included, excluded


def _attach_chosen_paths(result: dict, chosen_path_by_repo: dict[str, str]) -> None:
    for entry in result["by_repo"]:
        path = chosen_path_by_repo.get(entry["repo"])
        if path:
            entry["path"] = path


class ProductivityCollector(BaseCollector):
    name = "productivity"
    interval_s = 600.0

    def __init__(
        self, ctx=None, store=None, host: str = "localhost",
        worker_pool: int = 32, git_timeout_s: float = 15.0, since: str = "30 days ago",
        excluded_repos: list[str] | None = None,
    ):
        super().__init__(ctx)
        self.store = store
        self.host = host
        self.worker_pool = worker_pool
        self.git_timeout_s = git_timeout_s
        self.since = since
        self.excluded_repos = (
            list(excluded_repos) if excluded_repos is not None else list(_DEFAULT_EXCLUDED_REPOS)
        )
        # fast-retry state (see module docstring). `_steady_interval_s` is
        # captured lazily on the first collect() call rather than in
        # __init__, because main.py constructs this collector and THEN
        # overrides `.interval_s` from config before the scheduler ever
        # calls collect() -- capturing here would freeze the class default
        # (600.0) instead of the configured value.
        self._steady_interval_s: float | None = None
        self._retry_delay_s = _RETRY_INITIAL_S
        self._identity_cache: dict[str, str] = {}

    async def collect(self) -> dict:
        if self._steady_interval_s is None:
            self._steady_interval_s = self.interval_s

        # The retry decision is based on the RAW worktrees list, not the
        # host-filtered one: an empty raw list means WorktreesCollector
        # hasn't produced its first scan yet (the bug this fixes), which is
        # a different situation from "WorktreesCollector has run and this
        # host genuinely has zero local worktrees" (e.g. a host that only
        # ever sees remote-hosted worktrees) -- that second case is a
        # legitimate zero, not a "not ready yet", and must still settle onto
        # the steady cadence and report an honest (possibly all-zero, but
        # remote-merged) result rather than retrying forever.
        worktrees = self.ctx.latest_worktrees if self.ctx is not None else []
        if not worktrees:
            delay = min(self._retry_delay_s, self._steady_interval_s)
            self.interval_s = delay
            self._retry_delay_s = min(self._retry_delay_s * 2, self._steady_interval_s)
            return {}

        self.interval_s = self._steady_interval_s
        self._retry_delay_s = _RETRY_INITIAL_S

        local_worktrees = [
            w for w in worktrees
            if w.get("host", self.host) == self.host and w.get("path")
        ]

        sem = asyncio.Semaphore(max(1, self.worker_pool))

        # Dedup only ever considers standalone checkouts (see module
        # docstring) -- linked `git worktree add` checkouts are never
        # identity-compared, so they skip the git rev-list/remote-url calls
        # entirely and are always swept individually.
        standalone = [w for w in local_worktrees if is_standalone_checkout(w["path"])]
        linked = [w for w in local_worktrees if not is_standalone_checkout(w["path"])]

        async def identity_for(w: dict) -> tuple[str, str]:
            path = w["path"]
            cached = self._identity_cache.get(path)
            if cached is not None:
                return path, cached
            async with sem:
                ident = await compute_repo_identity(path, self.git_timeout_s)
            self._identity_cache[path] = ident
            return path, ident

        identities = dict(await asyncio.gather(*(identity_for(w) for w in standalone)))

        groups: dict[str, list[dict]] = {}
        for w in standalone:
            groups.setdefault(identities[w["path"]], []).append(w)
        chosen = [pick_representative(g) for g in groups.values()] + linked
        chosen_path_by_repo = {(w.get("repo") or w["path"]): w["path"] for w in chosen}

        async def bound(w: dict):
            async with sem:
                return await _git_log_numstat(w["path"], self.since, self.git_timeout_s)

        outputs = await asyncio.gather(*(bound(w) for w in chosen))

        commits_by_repo: dict[str, list[dict]] = {}
        for w, output in zip(chosen, outputs, strict=True):
            if not output:
                continue
            repo = w.get("repo") or w["path"]
            commits = parse_log_numstat(output)
            if commits:
                commits_by_repo.setdefault(repo, []).extend(commits)

        now = datetime.now(UTC)
        included_commits, excluded_commits = split_commits_by_exclusion(commits_by_repo, self.excluded_repos)
        local_all = aggregate_commits(commits_by_repo, now)
        local_excluding = aggregate_commits(included_commits, now)
        local_fixtures = aggregate_commits(excluded_commits, now)

        if self.store is not None:
            since_day = (now - timedelta(days=7)).strftime("%Y-%m-%d")
            remote_rows = list(self.store.remote_productivity_grouped(since_day))
        else:
            remote_rows = []
        remote_included, remote_excluded = split_remote_rows_by_exclusion(remote_rows, self.excluded_repos)

        result = merge_by_repo(local_all, remote_rows)
        result["excluding_fixtures"] = merge_by_repo(local_excluding, remote_included)
        result["fixtures"] = merge_by_repo(local_fixtures, remote_excluded)
        result["excluded_repo_patterns"] = list(self.excluded_repos)

        _attach_chosen_paths(result, chosen_path_by_repo)
        _attach_chosen_paths(result["excluding_fixtures"], chosen_path_by_repo)
        _attach_chosen_paths(result["fixtures"], chosen_path_by_repo)

        if self.ctx is not None:
            self.ctx.latest_productivity = result

        return {}
