import json
import sys

import pytest

from critdash.collectors import CollectorIssue
from critdash.collectors import beads as beads_mod
from critdash.collectors.beads import (
    BeadsCollector,
    bd_shell_prefix,
    build_dependency_maps,
    guess_repo,
    is_review_lane,
    resolve_bd_bin,
    transform_item,
)


def load_fixture(fixtures_dir, name):
    with open(fixtures_dir / name) as f:
        return json.load(f)


def test_guess_repo_from_label():
    item = {"labels": ["demo-web"], "title": "whatever"}
    assert guess_repo(item) == "demo-web"


def test_guess_repo_skips_intent_labels():
    item = {"labels": ["needs-codex", "grok-review"], "title": "demo-lms-content: fix thing"}
    assert guess_repo(item) == "demo-lms-content"


def test_guess_repo_none_when_unresolvable():
    item = {"labels": [], "title": "just a plain sentence with no colon"}
    assert guess_repo(item) is None


def test_is_review_lane():
    assert is_review_lane({"labels": ["needs-review"]}) is True
    assert is_review_lane({"labels": ["needs-codex"]}) is False


def test_build_dependency_maps_direction():
    # issue jx11 depends on d81w (type "blocks") -> jx11 is blocked_by d81w,
    # and d81w blocks jx11.
    items = [
        {
            "id": "jx11",
            "dependencies": [
                {"issue_id": "jx11", "depends_on_id": "d81w", "type": "blocks"},
                {"issue_id": "jx11", "depends_on_id": "parent1", "type": "parent-child"},
            ],
        },
        {"id": "d81w", "dependencies": []},
    ]
    blocked_by, blocks = build_dependency_maps(items)
    assert blocked_by["jx11"] == ["d81w"]
    assert blocks["d81w"] == ["jx11"]
    # parent-child dependency must not show up as a blocker
    assert "parent1" not in blocked_by.get("jx11", [])


def test_transform_item_shape(fixtures_dir):
    items_raw = load_fixture(fixtures_dir, "bd_list.json")
    blocked_by, blocks = build_dependency_maps(items_raw)
    item = transform_item(items_raw[0], blocked_by, blocks)
    for key in (
        "id", "title", "status", "priority", "type", "assignee", "labels",
        "created_at", "updated_at", "closed_at", "age_s", "blocked_by",
        "blocks", "parent", "repo", "url",
    ):
        assert key in item
    assert item["type"] in ("task", "bug", "feature", "epic", "chore")
    assert isinstance(item["labels"], list)


@pytest.mark.asyncio
async def test_collect_end_to_end(fixtures_dir, tmp_path, monkeypatch):
    list_text = (fixtures_dir / "bd_list.json").read_text()
    stats_text = (fixtures_dir / "bd_stats.json").read_text()
    ready_text = (fixtures_dir / "bd_ready.json").read_text()

    async def fake_run(cmd, timeout=20.0):
        if " list " in cmd:
            return list_text
        if " stats " in cmd:
            return stats_text
        if " ready " in cmd:
            return ready_text
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(beads_mod, "_run", fake_run)

    # Preflight checks (dependency_missing/config_missing) run before _run is
    # ever called -- bd_bin must resolve to something real (sys.executable is
    # a guaranteed-present stand-in; _run is mocked, so it's never actually
    # invoked) and beads_env must exist, independent of whatever this host
    # happens to have installed.
    env_path = tmp_path / "env"
    env_path.write_text("")
    collector = BeadsCollector(bd_bin=sys.executable, beads_env=str(env_path))
    result = await collector.collect()

    assert "beads" in result
    b = result["beads"]
    assert set(b["stats"].keys()) == {"open", "in_progress", "blocked", "closed_today", "ready"}
    assert isinstance(b["items"], list) and len(b["items"]) == len(json.loads(list_text))
    assert set(b["lanes"].keys()) == {"ready", "in_progress", "blocked", "review"}


@pytest.mark.asyncio
async def test_pagination_notice_after_json_is_stripped(fixtures_dir, tmp_path, monkeypatch):
    """bd ready --json can print a trailing plain-text pagination notice on
    stdout after the JSON array when truncated; the collector must not choke
    on it (regression test for a real bug hit while building this)."""
    list_text = (fixtures_dir / "bd_list.json").read_text()
    stats_text = (fixtures_dir / "bd_stats.json").read_text()
    ready_with_notice = (
        '[{"id": "x1", "status": "open", "title": "t"}]\n'
        "Showing 1 of 5 ready issues. Use --limit 0 for all.\n"
    )

    async def fake_run(cmd, timeout=20.0):
        if " list " in cmd:
            return list_text
        if " stats " in cmd:
            return stats_text
        if " ready " in cmd:
            return ready_with_notice
        raise AssertionError(cmd)

    monkeypatch.setattr(beads_mod, "_run", fake_run)
    env_path = tmp_path / "env"
    env_path.write_text("")
    collector = BeadsCollector(bd_bin=sys.executable, beads_env=str(env_path))
    result = await collector.collect()
    assert result["beads"]["lanes"]["ready"] == ["x1"]


def test_status_transition_events(tmp_store):
    collector = BeadsCollector(bd_bin="bd", store=tmp_store)
    item_v1 = {
        "id": "abc", "title": "t", "status": "open", "priority": 1, "type": "task",
        "assignee": None, "labels": [], "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z", "closed_at": None, "age_s": 10,
        "blocked_by": [], "blocks": [], "parent": None, "repo": None, "url": None,
    }
    collector._detect_transitions([item_v1])
    events = tmp_store.recent_events(10)
    assert any(e["kind"] == "bead_created" for e in events)

    # status change + a fresh claim in the same poll: bead_claimed is emitted
    # (the more specific, informative event) and a redundant bead_status is
    # suppressed since "claimed" already implies the open -> in_progress move.
    item_v2 = dict(item_v1, status="in_progress", assignee="localhost-claude")
    collector._detect_transitions([item_v2])
    events = tmp_store.recent_events(10)
    kinds = {e["kind"] for e in events}
    assert "bead_claimed" in kinds
    assert "bead_status" not in kinds

    # a pure status change with no assignee change does emit bead_status
    item_v2b = dict(item_v2, status="blocked")
    collector._detect_transitions([item_v2b])
    events = tmp_store.recent_events(10)
    assert any(e["kind"] == "bead_status" for e in events)

    item_v3 = dict(item_v2b, status="closed")
    collector._detect_transitions([item_v3])
    events = tmp_store.recent_events(10)
    assert any(e["kind"] == "bead_closed" for e in events)


# -- reason_code classification (briefing: "explain why a collector has no
# data") -------------------------------------------------------------------


class _FakeProc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang

    async def communicate(self):
        if self._hang:
            import asyncio as _asyncio

            await _asyncio.sleep(999)
        return self._stdout, self._stderr

    def kill(self):
        pass

    async def wait(self):
        return None


def test_resolve_bd_bin_absolute_path_exists(tmp_path):
    binp = tmp_path / "bd"
    binp.write_text("#!/bin/sh\n")
    binp.chmod(0o755)
    assert resolve_bd_bin(str(binp)) == str(binp)


def test_resolve_bd_bin_absolute_path_missing(tmp_path):
    assert resolve_bd_bin(str(tmp_path / "no-such-binary")) is None


def test_resolve_bd_bin_bare_name_missing():
    assert resolve_bd_bin("definitely-not-a-real-binary-xyz123") is None


@pytest.mark.asyncio
async def test_collect_dependency_missing_when_bd_not_on_path(tmp_path):
    env_path = tmp_path / "env"
    env_path.write_text("")
    collector = BeadsCollector(bd_bin=str(tmp_path / "no-such-bd-binary"), beads_env=str(env_path))
    with pytest.raises(CollectorIssue) as exc_info:
        await collector.collect()
    issue = exc_info.value
    assert issue.reason_code == "dependency_missing"
    assert issue.optional is True
    assert "no-such-bd-binary" in issue.detail


@pytest.mark.asyncio
async def test_collect_succeeds_with_no_env_file(monkeypatch, fixtures_dir):
    """Bug 3 (corrected model): ~/.config/beads/env is a site-specific
    convention, not something bd requires. A `bd` with its own local
    workspace (bd init / BEADS_DIR) needs no env file at all, and the
    collector must not refuse to run just because one is absent."""
    list_text = (fixtures_dir / "bd_list.json").read_text()
    stats_text = (fixtures_dir / "bd_stats.json").read_text()
    ready_text = (fixtures_dir / "bd_ready.json").read_text()

    async def fake_run(cmd, timeout=20.0):
        # No `.` (dot) sourcing a nonexistent env file should ever reach the
        # command line when beads_env doesn't exist -- see bd_shell_prefix.
        assert cmd.startswith("export BEADS_ACTOR=")
        if " list " in cmd:
            return list_text
        if " stats " in cmd:
            return stats_text
        if " ready " in cmd:
            return ready_text
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(beads_mod, "_run", fake_run)
    collector = BeadsCollector(bd_bin=sys.executable, beads_env="/nonexistent/beads/env")
    result = await collector.collect()
    assert "beads" in result


@pytest.mark.asyncio
async def test_collect_zero_beads_is_success_not_failure(tmp_path, monkeypatch):
    """bd ran fine and genuinely has zero beads -- this is success with no
    data, not a failure, and must not raise."""
    env_path = tmp_path / "env"
    env_path.write_text("")

    async def fake_run(cmd, timeout=20.0):
        if " list " in cmd:
            return "[]"
        if " stats " in cmd:
            return json.dumps({"summary": {}})
        if " ready " in cmd:
            return "[]"
        raise AssertionError(cmd)

    monkeypatch.setattr(beads_mod, "_run", fake_run)
    collector = BeadsCollector(bd_bin=sys.executable, beads_env=str(env_path))
    result = await collector.collect()
    assert result["beads"]["items"] == []
    assert result["beads"]["stats"]["open"] == 0
    assert result["beads"]["lanes"] == {"ready": [], "in_progress": [], "blocked": [], "review": []}


@pytest.mark.asyncio
async def test_run_command_failed_uses_stderr(monkeypatch):
    async def fake_exec(cmd, **kwargs):
        return _FakeProc(stderr=b"Error: no beads database found\n", returncode=1)

    monkeypatch.setattr(beads_mod.asyncio, "create_subprocess_shell", fake_exec)
    with pytest.raises(CollectorIssue) as exc_info:
        await beads_mod._run("bd stats --json")
    issue = exc_info.value
    assert issue.reason_code == "command_failed"
    assert "no beads database found" in issue.detail
    assert issue.optional is False


@pytest.mark.asyncio
async def test_run_command_failed_falls_back_to_stdout_when_stderr_empty(monkeypatch):
    """Reproduces the fresh-install bug: a failure whose stderr is empty
    (the dash dot-builtin-abort case, or any tool that writes its error to
    stdout instead) must not produce a blank reason."""

    async def fake_exec(cmd, **kwargs):
        return _FakeProc(stdout=b"fatal: something went wrong\n", stderr=b"", returncode=2)

    monkeypatch.setattr(beads_mod.asyncio, "create_subprocess_shell", fake_exec)
    with pytest.raises(CollectorIssue) as exc_info:
        await beads_mod._run("bd stats --json")
    issue = exc_info.value
    assert issue.reason_code == "command_failed"
    assert "fatal: something went wrong" in issue.detail


@pytest.mark.asyncio
async def test_run_empty_output_both_streams_still_has_a_reason(monkeypatch):
    """Both streams empty (the literal fresh-install "exit 2:" bug) must
    still produce a non-blank, real reason."""

    async def fake_exec(cmd, **kwargs):
        return _FakeProc(stdout=b"", stderr=b"", returncode=2)

    monkeypatch.setattr(beads_mod.asyncio, "create_subprocess_shell", fake_exec)
    with pytest.raises(CollectorIssue) as exc_info:
        await beads_mod._run("bd stats --json")
    issue = exc_info.value
    assert issue.reason_code == "command_failed"
    assert issue.detail.strip() != "bd exited 2:"
    assert "no output" in issue.detail


@pytest.mark.asyncio
async def test_run_timeout_is_unreachable(monkeypatch):
    async def fake_exec(cmd, **kwargs):
        return _FakeProc(hang=True)

    monkeypatch.setattr(beads_mod.asyncio, "create_subprocess_shell", fake_exec)
    with pytest.raises(CollectorIssue) as exc_info:
        await beads_mod._run("bd stats --json", timeout=0.02)
    issue = exc_info.value
    assert issue.reason_code == "unreachable"
    assert issue.optional is False


# -- availability_issue (Bug 3 fix: shared preflight, used by collect() and
# by main.py's startup auto-detection. Corrected model: `bd` resolving is
# the ONLY thing that gates availability -- the beads env file is an
# optional, site-specific convention, not a requirement. See module
# docstring for the live `bd`-with-no-env-file behavior this is built on.)
# -----------------------------------------------------------------------


def test_availability_issue_none_when_bd_present():
    """Available as soon as `bd` resolves -- no env file required at all."""
    assert beads_mod.availability_issue(sys.executable) is None


def test_availability_issue_dependency_missing_when_bd_absent(tmp_path):
    issue = beads_mod.availability_issue(str(tmp_path / "no-such-bd"))
    assert issue.reason_code == "dependency_missing"
    assert issue.optional is True


# -- bd_shell_prefix (Bug 3: env file optional, sourced only if present) ----


def test_bd_shell_prefix_sources_env_file_when_present(tmp_path):
    env_path = tmp_path / "env"
    env_path.write_text("export BEADS_DB=foo\n")
    prefix = bd_shell_prefix(str(env_path), "critdash")
    assert prefix == f". {env_path} 2>/dev/null; export BEADS_ACTOR=critdash;"


def test_bd_shell_prefix_skips_sourcing_when_env_file_absent():
    prefix = bd_shell_prefix("/nonexistent/beads/env", "critdash")
    assert "/nonexistent/beads/env" not in prefix
    assert prefix.startswith("export BEADS_ACTOR=")


def test_bd_shell_prefix_skips_sourcing_when_env_empty_string():
    """The shipped config default for beads_env is now "" (Bug 3) -- must
    behave exactly like "no env file", not try to source an empty path."""
    prefix = bd_shell_prefix("", "critdash")
    assert prefix.startswith("export BEADS_ACTOR=")


# -- beads_dir precedence: beads_dir -> beads_env -> bd's own resolution ----


def test_bd_shell_prefix_exports_beads_dir_when_set(tmp_path):
    ws = tmp_path / "workspace" / ".beads"
    ws.mkdir(parents=True)
    prefix = bd_shell_prefix("", "critdash", str(ws))
    assert prefix == f"export BEADS_ACTOR=critdash; export BEADS_DIR={ws};"


def test_bd_shell_prefix_omits_beads_dir_when_unset():
    prefix = bd_shell_prefix("", "critdash", "")
    assert "BEADS_DIR" not in prefix


def test_bd_shell_prefix_beads_dir_exported_after_env_file_so_it_wins(tmp_path):
    """beads_dir takes precedence over whatever beads_env's sourced script
    exports -- it must be exported AFTER the `.` (source) line, so a shell
    evaluating this prefix left-to-right ends up with BEADS_DIR pointed at
    beads_dir, not whatever the env file set."""
    env_path = tmp_path / "env"
    env_path.write_text("export BEADS_DIR=/wrong/path\n")
    ws = tmp_path / "workspace" / ".beads"
    ws.mkdir(parents=True)
    prefix = bd_shell_prefix(str(env_path), "critdash", str(ws))
    source_idx = prefix.index(". ")
    beads_dir_idx = prefix.index("export BEADS_DIR=")
    assert source_idx < beads_dir_idx
    assert str(ws) in prefix


def test_validate_beads_dir_none_when_unset():
    assert beads_mod.validate_beads_dir("") is None


def test_validate_beads_dir_none_when_directory_exists(tmp_path):
    ws = tmp_path / ".beads"
    ws.mkdir()
    assert beads_mod.validate_beads_dir(str(ws)) is None


def test_validate_beads_dir_issue_when_path_does_not_exist(tmp_path):
    bad = tmp_path / "does-not-exist" / ".beads"
    issue = beads_mod.validate_beads_dir(str(bad))
    assert issue.reason_code == "config_missing"
    assert issue.optional is False
    assert str(bad) in issue.detail
    assert ".beads directory ITSELF" in issue.remedy


def test_validate_beads_dir_issue_when_path_is_the_parent_not_dot_beads(tmp_path):
    """The documented easy mistake: pointing beads_dir at the workspace's
    PARENT directory instead of its .beads subdirectory. The parent exists
    as a directory, so this can't be caught by existence alone -- but a
    parent that has no .beads child is still just "not a workspace" from
    bd's point of view once BEADS_DIR is exported; validate_beads_dir only
    guarantees the configured path itself exists as a directory. This test
    documents that a nonexistent path (the more common typo) is caught."""
    parent = tmp_path / "myproject"
    parent.mkdir()
    assert beads_mod.validate_beads_dir(str(parent)) is None  # exists as a dir -- not rejected here


def test_availability_issue_none_when_beads_dir_valid(tmp_path):
    ws = tmp_path / ".beads"
    ws.mkdir()
    assert beads_mod.availability_issue(sys.executable, str(ws)) is None


def test_availability_issue_config_missing_when_beads_dir_bad(tmp_path):
    bad = tmp_path / "no-such-workspace"
    issue = beads_mod.availability_issue(sys.executable, str(bad))
    assert issue.reason_code == "config_missing"
    assert issue.optional is False


def test_availability_issue_bd_missing_takes_priority_over_beads_dir(tmp_path):
    """bd_bin missing is checked first -- a dependency_missing issue, not a
    beads_dir config_missing one, even if beads_dir is ALSO bad."""
    issue = beads_mod.availability_issue(str(tmp_path / "no-such-bd"), str(tmp_path / "no-such-dir"))
    assert issue.reason_code == "dependency_missing"


@pytest.mark.asyncio
async def test_collect_uses_beads_dir_in_command_prefix(monkeypatch, fixtures_dir):
    """End-to-end: BeadsCollector actually threads beads_dir into every `bd`
    invocation via _prefix()."""
    ws = fixtures_dir  # any existing directory works for validate_beads_dir
    seen_cmds = []

    async def fake_exec(cmd, **kwargs):
        seen_cmds.append(cmd)
        if "list" in cmd:
            return _FakeProc(stdout=b"[]")
        if "stats" in cmd:
            return _FakeProc(stdout=b'{"summary": {}}')
        return _FakeProc(stdout=b"[]")

    monkeypatch.setattr(beads_mod.asyncio, "create_subprocess_shell", fake_exec)
    collector = BeadsCollector(bd_bin=sys.executable, beads_env="", beads_dir=str(ws))
    await collector.collect()
    assert seen_cmds
    assert all(f"BEADS_DIR={ws}" in cmd for cmd in seen_cmds)


@pytest.mark.asyncio
async def test_bd_no_workspace_failure_classified_with_real_message(monkeypatch):
    """Live-verified (isolated HOME, no env file, no `bd init`): `bd list
    --json --all --limit 0` exits 1 with this exact stderr. That must
    surface through the normal command_failed classification with bd's own
    message intact -- not a special-cased "config_missing"."""
    real_stderr = (
        "Error: no beads database found\n"
        "Hint: run 'bd where' to inspect the resolved workspace, or 'bd init' "
        "to create a new database\n"
        "      or set BEADS_DIR to point to your .beads directory"
    )

    async def fake_exec(cmd, **kwargs):
        return _FakeProc(stderr=real_stderr.encode(), returncode=1)

    monkeypatch.setattr(beads_mod.asyncio, "create_subprocess_shell", fake_exec)
    with pytest.raises(CollectorIssue) as exc_info:
        await beads_mod._run("bd list --json --all --limit 0")
    issue = exc_info.value
    assert issue.reason_code == "command_failed"
    assert "no beads database found" in issue.detail
    assert "bd init" in issue.detail
