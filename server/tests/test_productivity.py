"""productivity collector (briefing Task 2d): git log --numstat parsing,
7d/30d windowed aggregation, and remote by_repo merge."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from critdash.collectors.productivity import (
    ProductivityCollector,
    aggregate_commits,
    compute_repo_identity,
    is_excluded_repo,
    is_standalone_checkout,
    merge_by_repo,
    parse_log_numstat,
    pick_representative,
    split_commits_by_exclusion,
)

SEP = "\x1f"


def _commit_line(sha, epoch, author="crit"):
    return f"COMMIT{SEP}{sha}{SEP}{epoch}{SEP}{author}"


# -- parse_log_numstat ---------------------------------------------------------


def test_parse_log_numstat_single_commit():
    output = "\n".join([
        _commit_line("abc123", 1758196800),
        "",
        "10\t2\tsrc/a.py",
        "5\t0\tsrc/b.py",
    ])
    commits = parse_log_numstat(output)
    assert len(commits) == 1
    c = commits[0]
    assert c["sha"] == "abc123"
    assert c["epoch_s"] == 1758196800
    assert c["lines_added"] == 15
    assert c["lines_removed"] == 2
    assert c["files_changed"] == 2


def test_parse_log_numstat_multiple_commits():
    output = "\n".join([
        _commit_line("c1", 1000),
        "",
        "1\t1\ta.py",
        _commit_line("c2", 2000),
        "",
        "2\t2\tb.py",
    ])
    commits = parse_log_numstat(output)
    assert len(commits) == 2
    assert commits[0]["sha"] == "c1"
    assert commits[1]["sha"] == "c2"


def test_parse_log_numstat_binary_file_counts_as_changed_not_lines():
    output = "\n".join([_commit_line("c1", 1000), "", "-\t-\timage.png"])
    commits = parse_log_numstat(output)
    assert commits[0]["files_changed"] == 1
    assert commits[0]["lines_added"] == 0
    assert commits[0]["lines_removed"] == 0


def test_parse_log_numstat_empty_output():
    assert parse_log_numstat("") == []


def test_parse_log_numstat_commit_with_no_file_changes():
    # e.g. an empty merge commit -- no numstat lines follow
    output = _commit_line("c1", 1000)
    commits = parse_log_numstat(output)
    assert len(commits) == 1
    assert commits[0]["files_changed"] == 0


# -- aggregate_commits: 7d/30d windowing ----------------------------------------


def test_aggregate_commits_buckets_by_window():
    now = datetime(2026, 9, 18, tzinfo=UTC)
    recent = (now - timedelta(days=2)).timestamp()
    mid = (now - timedelta(days=15)).timestamp()
    old = (now - timedelta(days=60)).timestamp()

    commits_by_repo = {
        "repo-a": [
            {"epoch_s": int(recent), "lines_added": 10, "lines_removed": 2, "files_changed": 1},
            {"epoch_s": int(mid), "lines_added": 100, "lines_removed": 5, "files_changed": 3},
        ],
        "repo-b": [
            {"epoch_s": int(old), "lines_added": 999, "lines_removed": 999, "files_changed": 9},
        ],
    }
    result = aggregate_commits(commits_by_repo, now)
    assert result["commits_7d"] == 1  # only the "recent" commit
    assert result["commits_30d"] == 2  # "recent" + "mid", not the 60-day-old one
    assert result["lines_added_7d"] == 10
    assert result["lines_removed_7d"] == 2
    assert result["files_changed_7d"] == 1
    # repo-b's only commit is outside even the 30d window -> excluded from by_repo entirely
    repos = {r["repo"] for r in result["by_repo"]}
    assert repos == {"repo-a"}


def test_aggregate_commits_by_repo_only_counts_7d_window():
    now = datetime(2026, 9, 18, tzinfo=UTC)
    recent = (now - timedelta(days=1)).timestamp()
    mid = (now - timedelta(days=10)).timestamp()
    commits_by_repo = {
        "repo-a": [
            {"epoch_s": int(recent), "lines_added": 5, "lines_removed": 1, "files_changed": 1},
            {"epoch_s": int(mid), "lines_added": 50, "lines_removed": 10, "files_changed": 2},
        ],
    }
    result = aggregate_commits(commits_by_repo, now)
    repo_a = next(r for r in result["by_repo"] if r["repo"] == "repo-a")
    assert repo_a["commits"] == 1  # the 10-day-old commit is outside by_repo's 7d window
    assert repo_a["lines_added"] == 5


def test_aggregate_commits_empty_input():
    result = aggregate_commits({}, datetime.now(UTC))
    assert result["commits_7d"] == 0
    assert result["commits_30d"] == 0
    assert result["by_repo"] == []
    assert result["truncated"] is False


def test_aggregate_commits_truncates_by_repo_top_n():
    now = datetime.now(UTC)
    epoch = int((now - timedelta(days=1)).timestamp())
    commits_by_repo = {
        f"repo-{i}": [{"epoch_s": epoch, "lines_added": 1, "lines_removed": 0, "files_changed": 1}]
        for i in range(50)
    }
    result = aggregate_commits(commits_by_repo, now)
    assert len(result["by_repo"]) == 30
    assert result["truncated"] is True


# -- merge_by_repo: fold remote host rows without double counting ------------


def test_merge_by_repo_adds_remote_contribution():
    local = {
        "commits_7d": 2, "commits_30d": 2, "lines_added_7d": 20, "lines_removed_7d": 3,
        "files_changed_7d": 2,
        "by_repo": [{"repo": "repo-a", "commits": 2, "lines_added": 20, "lines_removed": 3}],
        "truncated": False,
    }
    remote_rows = [
        {"repo": "repo-a", "commits": 1, "lines_added": 5, "lines_removed": 1, "files_changed": 1},
        {"repo": "repo-b", "commits": 3, "lines_added": 30, "lines_removed": 0, "files_changed": 4},
    ]
    merged = merge_by_repo(local, remote_rows)
    assert merged["commits_7d"] == 6  # 2 local + 1 + 3 remote, not double counted
    assert merged["lines_added_7d"] == 55
    by_repo = {r["repo"]: r for r in merged["by_repo"]}
    assert by_repo["repo-a"]["commits"] == 3  # 2 local + 1 remote, same repo, summed not replaced
    assert by_repo["repo-b"]["commits"] == 3  # new repo, added as its own entry


def test_merge_by_repo_does_not_mutate_local_input():
    local = {
        "commits_7d": 1, "commits_30d": 1, "lines_added_7d": 1, "lines_removed_7d": 0,
        "files_changed_7d": 1,
        "by_repo": [{"repo": "repo-a", "commits": 1, "lines_added": 1, "lines_removed": 0}],
        "truncated": False,
    }
    original_by_repo = [dict(r) for r in local["by_repo"]]
    remote_row = {"repo": "repo-a", "commits": 1, "lines_added": 1, "lines_removed": 0, "files_changed": 1}
    merge_by_repo(local, [remote_row])
    assert local["by_repo"] == original_by_repo


# -- ProductivityCollector: real git repo end-to-end --------------------------


def _init_repo(path):
    # Content is seeded from the directory name so two independently
    # `_init_repo`-created repos never hash to the same root commit (git
    # commit hashes depend only on tree+author+message+timestamp, not the
    # filesystem path, so two repos with byte-identical content/messages
    # created in the same second CAN collide) -- distinct from `_clone_repo`
    # below, which is used exactly where an identical history IS wanted (to
    # test mirrored-repo dedupe).
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "a.txt").write_text(f"hello {path.name}\n")
    subprocess.run(["git", "add", "a.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=path, check=True)
    (path / "a.txt").write_text(f"hello {path.name}\nworld\n")
    subprocess.run(["git", "commit", "-qam", "second"], cwd=path, check=True)


class _FakeCtx:
    def __init__(self, worktrees):
        self.latest_worktrees = worktrees
        self.latest_productivity = {}


@pytest.mark.asyncio
async def test_productivity_collector_real_repo(tmp_path):
    repo = tmp_path / "repo1"
    repo.mkdir()
    _init_repo(repo)

    ctx = _FakeCtx([{"path": str(repo), "repo": "repo1", "host": "localhost"}])
    collector = ProductivityCollector(ctx=ctx, store=None, host="localhost")
    returned = await collector.collect()

    assert returned == {}  # never returns a top-level snapshot key -- see module docstring
    assert ctx.latest_productivity["commits_7d"] == 2
    assert ctx.latest_productivity["commits_30d"] == 2
    assert ctx.latest_productivity["lines_added_7d"] == 2
    repo_entry = ctx.latest_productivity["by_repo"][0]
    assert repo_entry["repo"] == "repo1"
    assert repo_entry["commits"] == 2


@pytest.mark.asyncio
async def test_productivity_collector_filters_to_own_host():
    ctx = _FakeCtx([
        {"path": "/some/remote/path", "repo": "remote-repo", "host": "host-b"},
    ])
    collector = ProductivityCollector(ctx=ctx, store=None, host="localhost")
    await collector.collect()
    # remote-hosted worktree must never be git-scanned by the local collector
    assert ctx.latest_productivity["commits_7d"] == 0
    assert ctx.latest_productivity["by_repo"] == []


@pytest.mark.asyncio
async def test_productivity_collector_survives_bad_repo_path():
    ctx = _FakeCtx([{"path": "/nonexistent/path/xyz", "repo": "ghost", "host": "localhost"}])
    collector = ProductivityCollector(ctx=ctx, store=None, host="localhost", git_timeout_s=2.0)
    await collector.collect()  # must not raise
    assert ctx.latest_productivity["commits_7d"] == 0


# -- Bug 1: fast-retry after an empty worktrees list (no 10-minute blank panel) --


@pytest.mark.asyncio
async def test_productivity_collector_fast_retries_when_worktrees_not_ready():
    ctx = _FakeCtx([])  # WorktreesCollector hasn't produced its first scan yet
    collector = ProductivityCollector(ctx=ctx, store=None, host="localhost")
    collector.interval_s = 600.0  # main.py applies the configured steady interval after construction

    await collector.collect()
    assert collector.interval_s == 3.0  # short backoff, not the full 600s
    assert ctx.latest_productivity == {}  # not overwritten with zeros while we're not ready

    await collector.collect()
    assert collector.interval_s == 6.0  # escalating

    for _ in range(10):
        await collector.collect()
    assert collector.interval_s == 600.0  # never exceeds the steady interval


@pytest.mark.asyncio
async def test_productivity_collector_settles_back_to_steady_interval_once_ready():
    ctx = _FakeCtx([])
    collector = ProductivityCollector(ctx=ctx, store=None, host="localhost")
    collector.interval_s = 600.0

    await collector.collect()
    await collector.collect()
    assert collector.interval_s == 6.0  # mid-backoff

    # worktrees show up: the full sweep runs and the interval resets
    ctx.latest_worktrees = [{"path": "/nonexistent", "repo": "x", "host": "localhost"}]
    await collector.collect()
    assert collector.interval_s == 600.0


@pytest.mark.asyncio
async def test_productivity_collector_does_not_fast_retry_when_host_has_zero_local_worktrees():
    # WorktreesCollector HAS run -- the list is non-empty -- it's just that
    # every worktree in it belongs to a remote host. This is a legitimate
    # zero, not "not ready yet", so it must settle at the steady interval and
    # report immediately rather than retrying forever.
    ctx = _FakeCtx([{"path": "/some/remote/path", "repo": "remote-repo", "host": "host-b"}])
    collector = ProductivityCollector(ctx=ctx, store=None, host="localhost")
    collector.interval_s = 600.0
    await collector.collect()
    assert collector.interval_s == 600.0
    assert ctx.latest_productivity["commits_7d"] == 0
    assert ctx.latest_productivity["by_repo"] == []


# -- Bug 2: remote productivity rows must show up in by_repo -----------------


@pytest.mark.asyncio
async def test_productivity_collector_unions_remote_rows_into_by_repo(tmp_path, tmp_store):
    repo = tmp_path / "repo1"
    repo.mkdir()
    _init_repo(repo)

    since_day = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    tmp_store.upsert_remote_productivity_buckets([
        {"host": "host-b", "day": since_day, "repo": "remote-only-repo",
         "commits": 4, "lines_added": 40, "lines_removed": 5, "files_changed": 6},
    ])

    ctx = _FakeCtx([{"path": str(repo), "repo": "repo1", "host": "localhost"}])
    collector = ProductivityCollector(ctx=ctx, store=tmp_store, host="localhost")
    await collector.collect()

    prod = ctx.latest_productivity
    by_repo = {r["repo"]: r for r in prod["by_repo"]}
    assert "remote-only-repo" in by_repo  # shows up even though the local sweep never touched it
    assert by_repo["remote-only-repo"]["commits"] == 4
    assert prod["commits_7d"] == 2 + 4  # local repo1's 2 commits + remote's 4, unioned not lost


# -- Bug 3a: mirrored-repo dedupe by repository identity ---------------------


def _clone_repo(src, dst):
    subprocess.run(["git", "clone", "-q", str(src), str(dst)], check=True)


@pytest.mark.asyncio
async def test_productivity_collector_dedupes_mirrored_repos_by_root_commit(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _init_repo(origin)  # 2 commits: "first" (root) + "second"

    mirror_a = tmp_path / "mirror_a"
    mirror_b = tmp_path / "mirror_b"
    _clone_repo(origin, mirror_a)
    _clone_repo(origin, mirror_b)

    # mirror_b gets one more commit, making it the newer HEAD of the pair
    (mirror_b / "b.txt").write_text("extra\n")
    subprocess.run(["git", "add", "b.txt"], cwd=mirror_b, check=True)
    subprocess.run(["git", "commit", "-qm", "third"], cwd=mirror_b, check=True)

    worktrees = [
        {"path": str(mirror_a), "repo": "demo-mailer", "host": "localhost",
         "last_commit_at": "2020-01-01T00:00:00Z"},
        {"path": str(mirror_b), "repo": "demo-mailer", "host": "localhost",
         "last_commit_at": "2030-01-01T00:00:00Z"},
    ]
    ctx = _FakeCtx(worktrees)
    collector = ProductivityCollector(ctx=ctx, store=None, host="localhost")
    await collector.collect()

    prod = ctx.latest_productivity
    # 3 distinct commits (first, second, third) counted ONCE across the
    # mirrored pair, not 5 (2 + 3) if both paths were swept independently.
    assert prod["commits_7d"] == 3
    by_repo = {r["repo"]: r for r in prod["by_repo"]}
    assert by_repo["demo-mailer"]["commits"] == 3
    assert by_repo["demo-mailer"]["path"] == str(mirror_b)  # newer HEAD was the one chosen


def test_is_standalone_checkout_true_for_real_clone(tmp_path):
    repo = tmp_path / "repo1"
    repo.mkdir()
    _init_repo(repo)
    assert is_standalone_checkout(str(repo)) is True


def test_is_standalone_checkout_false_for_linked_worktree(tmp_path):
    repo = tmp_path / "main"
    repo.mkdir()
    _init_repo(repo)
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "agent-branch", str(linked)], cwd=repo, check=True
    )
    assert is_standalone_checkout(str(linked)) is False


@pytest.mark.asyncio
async def test_productivity_collector_never_dedupes_linked_worktrees(tmp_path):
    # Every linked `git worktree add` checkout of one repo shares that
    # repo's root commit BY CONSTRUCTION -- this is the real-fleet failure
    # mode found during delivery verification (root-SHA dedup collapsed ~150
    # of ~209 real worktrees, most of them agent worktrees under one repo,
    # into a handful of rows). Each linked worktree's own commits, on its
    # own branch, must all be counted -- none of them may be treated as a
    # "mirror" of the main checkout or of each other.
    main = tmp_path / "main"
    main.mkdir()
    _init_repo(main)  # 2 commits on the default branch

    wt_a = tmp_path / "wt-agent-a"
    wt_b = tmp_path / "wt-agent-b"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "agent-a", str(wt_a)], cwd=main, check=True)
    subprocess.run(["git", "worktree", "add", "-q", "-b", "agent-b", str(wt_b)], cwd=main, check=True)

    (wt_a / "a-only.txt").write_text("a\n")
    subprocess.run(["git", "add", "a-only.txt"], cwd=wt_a, check=True)
    subprocess.run(["git", "commit", "-qm", "agent a work"], cwd=wt_a, check=True)

    (wt_b / "b-only.txt").write_text("b\n")
    subprocess.run(["git", "add", "b-only.txt"], cwd=wt_b, check=True)
    subprocess.run(["git", "commit", "-qm", "agent b work"], cwd=wt_b, check=True)

    worktrees = [
        {"path": str(main), "repo": "main", "host": "localhost"},
        {"path": str(wt_a), "repo": "wt-agent-a", "host": "localhost"},
        {"path": str(wt_b), "repo": "wt-agent-b", "host": "localhost"},
    ]
    ctx = _FakeCtx(worktrees)
    collector = ProductivityCollector(ctx=ctx, store=None, host="localhost")
    await collector.collect()

    prod = ctx.latest_productivity
    by_repo = {r["repo"]: r for r in prod["by_repo"]}
    # all three checkouts show up as their own rows -- none collapsed away
    # because they share a root commit
    assert set(by_repo) == {"main", "wt-agent-a", "wt-agent-b"}
    # each linked worktree's `git log` naturally includes the 2 shared base
    # commits plus its own 1 new commit -- that's real, distinct git history
    # per checkout (not a dedup target), so each worktree reports 3 and none
    # were collapsed down to sharing a single row with `main`.
    assert by_repo["main"]["commits"] == 2
    assert by_repo["wt-agent-a"]["commits"] == 3
    assert by_repo["wt-agent-b"]["commits"] == 3


@pytest.mark.asyncio
async def test_productivity_collector_caches_identity_per_path(tmp_path):
    repo = tmp_path / "repo1"
    repo.mkdir()
    _init_repo(repo)

    ctx = _FakeCtx([{"path": str(repo), "repo": "repo1", "host": "localhost",
                      "last_commit_at": "2020-01-01T00:00:00Z"}])
    collector = ProductivityCollector(ctx=ctx, store=None, host="localhost")
    await collector.collect()
    assert str(repo) in collector._identity_cache
    cached_identity = collector._identity_cache[str(repo)]

    await collector.collect()  # second sweep must reuse the cached identity, not recompute it
    assert collector._identity_cache[str(repo)] == cached_identity


def test_pick_representative_prefers_newer_last_commit_at():
    older = {"path": "/a", "last_commit_at": "2020-01-01T00:00:00Z"}
    newer = {"path": "/b", "last_commit_at": "2025-01-01T00:00:00Z"}
    assert pick_representative([older, newer]) is newer
    assert pick_representative([newer, older]) is newer


def test_pick_representative_missing_timestamp_sorts_lowest():
    missing = {"path": "/a"}
    present = {"path": "/b", "last_commit_at": "2020-01-01T00:00:00Z"}
    assert pick_representative([missing, present]) is present


@pytest.mark.asyncio
async def test_compute_repo_identity_matches_for_clones_of_same_history(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _init_repo(origin)
    clone = tmp_path / "clone"
    _clone_repo(origin, clone)

    id_origin = await compute_repo_identity(str(origin), 5.0)
    id_clone = await compute_repo_identity(str(clone), 5.0)
    assert id_origin == id_clone
    assert id_origin.startswith("sha:")


@pytest.mark.asyncio
async def test_compute_repo_identity_falls_back_to_path_when_git_fails():
    identity = await compute_repo_identity("/nonexistent/path/xyz", 2.0)
    assert identity == "path:/nonexistent/path/xyz"


# -- Bug 3b: fixture-repo exclusion split -------------------------------------


def test_is_excluded_repo_matches_glob_pattern():
    assert is_excluded_repo("demo-fixture-repo", ["demo-fixture-*"]) is True
    assert is_excluded_repo("demo-mailer", ["demo-fixture-*"]) is False


def test_split_commits_by_exclusion():
    commits_by_repo = {
        "realproj": [{"epoch_s": 1, "lines_added": 1, "lines_removed": 0, "files_changed": 1}],
        "demo-fixture-repo": [{"epoch_s": 1, "lines_added": 1, "lines_removed": 0, "files_changed": 1}],
    }
    included, excluded = split_commits_by_exclusion(commits_by_repo, ["demo-fixture-*"])
    assert set(included) == {"realproj"}
    assert set(excluded) == {"demo-fixture-repo"}


@pytest.mark.asyncio
async def test_productivity_collector_splits_excluded_fixture_repos(tmp_path):
    real_repo = tmp_path / "realproj"
    real_repo.mkdir()
    _init_repo(real_repo)

    fixture_repo = tmp_path / "demo-fixture-repo"
    fixture_repo.mkdir()
    _init_repo(fixture_repo)

    worktrees = [
        {"path": str(real_repo), "repo": "realproj", "host": "localhost"},
        {"path": str(fixture_repo), "repo": "demo-fixture-repo", "host": "localhost"},
    ]
    ctx = _FakeCtx(worktrees)
    collector = ProductivityCollector(
        ctx=ctx, store=None, host="localhost", excluded_repos=["demo-fixture-*"]
    )
    await collector.collect()

    prod = ctx.latest_productivity
    # top-level stays inclusive of everything -- nothing is silently dropped
    assert prod["commits_7d"] == 4
    assert {r["repo"] for r in prod["by_repo"]} == {"realproj", "demo-fixture-repo"}
    assert prod["excluded_repo_patterns"] == ["demo-fixture-*"]

    excl = prod["excluding_fixtures"]
    assert {r["repo"] for r in excl["by_repo"]} == {"realproj"}
    assert excl["commits_7d"] == 2

    fix = prod["fixtures"]
    assert {r["repo"] for r in fix["by_repo"]} == {"demo-fixture-repo"}
    assert fix["commits_7d"] == 2


@pytest.mark.asyncio
async def test_productivity_collector_default_excludes_nothing(tmp_path):
    fixture_repo = tmp_path / "demo-fixture-repo"
    fixture_repo.mkdir()
    _init_repo(fixture_repo)

    ctx = _FakeCtx([{"path": str(fixture_repo), "repo": "demo-fixture-repo", "host": "localhost"}])
    collector = ProductivityCollector(ctx=ctx, store=None, host="localhost")
    await collector.collect()

    prod = ctx.latest_productivity
    # default excludes nothing -- all repos are included
    assert prod["excluded_repo_patterns"] == []
    assert {r["repo"] for r in prod["by_repo"]} == {"demo-fixture-repo"}
    assert prod["excluding_fixtures"]["commits_7d"] == 2


@pytest.mark.asyncio
async def test_productivity_collector_custom_excluded_repos_pattern(tmp_path):
    fixture_repo = tmp_path / "load-test-9"
    fixture_repo.mkdir()
    _init_repo(fixture_repo)

    ctx = _FakeCtx([{"path": str(fixture_repo), "repo": "load-test-9", "host": "localhost"}])
    collector = ProductivityCollector(ctx=ctx, store=None, host="localhost", excluded_repos=["load-test-*"])
    await collector.collect()

    prod = ctx.latest_productivity
    assert prod["excluding_fixtures"]["commits_7d"] == 0
    assert prod["fixtures"]["commits_7d"] == 2
