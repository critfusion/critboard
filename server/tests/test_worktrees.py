import subprocess

import pytest

from critdash.collectors.worktrees import _parse_status_v2, collect_one, find_git_dirs


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def make_repo(path):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "a.txt").write_text("hello\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "initial commit")
    return path


def test_find_git_dirs(tmp_path):
    repo = make_repo(tmp_path / "repos" / "myrepo")
    (tmp_path / "repos" / "not_a_repo").mkdir()
    found = find_git_dirs(str(tmp_path / "repos"))
    assert str(repo) in found
    assert str(tmp_path / "repos" / "not_a_repo") not in found


def test_find_git_dirs_prunes_node_modules(tmp_path):
    repo = make_repo(tmp_path / "repos" / "myrepo")
    (tmp_path / "repos" / "myrepo" / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "repos" / "myrepo" / "node_modules" / "pkg" / ".git").mkdir()
    found = find_git_dirs(str(tmp_path / "repos"))
    assert str(repo) in found
    assert not any("node_modules" in f for f in found)


@pytest.mark.asyncio
async def test_collect_one_clean_repo(tmp_path):
    repo = make_repo(tmp_path / "clean")
    result = await collect_one(str(repo), str(tmp_path), timeout=5.0)
    assert result is not None
    assert result["branch"] == "main"
    assert result["dirty"] == 0
    assert result["untracked"] == 0
    assert result["staged"] == 0
    assert result["last_commit_msg"] == "initial commit"
    assert result["last_commit_author"] == "Test"
    assert result["stale_days"] == 0
    assert result["upstream"] is None
    assert result["ahead"] == 0 and result["behind"] == 0


@pytest.mark.asyncio
async def test_collect_one_dirty_and_untracked(tmp_path):
    repo = make_repo(tmp_path / "dirty")
    (repo / "a.txt").write_text("changed\n")
    (repo / "b.txt").write_text("new file\n")
    result = await collect_one(str(repo), str(tmp_path), timeout=5.0)
    assert result["dirty"] == 1
    assert result["untracked"] == 1
    assert result["staged"] == 0


@pytest.mark.asyncio
async def test_collect_one_staged(tmp_path):
    repo = make_repo(tmp_path / "staged")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "add", "a.txt")
    result = await collect_one(str(repo), str(tmp_path), timeout=5.0)
    assert result["staged"] == 1
    assert result["dirty"] == 0


@pytest.mark.asyncio
async def test_collect_one_not_a_repo_returns_none(tmp_path):
    not_repo = tmp_path / "not_a_repo"
    not_repo.mkdir()
    result = await collect_one(str(not_repo), str(tmp_path), timeout=5.0)
    assert result is None


@pytest.mark.asyncio
async def test_collect_one_upstream_ahead_behind(tmp_path):
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    repo = make_repo(tmp_path / "with_upstream")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "-u", "origin", "main")
    (repo / "b.txt").write_text("second\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "second commit")

    result = await collect_one(str(repo), str(tmp_path), timeout=5.0)
    assert result["upstream"] == "origin/main"
    assert result["ahead"] == 1
    assert result["behind"] == 0
    assert result["branch"] == "main"
    assert result["head"] is not None


# -- git status --porcelain=v2 parsing -------------------------------------
# v2 uses '.' (not ' ') as the "no change in this slot" placeholder in the XY
# pair. This is the subtle bit collapsing to a single `status --porcelain=v2
# --branch` call depends on getting right -- verified against real git output.


def test_parse_status_v2_clean_repo():
    porcelain = "# branch.oid abc123\n# branch.head main\n"
    dirty, untracked, staged, branch, upstream, ahead, behind = _parse_status_v2(porcelain)
    assert (dirty, untracked, staged) == (0, 0, 0)
    assert branch == "main"
    assert upstream is None
    assert (ahead, behind) == (0, 0)


def test_parse_status_v2_unstaged_and_untracked():
    porcelain = "# branch.head main\n1 .M N... 100644 100644 100644 aaa bbb a.txt\n? b.txt\n"
    dirty, untracked, staged, *_ = _parse_status_v2(porcelain)
    assert dirty == 1
    assert untracked == 1
    assert staged == 0


def test_parse_status_v2_staged():
    porcelain = "# branch.head main\n1 M. N... 100644 100644 100644 aaa bbb a.txt\n"
    dirty, untracked, staged, *_ = _parse_status_v2(porcelain)
    assert staged == 1
    assert dirty == 0


def test_parse_status_v2_ahead_behind_and_upstream():
    porcelain = "# branch.head main\n# branch.upstream origin/main\n# branch.ab +2 -3\n"
    _, _, _, branch, upstream, ahead, behind = _parse_status_v2(porcelain)
    assert branch == "main"
    assert upstream == "origin/main"
    assert ahead == 2
    assert behind == 3


def test_parse_status_v2_detached_head():
    # matches the pre-existing behavior of `git rev-parse --abbrev-ref HEAD`,
    # which prints the literal string "HEAD" for a detached checkout
    porcelain = "# branch.head (detached)\n"
    _, _, _, branch, *_ = _parse_status_v2(porcelain)
    assert branch == "HEAD"


def test_parse_status_v2_empty_string():
    assert _parse_status_v2("") == (0, 0, 0, None, None, 0, 0)


# -- find_git_dirs pruning ---------------------------------------------------


def test_find_git_dirs_never_descends_into_a_found_repo(tmp_path):
    # a nested .git inside a found repo's own subtree must NOT be reported as
    # a separate worktree, regardless of the directory name it sits under --
    # this must hold even when that directory name is not in the prune list.
    repo = make_repo(tmp_path / "repos" / "myrepo")
    nested = tmp_path / "repos" / "myrepo" / "packages" / "sub"
    nested.mkdir(parents=True)
    (nested / ".git").mkdir()
    found = find_git_dirs(str(tmp_path / "repos"))
    assert str(repo) in found
    assert str(nested) not in found
    assert len(found) == 1


def test_find_git_dirs_prunes_configured_names(tmp_path):
    # an explicit custom prune_dirs list must work on its own (e.g. a scan
    # root that isn't itself a repo, containing a named vendor dir before any
    # repo is found -- so the "stop descending into a found repo" rule alone
    # wouldn't cover it)
    root = tmp_path / "scanroot"
    (root / "some_custom_vendor_dir" / "pkg").mkdir(parents=True)
    (root / "some_custom_vendor_dir" / "pkg" / ".git").mkdir()
    found = find_git_dirs(str(root), prune_dirs={"some_custom_vendor_dir"})
    assert found == []


def test_find_git_dirs_prunes_dotdirs_even_when_not_in_prune_list(tmp_path):
    root = tmp_path / "scanroot"
    hidden = root / ".terraform" / "modules" / "pkg"
    hidden.mkdir(parents=True)
    (hidden / ".git").mkdir()
    found = find_git_dirs(str(root))
    assert found == []


def test_find_git_dirs_respects_max_depth(tmp_path):
    root = tmp_path / "scanroot"
    # depth 1: within range even at the tightest realistic limit
    shallow = make_repo(root / "reponame")
    # depth 3: root/a (depth1) -> root/a/b (depth2, hits the depth-2 limit and
    # is pruned there) -> root/a/b/c is never visited, so its .git is never seen
    deep_parent = root / "a" / "b" / "c"
    deep_parent.mkdir(parents=True)
    (deep_parent / ".git").mkdir()
    found = find_git_dirs(str(root), max_depth=2)
    assert str(shallow) in found
    assert str(deep_parent) not in found
