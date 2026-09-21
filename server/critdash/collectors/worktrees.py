"""worktrees collector: git status for every repo dir under sources.json:repo_roots.

Scans each root for directories containing a `.git` (bounded depth, prunes
heavy/irrelevant dirs), runs `git -C <dir> --no-optional-locks ...` concurrently
through a bounded worker pool. A repo that times out is skipped, not fatal.

Per-repo git cost: a real fleet's worktree count can run into the hundreds
(verified against `find <roots> -maxdepth 4 -name .git`, cross-checked
against the prune-pattern list -- zero matches, they are all real per-agent
worktrees, not vendored checkouts). The original slowness was not repo count
or scan depth -- it was
issuing 5-6 separate `git` subprocesses per repo (rev-parse HEAD, rev-parse
--short HEAD, status --porcelain=v1, rev-parse upstream, rev-list
ahead/behind, log -1). `git status --porcelain=v2 --branch` returns branch,
upstream, ahead/behind counts, AND the file-status lines in one call, so
`collect_one` now does exactly 2 subprocess calls per repo (status + one
`git log -1`), not 5-6.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

from . import BaseCollector, now_iso

# Named directories to prune during the scan. This is cheap insurance against
# vendored/build checkouts that could otherwise be walked into and mistaken
# for separate worktrees -- on a real fleet it is typically a no-op (verified
# on one: zero of its real .git dirs sit under any of these), because a
# repo's own subtree is never descended into once its `.git` is found (see
# find_git_dirs). Configurable via sources.json:worktree_skip_dirs.
DEFAULT_PRUNE_DIRS = frozenset({
    "node_modules", ".venv", "venv", "vendor", "site-packages", ".cache",
    ".pub-cache", "build", "dist", "target", ".tox", ".gradle",
})
# Configurable via sources.json:repo_scan_depth. Real worktrees can go as
# deep as root/group/wt-name (depth 2 under a repos root) and
# root/repos/group/wt-name (depth 3 under its parent) -- 4 leaves headroom
# without needing to be raised.
_DEFAULT_MAX_DEPTH = 4


def find_git_dirs(
    root: str,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    prune_dirs: frozenset[str] | set[str] | None = None,
) -> list[str]:
    """Find every directory containing a `.git` under `root`, up to
    `max_depth` levels down. A directory is checked for `.git` BEFORE any
    pruning is applied, so a repo sitting exactly at max_depth is still
    found. Once a `.git` is found, that directory is never descended into
    further -- a repo's own submodules or vendored copies are not separate
    worktrees. Directories named in `prune_dirs`, and any dotted directory
    (other than the `.git` entry itself, which triggers detection rather
    than being walked), are pruned before descending.
    """
    prune = set(prune_dirs) if prune_dirs is not None else set(DEFAULT_PRUNE_DIRS)
    found: list[str] = []
    if not os.path.isdir(root):
        return found
    root_depth = root.rstrip("/").count("/")

    for dirpath, dirnames, _filenames in os.walk(root):
        depth = dirpath.rstrip("/").count("/") - root_depth
        is_repo = ".git" in dirnames or os.path.isfile(os.path.join(dirpath, ".git"))
        if is_repo:
            found.append(dirpath)
            dirnames[:] = []  # never descend into a found repo's own subtree
            continue
        if depth >= max_depth:
            dirnames[:] = []  # hit the depth limit -- don't go any deeper from here
            continue
        dirnames[:] = [d for d in dirnames if d not in prune and not d.startswith(".")]
    return found


async def _git(dir_: str, args: list[str], timeout: float) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", dir_, "--no-optional-locks", *args,
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
    # NOTE: rstrip only -- `git status --porcelain` lines carry meaningful
    # leading characters (e.g. " M file" / ".M file" = unstaged modify); a
    # blanket .strip() would eat that first line's leading column and
    # misclassify it.
    return stdout.decode(errors="replace").rstrip("\n")


def _parse_status_v2(porcelain: str) -> tuple[int, int, int, str | None, str | None, int, int]:
    """Parse `git status --porcelain=v2 --branch` output into
    (dirty, untracked, staged, branch, upstream, ahead, behind).

    v2 uses '.' (not ' ') for the "no change in this slot" placeholder in
    the XY status pair -- verified against real `git status --porcelain=v2`
    output, this differs from porcelain=v1.
    """
    dirty = untracked = staged = ahead = behind = 0
    branch: str | None = None
    upstream: str | None = None
    if not porcelain:
        return dirty, untracked, staged, branch, upstream, ahead, behind
    for line in porcelain.splitlines():
        if not line:
            continue
        if line.startswith("# branch.head "):
            head_field = line[len("# branch.head "):]
            branch = "HEAD" if head_field == "(detached)" else head_field
            continue
        if line.startswith("# branch.upstream "):
            upstream = line[len("# branch.upstream "):]
            continue
        if line.startswith("# branch.ab "):
            for part in line[len("# branch.ab "):].split():
                if part.startswith("+"):
                    ahead = int(part[1:])
                elif part.startswith("-"):
                    behind = int(part[1:])
            continue
        if line.startswith("#"):
            continue
        if line.startswith("? "):
            untracked += 1
            continue
        if line.startswith("! "):
            continue  # ignored entries; not requested (--ignored not passed) but skip defensively
        # ordinary "1 XY ...", rename/copy "2 XY ...", unmerged "u XY ..."
        if len(line) < 4:
            continue
        x, y = line[2], line[3]
        if x != ".":
            staged += 1
        if y != ".":
            dirty += 1
    return dirty, untracked, staged, branch, upstream, ahead, behind


async def collect_one(dir_: str, root: str, timeout: float) -> dict | None:
    status_raw = await _git(dir_, ["status", "--porcelain=v2", "--branch"], timeout)
    if status_raw is None:
        return None
    dirty, untracked, staged, branch, upstream, ahead, behind = _parse_status_v2(status_raw)

    log = await _git(
        dir_, ["log", "-1", "--format=%h\x1f%H\x1f%ct\x1f%an\x1f%s"], timeout
    )
    head = last_commit_sha = last_commit_at = last_commit_msg = last_commit_author = None
    stale_days = None
    if log:
        parts = log.split("\x1f")
        if len(parts) == 5:
            head, last_commit_sha, epoch_s, last_commit_author, last_commit_msg = parts
            try:
                dt = datetime.fromtimestamp(int(epoch_s), tz=UTC)
                last_commit_at = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                stale_days = (datetime.now(UTC) - dt).days
            except ValueError:
                pass

    if branch is None:
        branch = head or "HEAD"

    return {
        "path": dir_,
        "repo": os.path.basename(dir_.rstrip("/")),
        "root": root,
        "branch": branch,
        "head": head,
        "dirty": dirty,
        "untracked": untracked,
        "staged": staged,
        "ahead": ahead,
        "behind": behind,
        "upstream": upstream,
        "last_commit_at": last_commit_at,
        "last_commit_msg": last_commit_msg,
        "last_commit_author": last_commit_author,
        "agents": [],
        "stale_days": stale_days,
        "_last_commit_sha": last_commit_sha,  # internal, stripped before publish
    }


class WorktreesCollector(BaseCollector):
    name = "worktrees"
    interval_s = 60.0

    def __init__(
        self,
        ctx=None,
        repo_roots: list[str] | None = None,
        worker_pool: int = 32,
        git_timeout_s: float = 5.0,
        store=None,
        max_depth: int = _DEFAULT_MAX_DEPTH,
        prune_dirs: frozenset[str] | set[str] | None = None,
        host: str = "localhost",
    ):
        super().__init__(ctx)
        self.repo_roots = repo_roots or []
        self.worker_pool = worker_pool
        self.git_timeout_s = git_timeout_s
        self.store = store
        self.max_depth = max_depth
        self.prune_dirs = set(prune_dirs) if prune_dirs is not None else set(DEFAULT_PRUNE_DIRS)
        self.host = host
        self._prev_heads: dict[str, str] = {}

    async def collect(self) -> dict:
        # repo_roots can legitimately overlap (e.g. sources.json lists both
        # a parent dir and a "repos" subdir under it, since non-repo content
        # can live directly under the parent too). Without dedup, any repo
        # under a nested root gets git-queried once per enclosing root --
        # dropped real-world first-run time from ~68s to well under that.
        seen: dict[str, str] = {}
        for root in sorted(self.repo_roots, key=len, reverse=True):  # most specific root wins
            for d in find_git_dirs(root, max_depth=self.max_depth, prune_dirs=self.prune_dirs):
                seen.setdefault(d, root)
        dirs = list(seen.items())

        sem = asyncio.Semaphore(self.worker_pool)

        async def bound(dir_, root):
            async with sem:
                return await collect_one(dir_, root, self.git_timeout_s)

        results = await asyncio.gather(*(bound(d, r) for d, r in dirs))
        worktrees = [r for r in results if r is not None]

        # join LOCAL agent cwd -> longest matching LOCAL worktree path prefix.
        # Host-filtered: ctx.latest_agents can hold remote agents too once
        # AgentsCollector starts merging them in, and a remote agent's cwd
        # (e.g. another host's "/home/user/repos/foo") must never match a
        # same-looking path scanned on this host.
        agents = [
            a for a in (self.ctx.latest_agents if self.ctx is not None else [])
            if a.get("host", self.host) == self.host
        ]
        for wt in worktrees:
            matched = []
            for agent in agents:
                cwd = agent.get("cwd") or ""
                if cwd == wt["path"] or cwd.startswith(wt["path"].rstrip("/") + "/"):
                    matched.append((len(wt["path"]), agent.get("id")))
            wt["agents"] = [aid for _len, aid in matched if aid]
            wt["host"] = self.host
            wt["stale"] = False

        self._detect_commits(worktrees)

        for wt in worktrees:
            wt.pop("_last_commit_sha", None)

        # Fleet-wide merge: remote worktrees already carry their own agents[]
        # join (done inside remote_probe.py against that SAME host's agent
        # list), so they're appended as-is aside from a live-recomputed
        # `stale` flag -- see AgentsCollector's matching comment for why this
        # is recomputed here rather than baked in by RemoteCollector.
        remote_hosts = self.ctx.remote_hosts if self.ctx is not None else {}
        merged_worktrees = list(worktrees)
        for rh in remote_hosts.values():
            stale = not rh.get("ok", False)
            for w in rh.get("worktrees", []):
                w2 = dict(w)
                w2["stale"] = stale
                merged_worktrees.append(w2)

        if self.ctx is not None:
            self.ctx.latest_worktrees = merged_worktrees

        return {"worktrees": merged_worktrees}

    def _detect_commits(self, worktrees: list[dict]) -> None:
        if self.store is None:
            return
        ts = now_iso()
        current: dict[str, str] = {}
        for wt in worktrees:
            sha = wt.get("_last_commit_sha")
            if not sha:
                continue
            current[wt["path"]] = sha
            prev_sha = self._prev_heads.get(wt["path"])
            if prev_sha is not None and prev_sha != sha:
                self.store.add_event(
                    ts, "commit", "info",
                    f"{wt['repo']}@{wt['branch']}: {wt.get('last_commit_msg') or sha[:7]}",
                    wt["repo"],
                )
        self._prev_heads = current
