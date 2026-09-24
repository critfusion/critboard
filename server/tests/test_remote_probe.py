import json
import sqlite3

import pytest

from critdash import remote_probe as rp
from critdash.collectors import analytics as local_an
from critdash.collectors import bead_sessions as local_bs


def _assistant_line(message_id, model, ts, input_tokens=2, output_tokens=10, cache_read=0,
                     cw_5m=0, cw_1h=0):
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": cw_5m,
                    "ephemeral_1h_input_tokens": cw_1h,
                },
            },
        },
    })


# -- collect_system (mirrors collectors/system.py -- Bug 1: cross-platform.
# Linux is exercised for real (this test host); the macOS branch is exercised
# by monkeypatching sys.platform and rp._run_command with captured-format
# fixtures -- implemented-but-unverified on real macOS, see that module's
# docstring / collectors/system.py's docstring.) ------------------------------

_SYSCTL_MEMSIZE_OUTPUT = "hw.memsize: 17179869184\n"
_VM_STAT_OUTPUT = """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                             212345.
Pages active:                          1234567.
Pages inactive:                         345678.
Pages wired down:                       456789.
Pages occupied by compressor:            23456.
"""


def test_collect_system_linux_reports_real_loadavg_and_meminfo():
    result = rp.collect_system(["/"])
    assert isinstance(result["load1"], float)
    assert result["mem_total_gb"] > 0
    assert result["mem_used_gb"] is not None
    assert any(d["mount"] == "/" for d in result["disks"])


def test_collect_system_default_disk_mount_no_longer_hardcodes_srv():
    import inspect
    src = inspect.getsource(rp.main)
    assert '["/"]' in src
    assert "/srv" not in src


def test_collect_system_darwin_branch_via_monkeypatched_platform(monkeypatch):
    monkeypatch.setattr(rp.sys, "platform", "darwin")

    def fake_run_command(args, timeout=5.0):
        if args[0] == "sysctl":
            return _SYSCTL_MEMSIZE_OUTPUT
        if args[0] == "vm_stat":
            return _VM_STAT_OUTPUT
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(rp, "_run_command", fake_run_command)

    result = rp.collect_system(["/"])
    assert result["mem_total_gb"] == pytest.approx(17179869184 / (1024 ** 3), rel=1e-3)
    expected_used_gb = round((1234567 + 456789 + 23456) * 4096 / (1024 ** 3), 1)
    assert result["mem_used_gb"] == pytest.approx(expected_used_gb)


def test_collect_system_darwin_branch_used_none_when_vm_stat_unparseable(monkeypatch):
    monkeypatch.setattr(rp.sys, "platform", "darwin")
    monkeypatch.setattr(
        rp, "_run_command",
        lambda args, timeout=5.0: _SYSCTL_MEMSIZE_OUTPUT if args[0] == "sysctl" else "garbage\n",
    )
    result = rp.collect_system(["/"])
    assert result["mem_total_gb"] > 0
    assert result["mem_used_gb"] is None


def test_collect_system_unsupported_platform_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(rp.sys, "platform", "win32")
    assert rp.collect_system(["/"]) == {}


def test_collect_system_skips_configured_mount_that_does_not_exist(tmp_path):
    missing = str(tmp_path / "does-not-exist")
    result = rp.collect_system(["/", missing])
    mounts = {d["mount"] for d in result["disks"]}
    assert mounts == {"/"}


# -- extract_usage_fields (token counts only, no cost) -----------------------


def test_extract_usage_fields_real_row_has_no_cost_key():
    doc = json.loads(_assistant_line("msg_1", "claude-opus-5", "2026-09-18T12:00:00.000Z",
                                      cache_read=100, cw_1h=50))
    row = rp.extract_usage_fields(doc)
    assert row is not None
    assert row["model"] == "claude-opus-5"
    assert row["message_id"] == "msg_1"
    assert row["cache_read"] == 100
    assert row["cache_write_1h"] == 50
    assert "cost_usd" not in row  # probe ships token counts only, never a cost


def test_extract_usage_fields_skips_synthetic():
    doc = {"type": "assistant", "timestamp": "2026-09-18T12:00:00Z",
           "message": {"model": "<synthetic>", "usage": {"input_tokens": 0}, "id": "x"}}
    assert rp.extract_usage_fields(doc) is None


def test_extract_usage_fields_skips_non_assistant():
    assert rp.extract_usage_fields({"type": "user"}) is None


def test_extract_usage_fields_skips_null_usage():
    doc = {"type": "assistant", "timestamp": "t", "message": {"model": "m", "id": "x", "usage": None}}
    assert rp.extract_usage_fields(doc) is None


def test_extract_usage_fields_falls_back_to_flat_cache_creation_field():
    doc = {
        "type": "assistant", "timestamp": "2026-09-18T12:00:00Z",
        "message": {
            "id": "msg_2", "model": "claude-opus-5",
            "usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 40},
        },
    }
    row = rp.extract_usage_fields(doc)
    assert row["cache_write_5m"] == 40
    assert row["cache_write_1h"] == 0


# -- aggregate_file: bucketing + message_id dedup within one file ------------


_EMPTY_AGGREGATE = {
    "usage": {}, "tools": {}, "errors": {}, "error_examples": {},
    "trouble_files": {}, "api_errors": {}, "sessions": {},
}


def test_aggregate_file_buckets_by_hour_and_model(tmp_path):
    path = tmp_path / "session.jsonl"
    lines = [
        _assistant_line("m1", "claude-opus-5", "2026-09-18T12:05:00.000Z", output_tokens=5),
        _assistant_line("m2", "claude-opus-5", "2026-09-18T12:40:00.000Z", output_tokens=7),
        _assistant_line("m3", "claude-sonnet-5", "2026-09-18T13:00:00.000Z", output_tokens=9),
    ]
    path.write_text("\n".join(lines) + "\n")
    buckets = rp.aggregate_file(str(path), "myproject")["usage"]
    assert set(buckets.keys()) == {
        f"2026-09-18T12{rp._SEP}claude-opus-5",
        f"2026-09-18T13{rp._SEP}claude-sonnet-5",
    }
    assert buckets[f"2026-09-18T12{rp._SEP}claude-opus-5"]["output"] == 12
    assert buckets[f"2026-09-18T12{rp._SEP}claude-opus-5"]["messages"] == 2
    assert buckets[f"2026-09-18T13{rp._SEP}claude-sonnet-5"]["messages"] == 1


def test_aggregate_file_dedupes_message_id(tmp_path):
    path = tmp_path / "session.jsonl"
    line = _assistant_line("dup", "claude-opus-5", "2026-09-18T12:00:00.000Z", output_tokens=5)
    path.write_text(line + "\n" + line + "\n")
    buckets = rp.aggregate_file(str(path), "p")["usage"]
    b = buckets[f"2026-09-18T12{rp._SEP}claude-opus-5"]
    assert b["messages"] == 1
    assert b["output"] == 5


def test_aggregate_file_skips_malformed_lines(tmp_path):
    path = tmp_path / "session.jsonl"
    good = _assistant_line("m1", "claude-opus-5", "2026-09-18T12:00:00.000Z")
    path.write_text("{not json\n" + good + "\n\n")
    buckets = rp.aggregate_file(str(path), "p")["usage"]
    assert len(buckets) == 1


def test_aggregate_file_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    assert rp.aggregate_file(str(path), "p") == _EMPTY_AGGREGATE


def test_aggregate_file_missing_file_returns_empty():
    assert rp.aggregate_file("/nonexistent/path.jsonl", "p") == _EMPTY_AGGREGATE


# -- collect_usage: file-level caching keyed by (path, inode, size, mtime) ---


def test_collect_usage_first_call_backfills(tmp_path):
    proj_dir = tmp_path / "projects" / "-home-user-repos-demo"
    proj_dir.mkdir(parents=True)
    (proj_dir / "s1.jsonl").write_text(
        _assistant_line("m1", "claude-opus-5", "2026-09-18T12:00:00.000Z", output_tokens=3) + "\n"
    )
    state_file = str(tmp_path / "state.json")
    out = rp.collect_usage(str(tmp_path / "projects" / "*" / "*.jsonl"), state_file=state_file)
    assert len(out) == 1
    assert out[0]["output"] == 3
    assert out[0]["messages"] == 1


def test_collect_usage_second_call_skips_unchanged_files(tmp_path, monkeypatch):
    proj_dir = tmp_path / "projects" / "-home-user-repos-demo"
    proj_dir.mkdir(parents=True)
    f = proj_dir / "s1.jsonl"
    f.write_text(_assistant_line("m1", "claude-opus-5", "2026-09-18T12:00:00.000Z") + "\n")
    state_file = str(tmp_path / "state.json")
    pattern = str(tmp_path / "projects" / "*" / "*.jsonl")

    first = rp.collect_usage(pattern, state_file=state_file)
    assert len(first) == 1

    calls = []
    orig = rp.aggregate_file

    def spy(path, project):
        calls.append(path)
        return orig(path, project)

    monkeypatch.setattr(rp, "aggregate_file", spy)
    second = rp.collect_usage(pattern, state_file=state_file)
    assert calls == []  # unchanged file -> cache hit, no re-parse
    assert second == first


def test_collect_usage_unchanged_file_cache_survives_multiple_calls(tmp_path, monkeypatch):
    """An unchanged file's cache entry must be carried forward into the saved
    state every call, not just the one call after it was first computed --
    otherwise the second call's state save silently drops it (the "unchanged"
    branch read from old_files but never wrote back into new_files), so the
    THIRD call finds it "not cached" and does a full re-parse, which then
    finds it unchanged again and drops it again: perpetual full-rebuild every
    other call instead of the one-time backfill this cache exists for."""
    proj_dir = tmp_path / "projects" / "-home-user-repos-demo"
    proj_dir.mkdir(parents=True)
    f = proj_dir / "s1.jsonl"
    f.write_text(_assistant_line("m1", "claude-opus-5", "2026-09-18T12:00:00.000Z") + "\n")
    state_file = str(tmp_path / "state.json")
    pattern = str(tmp_path / "projects" / "*" / "*.jsonl")

    first = rp.collect_usage(pattern, state_file=state_file)
    assert len(first) == 1
    second = rp.collect_usage(pattern, state_file=state_file)  # populates the cache-drop bug pre-fix
    assert second == first

    calls = []
    orig = rp.aggregate_file

    def spy(path, project):
        calls.append(path)
        return orig(path, project)

    monkeypatch.setattr(rp, "aggregate_file", spy)
    third = rp.collect_usage(pattern, state_file=state_file)
    assert calls == []  # still a cache hit on the third call, not a full re-parse
    assert third == first


def test_collect_usage_reparses_changed_file(tmp_path):
    proj_dir = tmp_path / "projects" / "-home-user-repos-demo"
    proj_dir.mkdir(parents=True)
    f = proj_dir / "s1.jsonl"
    f.write_text(_assistant_line("m1", "claude-opus-5", "2026-09-18T12:00:00.000Z", output_tokens=3) + "\n")
    state_file = str(tmp_path / "state.json")
    pattern = str(tmp_path / "projects" / "*" / "*.jsonl")

    first = rp.collect_usage(pattern, state_file=state_file)
    assert first[0]["output"] == 3

    with f.open("a") as fh:
        fh.write(_assistant_line("m2", "claude-opus-5", "2026-09-18T12:10:00.000Z", output_tokens=4) + "\n")

    second = rp.collect_usage(pattern, state_file=state_file)
    bucket = next(b for b in second if b["hour"] == "2026-09-18T12" and b["model"] == "claude-opus-5")
    assert bucket["output"] == 7
    assert bucket["messages"] == 2


# -- collect_agents: no herdr on the remote -> [] -----------------------------


def test_collect_agents_returns_empty_list_when_herdr_absent():
    assert rp.collect_agents("definitely-not-a-real-binary-xyz", []) == []


def test_find_herdr_returns_none_for_missing_absolute_path(tmp_path):
    assert rp.find_herdr(str(tmp_path / "no-such-herdr")) is None


def test_find_herdr_empty_string_means_no_herdr_no_path_search(monkeypatch):
    """Bug 2: sources.json's herdr_bin: null is shipped to the probe as "" by
    RemoteCollector (a host explicitly configured to have no herdr). This
    must short-circuit to None without ever falling back to a PATH search --
    a PATH search could accidentally resolve to an unrelated "herdr"-named
    binary on that host."""
    def boom(_name):
        raise AssertionError("must not search PATH for an explicit no-herdr host")

    monkeypatch.setattr(rp.shutil, "which", boom)
    assert rp.find_herdr("") is None


# -- build_result: one broken section never blanks the others ----------------


def test_build_result_survives_worktree_scan_failure(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("git scan exploded")

    monkeypatch.setattr(rp, "collect_worktrees", boom)
    result = rp.build_result(
        "testhost", str(tmp_path / "*" / "*.jsonl"), ["/nonexistent"], ["/"],
        "no-such-herdr", 4, 5.0, 4, str(tmp_path / "state.json"),
        kimi_dir=str(tmp_path / "no-kimi"),
    )
    assert result["worktrees"] == []
    assert result["agents"] == []
    assert "system" in result
    assert result["host"] == "testhost"


def test_build_result_survives_usage_collection_failure(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("usage parse exploded")

    monkeypatch.setattr(rp, "collect_usage_and_analytics", boom)
    result = rp.build_result(
        "testhost", str(tmp_path / "*" / "*.jsonl"), ["/nonexistent"], ["/"],
        "no-such-herdr", 4, 5.0, 4, str(tmp_path / "state.json"),
        kimi_dir=str(tmp_path / "no-kimi"),
    )
    assert result["usage_buckets"] == []
    assert isinstance(result["system"], dict)


def test_build_result_is_json_serializable(tmp_path):
    result = rp.build_result(
        "testhost", str(tmp_path / "*" / "*.jsonl"), ["/nonexistent"], ["/"],
        "no-such-herdr", 4, 5.0, 4, str(tmp_path / "state.json"),
        kimi_dir=str(tmp_path / "no-kimi"),
    )
    json.dumps(result)  # must not raise


@pytest.mark.parametrize("argv", [["myhost"]])
def test_main_prints_one_json_object(tmp_path, capsys, argv, monkeypatch):
    state_file = str(tmp_path / "state.json")
    monkeypatch.setattr(
        rp, "parse_args",
        lambda a: rp.argparse.Namespace(
            host="myhost", projects_glob=str(tmp_path / "*" / "*.jsonl"),
            repo_root=["/nonexistent"], disk_mount=["/"], herdr_bin="no-such-herdr",
            worker_pool=4, git_timeout=5.0, max_depth=4, state_file=state_file,
            productivity_since="30 days ago", session_window=900.0,
            kimi_dir=str(tmp_path / "no-kimi"),
        ),
    )
    rc = rp.main(argv)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    doc = json.loads(out)
    assert doc["host"] == "myhost"
    assert set(doc.keys()) >= {"host", "generated_at", "agents", "worktrees", "system", "usage_buckets"}


# -- drift detector: remote_probe.py's duplicated classifier must match the --
# -- local collectors/analytics.py copy for a fixed sample set. This file is  --
# -- stdlib-only and can't import critdash, so the rule tables are           --
# -- maintained as two copies by hand; this test is the tripwire if they     --
# -- drift apart.                                                            --

_DRIFT_SAMPLE_ERRORS = [
    "String to replace not found in file.",
    "Exit code 2\nls: cannot access 'foo.txt': No such file or directory",
    "Permission for this action was denied by the user.",
    "Exit code 143\nCommand timed out after 2m 0s",
    "Concurrent subagent limit reached. You can run up to N.",
    "json.decoder.JSONDecodeError: Expecting value: line 1 column 1",
    "Exit code 1\nTraceback (most recent call last)",
    "completely unrecognized failure text with no keywords",
]

_DRIFT_SAMPLE_API_ERRORS = [
    {"apiErrorStatus": 529, "error": "server_error"},
    {"apiErrorStatus": 429, "error": "rate_limit"},
    {"apiErrorStatus": 400, "error": "invalid_request"},
    {"apiErrorStatus": None, "error": "authentication_failed"},
    {"apiErrorStatus": 500},
]


@pytest.mark.parametrize("text", _DRIFT_SAMPLE_ERRORS)
def test_classify_error_matches_local_analytics_copy(text):
    assert rp.classify_error(text) == local_an.classify_error(text)


@pytest.mark.parametrize("doc", _DRIFT_SAMPLE_API_ERRORS)
def test_classify_api_error_matches_local_analytics_copy(doc):
    assert rp.classify_api_error(doc) == local_an.classify_api_error(doc)


# -- aggregate_file: wave-2 categories (tools/errors/api_errors/sessions) ----


def _assistant_tool_use_line(uuid, ts, tool_use_id, tool_name, tool_input=None, session="s1"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "timestamp": ts, "sessionId": session, "isSidechain": False,
        "message": {"content": [{"type": "tool_use", "id": tool_use_id, "name": tool_name,
                                   "input": tool_input or {}}]},
    })


def _user_tool_result_line(uuid, ts, tool_use_id, is_error, content, session="s1"):
    return json.dumps({
        "type": "user", "uuid": uuid, "timestamp": ts, "sessionId": session, "isSidechain": False,
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                                   "is_error": is_error, "content": content}]},
    })


def test_aggregate_file_tool_use_and_error_correlation(tmp_path):
    path = tmp_path / "session.jsonl"
    lines = [
        _assistant_tool_use_line("u1", "2026-09-18T10:00:00Z", "toolu_1", "Edit", {"file_path": "a.py"}),
        _user_tool_result_line("u2", "2026-09-18T10:00:01Z", "toolu_1", True,
                                "String to replace not found in file."),
        _assistant_tool_use_line("u3", "2026-09-18T10:00:02Z", "toolu_2", "Bash", {"command": "ls"}),
        _user_tool_result_line("u4", "2026-09-18T10:00:03Z", "toolu_2", False, "ok"),
    ]
    path.write_text("\n".join(lines) + "\n")
    buckets = rp.aggregate_file(str(path), "p")

    tools = buckets["tools"]
    assert tools[f"2026-09-18{rp._SEP}Edit"] == {"calls": 1, "errors": 1}
    assert tools[f"2026-09-18{rp._SEP}Bash"] == {"calls": 1, "errors": 0}

    errors = buckets["errors"]
    err_key = f"2026-09-18{rp._SEP}string_not_found{rp._SEP}Edit"
    assert errors[err_key]["count"] == 1

    trouble = buckets["trouble_files"]
    assert trouble[f"2026-09-18{rp._SEP}a.py{rp._SEP}Edit"]["errors"] == 1


def test_aggregate_file_api_error_bucket(tmp_path):
    path = tmp_path / "session.jsonl"
    line = json.dumps({
        "type": "assistant", "uuid": "u1", "timestamp": "2026-09-18T10:00:00Z",
        "isApiErrorMessage": True, "error": "rate_limit", "apiErrorStatus": 429,
        "message": {"content": [{"type": "text", "text": "limit hit"}]},
    })
    path.write_text(line + "\n")
    buckets = rp.aggregate_file(str(path), "p")
    assert buckets["api_errors"][f"2026-09-18{rp._SEP}rate_limit"]["count"] == 1
    assert buckets["usage"] == {}  # api error rows never contribute usage tokens


def test_aggregate_file_session_buckets_split_by_sidechain(tmp_path):
    path = tmp_path / "session.jsonl"

    def _assistant(uuid, ts, mid, sidechain):
        return json.dumps({
            "type": "assistant", "uuid": uuid, "timestamp": ts, "sessionId": "sess-x",
            "isSidechain": sidechain,
            "message": {"id": mid, "model": "claude-sonnet-5",
                        "usage": {"input_tokens": 10, "output_tokens": 5}},
        })

    lines = [
        _assistant("u1", "2026-09-18T10:00:00Z", "m1", False),
        _assistant("u2", "2026-09-18T10:00:01Z", "m2", True),
    ]
    path.write_text("\n".join(lines) + "\n")
    buckets = rp.aggregate_file(str(path), "myproj")
    sessions = buckets["sessions"]
    main_key = f"2026-09-18{rp._SEP}sess-x{rp._SEP}0{rp._SEP}claude-sonnet-5"
    side_key = f"2026-09-18{rp._SEP}sess-x{rp._SEP}1{rp._SEP}claude-sonnet-5"
    assert sessions[main_key]["messages"] == 1
    assert sessions[side_key]["messages"] == 1
    assert sessions[main_key]["project"] == "myproj"


# -- collect_usage_and_analytics: merge across categories, cache reuse -------


def test_collect_usage_and_analytics_merges_all_categories(tmp_path):
    proj_dir = tmp_path / "projects" / "-home-user-repos-demo"
    proj_dir.mkdir(parents=True)
    lines = [
        _assistant_tool_use_line("u1", "2026-09-18T10:00:00Z", "toolu_1", "Edit", {"file_path": "a.py"}),
        _user_tool_result_line(
            "u2", "2026-09-18T10:00:01Z", "toolu_1", True,
            "Permission for this action was denied by the user.",
        ),
    ]
    (proj_dir / "s1.jsonl").write_text("\n".join(lines) + "\n")
    state_file = str(tmp_path / "state.json")
    pattern = str(tmp_path / "projects" / "*" / "*.jsonl")

    result = rp.collect_usage_and_analytics(pattern, state_file=state_file)
    assert result["tool_buckets"] == [{"day": "2026-09-18", "tool": "Edit", "calls": 1, "errors": 1}]
    assert result["error_buckets"][0]["kind"] == "permission_denied"
    assert len(result["error_examples"]) == 1


def test_collect_usage_and_analytics_second_call_reuses_cache(tmp_path, monkeypatch):
    proj_dir = tmp_path / "projects" / "-home-user-repos-demo"
    proj_dir.mkdir(parents=True)
    (proj_dir / "s1.jsonl").write_text(
        _assistant_tool_use_line("u1", "2026-09-18T10:00:00Z", "toolu_1", "Edit") + "\n"
    )
    state_file = str(tmp_path / "state.json")
    pattern = str(tmp_path / "projects" / "*" / "*.jsonl")

    first = rp.collect_usage_and_analytics(pattern, state_file=state_file)
    assert first["tool_buckets"] == [{"day": "2026-09-18", "tool": "Edit", "calls": 1, "errors": 0}]

    calls = []
    orig = rp.aggregate_file

    def spy(path, project):
        calls.append(path)
        return orig(path, project)

    monkeypatch.setattr(rp, "aggregate_file", spy)
    second = rp.collect_usage_and_analytics(pattern, state_file=state_file)
    assert calls == []  # unchanged file -> cache hit, no re-parse
    assert second["tool_buckets"] == first["tool_buckets"]


# -- collect_productivity: real per-day bucketing, no rolling-window overlap --


def _init_git_repo(path, subprocess_module):
    subprocess_module.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess_module.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess_module.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "a.txt").write_text("hello\n")
    subprocess_module.run(["git", "add", "a.txt"], cwd=path, check=True)
    subprocess_module.run(["git", "commit", "-qm", "first"], cwd=path, check=True)


def test_collect_productivity_buckets_by_real_commit_day(tmp_path):
    import subprocess as sp

    repo = tmp_path / "repo1"
    repo.mkdir()
    _init_git_repo(repo, sp)

    worktrees = [{"path": str(repo), "repo": "repo1"}]
    rows = rp.collect_productivity(worktrees, worker_pool=4, git_timeout_s=5.0)
    assert len(rows) == 1
    assert rows[0]["repo"] == "repo1"
    assert rows[0]["commits"] == 1
    import datetime as _dt

    assert rows[0]["day"] == _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d")


def test_collect_productivity_empty_worktrees_returns_empty():
    assert rp.collect_productivity([], worker_pool=4, git_timeout_s=5.0) == []


def test_collect_productivity_survives_bad_path():
    rows = rp.collect_productivity(
        [{"path": "/nonexistent/xyz", "repo": "ghost"}], worker_pool=4, git_timeout_s=2.0
    )
    assert rows == []


# -- scan_session_agents / build_session_agent / merge (Bug 2, remote fleet) -


def _write_session_jsonl(path, session_id, cwd, git_branch, model, mtime_age_s):
    import os
    import time

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "assistant", "sessionId": session_id, "cwd": cwd, "gitBranch": git_branch,
        "timestamp": "2026-09-18T12:00:00Z", "message": {"model": model, "id": "msg_1"},
    }) + "\n")
    now = time.time()
    os.utime(path, (now - mtime_age_s, now - mtime_age_s))


def test_scan_session_agents_finds_recent_session(tmp_path):
    _write_session_jsonl(
        tmp_path / "-home-user2-demo-notes" / "39eb414c.jsonl", "39eb414c-b039",
        "/home/user2/demo-notes", "main", "claude-sonnet-5", mtime_age_s=60,
    )
    results = rp.scan_session_agents(str(tmp_path / "*/*.jsonl"), window_s=900)
    assert len(results) == 1
    r = results[0]
    assert r["session_id"] == "39eb414c-b039"
    assert r["cwd"] == "/home/user2/demo-notes"
    assert r["git_branch"] == "main"
    assert r["model"] == "claude-sonnet-5"
    assert r["status"] == "working"


def test_scan_session_agents_finds_model_when_last_line_has_none(tmp_path):
    # The exact shape verified live on host-c: the last line in the jsonl
    # is a "system" event with no message.model, a couple of lines after the
    # last real assistant turn. Must still surface that model, not None.
    d = tmp_path / "-home-user2-demo-notes"
    d.mkdir(parents=True)
    path = d / "sess.jsonl"
    lines = [
        json.dumps({
            "type": "assistant", "sessionId": "39eb414c-b039", "cwd": "/home/user2/demo-notes",
            "gitBranch": "main", "timestamp": "2026-09-18T13:26:35Z",
            "message": {"model": "claude-sonnet-5", "id": "msg_1"},
        }),
        json.dumps({
            "type": "system", "sessionId": "39eb414c-b039", "cwd": "/home/user2/demo-notes",
            "gitBranch": "main", "timestamp": "2026-09-18T13:29:37Z",
        }),
    ]
    path.write_text("\n".join(lines) + "\n")
    import os
    import time
    now = time.time()
    os.utime(path, (now - 60, now - 60))

    results = rp.scan_session_agents(str(tmp_path / "*/*.jsonl"), window_s=900)
    assert len(results) == 1
    assert results[0]["model"] == "claude-sonnet-5"


def test_scan_session_agents_excludes_stale_files(tmp_path):
    _write_session_jsonl(
        tmp_path / "-home-user2-old" / "old.jsonl", "old-sess", "/home/user2/old",
        "main", "claude-sonnet-5", mtime_age_s=3600,
    )
    assert rp.scan_session_agents(str(tmp_path / "*/*.jsonl"), window_s=900) == []


def test_build_session_agent_fields_and_worktree_repo_match():
    rec = {
        "session_id": "s1", "cwd": "/home/user2/demo-notes", "git_branch": "main",
        "model": "claude-sonnet-5", "mtime": "2026-09-18T18:23:53Z", "status": "working",
    }
    worktrees = [{"path": "/home/user2/demo-notes", "repo": "demo-notes", "branch": "main"}]
    a = rp.build_session_agent(rec, worktrees)
    assert a["session_id"] == "s1"
    assert a["repo"] == "demo-notes"
    assert a["model"] == "claude-sonnet-5"
    assert a["source"] == "session"
    assert a["kind"] == "claude"
    assert a["pane"] is None


def test_merge_remote_agent_sources_dedupes_by_session_id():
    herdr = [{
        "id": "s1", "kind": "claude", "status": "working", "cwd": "/herdr/cwd", "repo": None,
        "branch": None, "pane": "p1", "workspace": "w1", "title": "herdr-title", "label": "x",
        "focused": True, "session_id": "s1", "bead": None, "last_activity": None,
        "status_since": None, "tokens_today": {}, "cost_today_usd": 0.0, "msg_count_today": 0,
        "subagents_active": 0, "model": None, "source": "herdr",
    }]
    session = [rp.build_session_agent({
        "session_id": "s1", "cwd": "/jsonl/cwd", "git_branch": "main", "model": "claude-sonnet-5",
        "mtime": "2026-09-18T18:23:53Z", "status": "idle",
    }, [])]
    merged = rp.merge_remote_agent_sources(herdr, session)
    assert len(merged) == 1
    a = merged[0]
    assert a["source"] == "both"
    assert a["cwd"] == "/jsonl/cwd"
    assert a["pane"] == "p1"
    assert a["status"] == "working"


def test_build_result_surfaces_session_only_agent_when_herdr_reports_none(tmp_path):
    # Reproduces the exact host-c symptom: herdr agent list -> [], but a
    # real session's jsonl is actively being written to.
    _write_session_jsonl(
        tmp_path / "-home-user2-demo-notes" / "39eb414c.jsonl", "39eb414c-b039",
        "/home/user2/demo-notes", "main", "claude-sonnet-5", mtime_age_s=30,
    )
    result = rp.build_result(
        "host-c", str(tmp_path / "*" / "*.jsonl"), ["/nonexistent"], ["/"],
        "no-such-herdr", 4, 5.0, 4, str(tmp_path / "state.json"),
        session_window_s=900.0, kimi_dir=str(tmp_path / "no-kimi"),
    )
    assert len(result["agents"]) == 1
    assert result["agents"][0]["session_id"] == "39eb414c-b039"
    assert result["agents"][0]["source"] == "session"
    # regression: a session found by scanning claude_projects_dir is known to
    # be produced by Claude Code even when herdr has never heard of it -- and
    # this is the exact host-c symptom the fix targets.
    assert result["agents"][0]["kind"] == "claude"


def test_merge_remote_agent_sources_keeps_herdr_kind_when_it_differs():
    # A session-only build defaults kind to "claude", but herdr stays
    # authoritative when it also reports the session -- it may legitimately
    # report a different kind.
    herdr = [{
        "id": "s1", "kind": "codex", "status": "working", "cwd": "/herdr/cwd", "repo": None,
        "branch": None, "pane": "p1", "workspace": "w1", "title": "herdr-title", "label": "x",
        "focused": True, "session_id": "s1", "bead": None, "last_activity": None,
        "status_since": None, "tokens_today": {}, "cost_today_usd": 0.0, "msg_count_today": 0,
        "subagents_active": 0, "model": None, "source": "herdr",
    }]
    session = [rp.build_session_agent({
        "session_id": "s1", "cwd": "/jsonl/cwd", "git_branch": "main", "model": "claude-sonnet-5",
        "mtime": "2026-09-18T18:23:53Z", "status": "idle",
    }, [])]
    merged = rp.merge_remote_agent_sources(herdr, session)
    assert len(merged) == 1
    assert merged[0]["source"] == "both"
    assert merged[0]["kind"] == "codex"


def test_parse_args_session_window_default():
    args = rp.parse_args(["myhost"])
    assert args.session_window == 900.0


# -- Kimi (second provider, briefing 2026-09-18): remote-host parity ---------


def test_parse_args_kimi_dir_default():
    args = rp.parse_args(["myhost"])
    assert args.kimi_dir == "~/.kimi-code"


def test_collect_kimi_missing_dir_is_silent_noop(tmp_path):
    result = rp.collect_kimi(str(tmp_path / "no-such-kimi-dir"), [], 900.0, str(tmp_path / "state.json"))
    assert result == {"agents": [], "kimi_usage_buckets": [], "kimi_error_buckets": []}


def test_collect_kimi_discovers_sessions_and_buckets(make_kimi_root, tmp_path):
    root = make_kimi_root(age_s_1=300.0, age_s_2=30.0)
    result = rp.collect_kimi(str(root), [], 900.0, str(tmp_path / "state.json"))

    ids = {a["session_id"] for a in result["agents"]}
    assert ids == {"session_test-uuid-1", "session_test-uuid-2"}
    for a in result["agents"]:
        assert a["kind"] == "kimi"
        assert a["cost_today_usd"] is None

    total_tokens = sum(b["tokens"] for b in result["kimi_usage_buckets"])
    assert total_tokens == 208 + 22402 + 598

    kinds = {b["kind"] for b in result["kimi_error_buckets"]}
    assert kinds == {"quota_exceeded"}


def test_collect_kimi_session_outside_window_excluded_from_agents(make_kimi_root, tmp_path):
    root = make_kimi_root(age_s_1=3000.0, age_s_2=30.0)
    result = rp.collect_kimi(str(root), [], 900.0, str(tmp_path / "state.json"))
    ids = {a["session_id"] for a in result["agents"]}
    assert ids == {"session_test-uuid-2"}
    # the OUT-of-window session's turn still gets counted in usage buckets --
    # window filtering is an agent-liveness concept, not a usage-history one.
    total_tokens = sum(b["tokens"] for b in result["kimi_usage_buckets"])
    assert total_tokens == 208 + 22402 + 598


def test_collect_kimi_caches_unchanged_files(make_kimi_root, tmp_path, monkeypatch):
    root = make_kimi_root()
    state_file = str(tmp_path / "state.json")
    rp.collect_kimi(str(root), [], 900.0, state_file)

    def boom(path):
        raise AssertionError(f"aggregate_kimi_wire_file called again for unchanged {path}")

    monkeypatch.setattr(rp, "aggregate_kimi_wire_file", boom)
    result = rp.collect_kimi(str(root), [], 900.0, state_file)  # must use the cache, not re-parse
    total_tokens = sum(b["tokens"] for b in result["kimi_usage_buckets"])
    assert total_tokens == 208 + 22402 + 598


def test_collect_kimi_shares_state_file_with_claude_usage_without_clobbering(make_kimi_root, tmp_path):
    root = make_kimi_root()
    state_file = str(tmp_path / "state.json")
    # Claude usage collection runs first and writes its own "files" key...
    rp.collect_usage_and_analytics(str(tmp_path / "claude-projects" / "*" / "*.jsonl"), state_file)
    # ...collect_kimi must not wipe that key out when it saves its own.
    rp.collect_kimi(str(root), [], 900.0, state_file)
    state = rp.load_state(state_file)
    assert "files" in state
    assert "kimi_files" in state


def test_build_result_includes_kimi_agent_with_kind_kimi(make_kimi_root, tmp_path):
    root = make_kimi_root(age_s_1=300.0, age_s_2=30.0)
    result = rp.build_result(
        "testhost", str(tmp_path / "*" / "*.jsonl"), ["/nonexistent"], ["/"],
        "no-such-herdr", 4, 5.0, 4, str(tmp_path / "state.json"),
        kimi_dir=str(root),
    )
    kimi_agents = [a for a in result["agents"] if a["kind"] == "kimi"]
    assert len(kimi_agents) == 2
    for a in kimi_agents:
        assert a["cost_today_usd"] is None
    assert "kimi_usage_buckets" in result
    assert "kimi_error_buckets" in result
    json.dumps(result)  # must still be JSON-serializable with Kimi data present


# -- session-bead extraction (resolve_remote_session_bead) -- stateless,
# bounded-tail duplicate of bead_sessions.py's logic. Synthetic fixtures
# only (see that module's test file for the shared fixture-building style).
# ---------------------------------------------------------------------------

_CLAIM_OK = "✓ Updated issue: demo-a — demo title"
_CLOSE_OK = "✓ Closed demo-a — demo title"


def _rp_claude_lines(*pairs):
    lines = []
    for tool_id, command, result_text, is_error in pairs:
        lines.append(json.dumps({
            "timestamp": "2026-09-23T00:00:00.000Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tool_id, "name": "Bash",
                 "input": {"command": command, "description": "run"}},
            ]},
        }))
        lines.append(json.dumps({
            "timestamp": "2026-09-23T00:00:01.000Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "is_error": is_error,
                 "content": result_text},
            ]},
        }))
    return "\n".join(lines) + "\n"


def _rp_kimi_lines(*pairs):
    lines = []
    t = 1000
    for tool_id, command, result_text in pairs:
        lines.append(json.dumps({
            "type": "agent.message.appended", "time": t,
            "message": {"message": {"role": "assistant", "toolCalls": [
                {"type": "function", "id": tool_id, "name": "Bash",
                 "arguments": json.dumps({"command": command})},
            ]}},
        }))
        lines.append(json.dumps({
            "type": "agent.message.appended", "time": t + 1,
            "message": {"message": {"role": "tool", "toolCallId": tool_id,
                                     "content": [{"type": "text", "text": result_text}]}},
        }))
        t += 10
    return "\n".join(lines) + "\n"


def test_resolve_remote_session_bead_claude_claim(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_rp_claude_lines(("t1", "bd update demo-a --claim", _CLAIM_OK, False)))
    assert rp.resolve_remote_session_bead("claude", [str(p)]) == ["demo-a"]


def test_resolve_remote_session_bead_claude_claim_then_close_null(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_rp_claude_lines(
        ("t1", "bd update demo-a --claim", _CLAIM_OK, False),
        ("t2", "bd close demo-a", _CLOSE_OK, False),
    ))
    assert rp.resolve_remote_session_bead("claude", [str(p)]) == []


def test_resolve_remote_session_bead_claude_failed_claim_ignored(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_rp_claude_lines(
        ("t1", "bd update demo-a --claim", "already claimed by other", False),
    ))
    assert rp.resolve_remote_session_bead("claude", [str(p)]) == []


def test_resolve_remote_session_bead_kimi_claim(tmp_path):
    p = tmp_path / "wire.jsonl"
    p.write_text(_rp_kimi_lines(("k1", "bd update demo-a --claim", _CLAIM_OK)))
    assert rp.resolve_remote_session_bead("kimi", [str(p)]) == ["demo-a"]


def test_resolve_remote_session_bead_untracked_kind_returns_none(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_rp_claude_lines(("t1", "bd update demo-a --claim", _CLAIM_OK, False)))
    assert rp.resolve_remote_session_bead("opencode", [str(p)]) == []


def test_resolve_remote_session_bead_untracked_kind_not_parsed_via_kimi_fallback(tmp_path):
    # An untracked kind must be rejected up front, not silently fall through
    # to the Kimi scanner (the `else` branch of the kind->scanner pick) just
    # because it isn't literally "claude".
    p = tmp_path / "wire.jsonl"
    p.write_text(_rp_kimi_lines(("k1", "bd update demo-a --claim", _CLAIM_OK)))
    assert rp.resolve_remote_session_bead("notarealkind", [str(p)]) == []


def test_resolve_remote_session_bead_multiple_unreleased_newest_first(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_rp_claude_lines(
        ("t1", "bd update demo-a --claim", _CLAIM_OK, False),
        ("t2", "bd update demo-b --claim", "✓ Updated issue: demo-b — t", False),
    ))
    assert rp.resolve_remote_session_bead("claude", [str(p)]) == ["demo-b", "demo-a"]


def test_resolve_remote_session_bead_returns_only_ids_never_command_text():
    # Contract check: the function's return type is exactly `list[str]` --
    # never a dict/tuple (or anything carrying a timestamp) that could
    # smuggle command text or transcript content off the remote host.
    import inspect
    sig = inspect.signature(rp.resolve_remote_session_bead)
    assert sig.return_annotation == "list[str]"


def test_resolve_remote_session_bead_caps_at_max_claims(tmp_path):
    p = tmp_path / "s.jsonl"
    pairs = [
        (f"t{i}", f"bd update demo-{i} --claim", f"✓ Updated issue: demo-{i} — t", False)
        for i in range(15)
    ]
    p.write_text(_rp_claude_lines(*pairs))
    result = rp.resolve_remote_session_bead("claude", [str(p)])
    assert len(result) == rp._BD_MAX_CLAIMS
    assert result == [f"demo-{i}" for i in range(14, 14 - rp._BD_MAX_CLAIMS, -1)]


def test_build_session_agent_carries_bead_claims_from_transcript(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_rp_claude_lines(("t1", "bd update demo-a --claim", _CLAIM_OK, False)))
    rec = {
        "session_id": "sess-1", "cwd": "/srv/demo", "git_branch": "main",
        "model": None, "mtime": "2026-09-23T00:00:00Z", "status": "working", "path": str(p),
    }
    agent = rp.build_session_agent(rec, [])
    assert agent["bead_claims"] == ["demo-a"]
    assert agent["bead_tracked"] is True
    assert "bead" not in agent


# -- Defect 1: quoting honoured before splitting; heredocs/substitutions ----


def test_rp_defect1_quoted_multiline_description_with_embedded_fake_claims(tmp_path):
    handoff_text = (
        ". ~/.config/beads/env\n"
        "export BEADS_ACTOR=demo-actor-grok\n"
        "bd update $id --claim\n"
        "bd update demo-9 --claim\n"
        "bd show $id\n"
    )
    cmd = (
        'id=$(bd create "handoff bead" --json --assignee "")\n'
        f'bd update "$id" -d "{handoff_text}"\n'
        'bd assign "$id" ""\n'
        'bd label add "$id" needs-demo-grok\n'
    )
    p = tmp_path / "s.jsonl"
    p.write_text(_rp_claude_lines(("t1", cmd, "✓ Updated issue: demo-x — handoff bead", False)))
    assert rp.resolve_remote_session_bead("claude", [str(p)]) == []


def test_rp_defect1_heredoc_body_with_literal_claim_not_counted(tmp_path):
    cmd = "cat <<EOF\nbd update demo-1 --claim\nEOF\n"
    p = tmp_path / "s.jsonl"
    p.write_text(_rp_claude_lines(("t1", cmd, "ok", False)))
    assert rp.resolve_remote_session_bead("claude", [str(p)]) == []


def test_rp_defect1_timeout_pipe_redirection_claim_counted(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_rp_claude_lines(
        ("t1", "timeout 60 bd update demo-1 --claim 2>&1 | tail -1", "✓ Updated issue: demo-1 — t", False),
    ))
    assert rp.resolve_remote_session_bead("claude", [str(p)]) == ["demo-1"]


def test_rp_defect1_command_substitution_as_id_argument_skipped(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_rp_claude_lines(
        ("t1", "bd update $(cat f) --claim", "✓ Updated issue: demo-1 — t", False),
    ))
    assert rp.resolve_remote_session_bead("claude", [str(p)]) == []


# -- Defect 2: non-literal ids skipped, never inferred from result text -----


def test_rp_defect2_bare_dollar_var_id_skipped(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_rp_claude_lines(
        ("t1", "bd update $BID --claim", "✓ Updated issue: demo-a — t", False),
    ))
    assert rp.resolve_remote_session_bead("claude", [str(p)]) == []


# -- bd assign -- release --------------------------------------------------


def test_rp_assign_empty_after_claim_releases(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_rp_claude_lines(
        ("t1", "bd update demo-a --claim", _CLAIM_OK, False),
        ("t2", 'bd assign demo-a ""', "✓ Unassigned demo-a — t", False),
    ))
    assert rp.resolve_remote_session_bead("claude", [str(p)]) == []


# -- parity: the local (bead_sessions.py) and remote (this module) bead
# extractors MUST behave identically on the same fixture set -- both are
# hand-maintained duplicates (this file can't import critdash), so this is
# the tripwire if they drift apart, same pattern as the classify_error
# drift test above.


_PARITY_CLAUDE_SCENARIOS: tuple[tuple[str, tuple[tuple[str, str, str, bool], ...]], ...] = (
    ("plain_claim", (("t1", "bd update demo-a --claim", _CLAIM_OK, False),)),
    ("claim_then_close", (
        ("t1", "bd update demo-a --claim", _CLAIM_OK, False),
        ("t2", "bd close demo-a", _CLOSE_OK, False),
    )),
    ("failed_already_claimed", (
        ("t1", "bd update demo-a --claim", "already claimed by other", False),
    )),
    ("is_error_true", (("t1", "bd update demo-a --claim", _CLAIM_OK, True),)),
    ("compound_wrapped", (
        (
            "t1",
            "cd /srv/demo && . env; export BEADS_ACTOR=y; "
            "timeout 60 bd update demo-a --claim 2>&1 | tail -1",
            _CLAIM_OK, False,
        ),
    )),
    ("newline_separated", (
        ("t1", "export BEADS_ACTOR=y\nbd update demo-a --claim", _CLAIM_OK, False),
    )),
    ("echo_not_counted", (
        ("t1", 'echo "bd update demo-a --claim"', "bd update demo-a --claim", False),
    )),
    ("grep_not_counted", (
        ("t1", "grep -- '--claim' notes.txt", "notes.txt:1: --claim", False),
    )),
    ("heredoc_body_not_counted", (
        ("t1", "cat <<EOF\nbd update demo-a --claim\nEOF\n", "ok", False),
    )),
    ("command_substitution_id_skipped", (
        ("t1", "bd update $(cat f) --claim", _CLAIM_OK, False),
    )),
    ("variable_id_skipped", (
        ("t1", "bd update $BID --claim", _CLAIM_OK, False),
    )),
    ("defect1_quoted_multiline_with_fake_claims", (
        (
            "t1",
            'id=$(bd create "h" --json)\n'
            'bd update "$id" -d "export BEADS_ACTOR=y\nbd update $id --claim\n'
            'bd update demo-9 --claim"\n'
            'bd assign "$id" ""\n',
            "✓ Updated issue: demo-x — h",
            False,
        ),
    )),
    ("assign_unassign_releases", (
        ("t1", "bd update demo-a --claim", _CLAIM_OK, False),
        ("t2", 'bd assign demo-a ""', "✓ Unassigned demo-a — t", False),
    )),
    ("assign_to_someone_releases", (
        ("t1", "bd update demo-a --claim", _CLAIM_OK, False),
        ("t2", "bd assign demo-a demo-actor-alice", "✓ Assigned demo-a — t to demo-actor-alice", False),
    )),
    ("claim_a_then_b_both_unreleased", (
        ("t1", "bd update demo-a --claim", _CLAIM_OK, False),
        ("t2", "bd update demo-b --claim", "✓ Updated issue: demo-b — t", False),
    )),
)


@pytest.mark.parametrize("name,pairs", _PARITY_CLAUDE_SCENARIOS, ids=[s[0] for s in _PARITY_CLAUDE_SCENARIOS])
def test_bead_extraction_parity_claude(tmp_path, name, pairs):
    p = tmp_path / f"{name}.jsonl"
    p.write_text(_rp_claude_lines(*pairs))
    local_claims = local_bs.resolve_session_bead("claude", [str(p)], {})
    remote_ids = rp.resolve_remote_session_bead("claude", [str(p)])
    assert [bid for bid, _ts in local_claims] == remote_ids


_PARITY_KIMI_SCENARIOS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    ("plain_claim", (("k1", "bd update demo-a --claim", _CLAIM_OK),)),
    ("claim_then_close", (
        ("k1", "bd update demo-a --claim", _CLAIM_OK),
        ("k2", "bd close demo-a", _CLOSE_OK),
    )),
    ("already_claimed", (("k1", "bd update demo-a --claim", "already claimed by other"),)),
    ("assign_releases", (
        ("k1", "bd update demo-a --claim", _CLAIM_OK),
        ("k2", 'bd assign demo-a ""', "✓ Unassigned demo-a — t"),
    )),
)


@pytest.mark.parametrize("name,pairs", _PARITY_KIMI_SCENARIOS, ids=[s[0] for s in _PARITY_KIMI_SCENARIOS])
def test_bead_extraction_parity_kimi(tmp_path, name, pairs):
    p = tmp_path / f"{name}.jsonl"
    p.write_text(_rp_kimi_lines(*pairs))
    local_claims = local_bs.resolve_session_bead("kimi", [str(p)], {})
    remote_ids = rp.resolve_remote_session_bead("kimi", [str(p)])
    assert [bid for bid, _ts in local_claims] == remote_ids


# -- parity: Codex, Grok, Cursor (see bead_sessions.py's module docstring
# for the verified on-disk shape each fixture builder below mirrors) ------


def _rp_codex_lines(*items):
    lines = []
    t = 1000
    for command, exit_code, status, stdout in items:
        lines.append(json.dumps({
            "type": "event_msg", "timestamp": "2026-09-23T00:00:00.000Z",
            "payload": {
                "type": "item_completed", "completed_at_ms": t,
                "item": {
                    "type": "CommandExecution", "command": ["/bin/bash", "-lc", command],
                    "status": status, "exit_code": exit_code, "stdout": stdout, "stderr": "",
                    "aggregated_output": stdout,
                },
            },
        }))
        t += 10
    return "\n".join(lines) + "\n"


_PARITY_CODEX_SCENARIOS: tuple[tuple[str, tuple[tuple[str, int, str, str], ...]], ...] = (
    ("plain_claim", (("bd update demo-a --claim", 0, "completed", _CLAIM_OK),)),
    ("claim_then_close", (
        ("bd update demo-a --claim", 0, "completed", _CLAIM_OK),
        ("bd close demo-a", 0, "completed", _CLOSE_OK),
    )),
    ("failed_exit_code", (("bd update demo-a --claim", 1, "failed", "permission denied"),)),
    ("compound_wrapped", (("cd /srv/demo && bd update demo-a --claim", 0, "completed", _CLAIM_OK),)),
    ("echo_not_counted", (
        ('echo "bd update demo-a --claim"', 0, "completed", "bd update demo-a --claim"),
    )),
    ("variable_id_skipped", (("bd update $BID --claim", 0, "completed", _CLAIM_OK),)),
)


@pytest.mark.parametrize("name,items", _PARITY_CODEX_SCENARIOS, ids=[s[0] for s in _PARITY_CODEX_SCENARIOS])
def test_bead_extraction_parity_codex(tmp_path, name, items):
    p = tmp_path / f"{name}.jsonl"
    p.write_text(_rp_codex_lines(*items))
    local_claims = local_bs.resolve_session_bead("codex", [str(p)], {})
    remote_ids = rp.resolve_remote_session_bead("codex", [str(p)])
    assert [bid for bid, _ts in local_claims] == remote_ids


def _rp_grok_files(tmp_path, name, *pairs):
    chat_lines = []
    event_lines = []
    for i, (tool_id, command, outcome) in enumerate(pairs):
        chat_lines.append(json.dumps({
            "type": "assistant", "content": "demo turn",
            "tool_calls": [{
                "id": tool_id, "name": "run_terminal_command",
                "arguments": json.dumps({"command": command, "description": "run"}),
            }],
        }))
        # chat_history.jsonl's own result text is pruned/untrustworthy on a
        # real host (see bead_sessions.py's module docstring) -- every
        # fixture uses that same placeholder to prove events.jsonl's
        # "outcome" is what actually decides success/failure here.
        chat_lines.append(json.dumps({
            "type": "tool_result", "tool_call_id": tool_id, "content": "[Tool result omitted — too old]",
        }))
        event_lines.append(json.dumps({
            "ts": f"2026-09-23T00:00:{i:02d}.000Z", "type": "tool_completed",
            "tool_name": "run_terminal_command", "duration_ms": 5,
            "outcome": outcome, "tool_call_id": tool_id,
        }))
    d = tmp_path / name
    d.mkdir()
    chat_p = d / "chat_history.jsonl"
    chat_p.write_text("\n".join(chat_lines) + "\n")
    events_p = d / "events.jsonl"
    events_p.write_text("\n".join(event_lines) + "\n")
    return [str(chat_p), str(events_p)]


_PARITY_GROK_SCENARIOS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    ("plain_claim", (("g1", "bd update demo-a --claim", "success"),)),
    ("claim_then_close", (
        ("g1", "bd update demo-a --claim", "success"),
        ("g2", "bd close demo-a", "success"),
    )),
    ("failed_outcome", (("g1", "bd update demo-a --claim", "error"),)),
    ("compound_wrapped", (("g1", "cd /srv/demo && bd update demo-a --claim", "success"),)),
    ("echo_not_counted", (("g1", 'echo "bd update demo-a --claim"', "success"),)),
    ("variable_id_skipped", (("g1", "bd update $BID --claim", "success"),)),
)


@pytest.mark.parametrize("name,pairs", _PARITY_GROK_SCENARIOS, ids=[s[0] for s in _PARITY_GROK_SCENARIOS])
def test_bead_extraction_parity_grok(tmp_path, name, pairs):
    paths = _rp_grok_files(tmp_path, name, *pairs)
    local_claims = local_bs.resolve_session_bead("grok", paths, {})
    remote_ids = rp.resolve_remote_session_bead("grok", paths)
    assert [bid for bid, _ts in local_claims] == remote_ids


def _rp_cursor_db(tmp_path, name, *pairs):
    path = tmp_path / name
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    idx = 0
    for tool_id, command, is_error in pairs:
        call_row = {
            "role": "assistant",
            "content": [{
                "type": "tool-call", "toolCallId": tool_id, "toolName": "Shell",
                "args": {"command": command, "description": "run"},
            }],
            "id": f"demo-msg-{idx}",
        }
        con.execute("INSERT INTO blobs (id, data) VALUES (?, ?)", (f"demo-blob-{idx}", json.dumps(call_row)))
        idx += 1
        if is_error:
            hltcr = {"output": ["demo error text"], "isError": True, "rawErrorMessages": ["demo error text"]}
        else:
            hltcr = {
                "output": {"command": command, "stdout": "ok", "executionTime": 1, "localExecutionTimeMs": 1},
                "isError": False,
            }
        result_row = {
            "role": "tool",
            "content": [{
                "type": "tool-result", "toolCallId": tool_id, "result": "ok",
                "experimental_content": [{"type": "text", "text": "ok"}],
            }],
            "id": f"demo-msg-{idx}",
            "providerOptions": {"cursor": {"highLevelToolCallResult": hltcr}},
        }
        con.execute(
            "INSERT INTO blobs (id, data) VALUES (?, ?)", (f"demo-blob-{idx}", json.dumps(result_row))
        )
        idx += 1
    con.commit()
    con.close()
    return str(path)


_PARITY_CURSOR_SCENARIOS: tuple[tuple[str, tuple[tuple[str, str, bool], ...]], ...] = (
    ("plain_claim", (("c1", "bd update demo-a --claim", False),)),
    ("claim_then_close", (
        ("c1", "bd update demo-a --claim", False),
        ("c2", "bd close demo-a", False),
    )),
    ("failed_is_error", (("c1", "bd update demo-a --claim", True),)),
    ("compound_wrapped", (("c1", "cd /srv/demo && bd update demo-a --claim", False),)),
    ("echo_not_counted", (("c1", 'echo "bd update demo-a --claim"', False),)),
    ("variable_id_skipped", (("c1", "bd update $BID --claim", False),)),
)


@pytest.mark.parametrize("name,pairs", _PARITY_CURSOR_SCENARIOS, ids=[s[0] for s in _PARITY_CURSOR_SCENARIOS])
def test_bead_extraction_parity_cursor(tmp_path, name, pairs):
    p = _rp_cursor_db(tmp_path, f"{name}.db", *pairs)
    local_claims = local_bs.resolve_session_bead("cursor", [p], {})
    remote_ids = rp.resolve_remote_session_bead("cursor", [p])
    assert [bid for bid, _ts in local_claims] == remote_ids


def test_collect_agents_herdr_only_sets_bead_tracked_by_kind(monkeypatch):
    herdr_json = json.dumps({"result": {"agents": [
        {"agent": "claude", "agent_session": {"value": "s1"}, "cwd": "/srv/demo"},
        {"agent": "opencode", "agent_session": {"value": "s2"}, "cwd": "/srv/demo"},
    ]}})

    class FakeProc:
        returncode = 0
        stdout = herdr_json.encode()
        stderr = b""

    monkeypatch.setattr(rp.subprocess, "run", lambda *a, **k: FakeProc())
    monkeypatch.setattr(rp, "find_herdr", lambda b: "herdr")
    agents = rp.collect_agents("herdr", [])
    by_kind = {a["kind"]: a for a in agents}
    assert by_kind["claude"]["bead_tracked"] is True
    assert by_kind["claude"]["bead"] is None  # no transcript path from herdr alone
    assert by_kind["opencode"]["bead_tracked"] is False


# -- by-session-id transcript backfill for herdr-only agents (the same
# "pane went quiet while holding a bead" gap agents.py fixes locally --
# see backfill_herdr_only_bead_claims' docstring) ---------------------------


def test_claude_projects_dir_from_glob_strips_fixed_suffix():
    assert rp._claude_projects_dir_from_glob("/x/y/projects/*/*.jsonl") == "/x/y/projects"


def test_find_claude_transcript_by_session_id_ignores_pane_cwd(tmp_path):
    # The transcript lives under a project dir name that does NOT match any
    # cwd -- proves the lookup goes by filename only, never a decoded/guessed
    # project dir.
    project_dir = tmp_path / "-mismatched-project-dir"
    project_dir.mkdir()
    (project_dir / "remote-session-1.jsonl").write_text("{}\n")
    found = rp.find_claude_transcript_by_session_id(str(tmp_path), "remote-session-1")
    assert found == [str(project_dir / "remote-session-1.jsonl")]


def test_find_claude_transcript_by_session_id_no_match_returns_empty(tmp_path):
    assert rp.find_claude_transcript_by_session_id(str(tmp_path), "no-such-session") == []


def test_find_kimi_wire_paths_by_session_id(tmp_path):
    session_id = "session_remote_kimi1"
    session_dir = tmp_path / "sessions" / "wd_x" / session_id
    agent_home = session_dir / "agents" / "main"
    agent_home.mkdir(parents=True)
    (tmp_path / "session_index.jsonl").write_text(json.dumps({
        "sessionId": session_id, "sessionDir": str(session_dir), "workDir": "/srv/demo",
    }) + "\n")
    (session_dir / "state.json").write_text(json.dumps({
        "id": session_id, "agents": {"main": {"homedir": str(agent_home)}},
    }))
    found = rp.find_kimi_wire_paths_by_session_id(str(tmp_path), session_id)
    assert found == [str(agent_home / "wire.jsonl")]


def test_find_kimi_wire_paths_by_session_id_no_match_returns_empty(tmp_path):
    (tmp_path / "session_index.jsonl").write_text("")
    assert rp.find_kimi_wire_paths_by_session_id(str(tmp_path), "no-such-session") == []


def test_backfill_herdr_only_bead_claims_resolves_claude_transcript(tmp_path):
    project_dir = tmp_path / "-mismatched"
    project_dir.mkdir()
    p = project_dir / "stale-remote-session.jsonl"
    p.write_text(_rp_claude_lines(("t1", "bd update demo-a --claim", _CLAIM_OK, False)))

    agents = [{
        "source": "herdr", "kind": "claude", "session_id": "stale-remote-session",
        "bead": None, "bead_tracked": True,
    }]
    rp.backfill_herdr_only_bead_claims(agents, str(tmp_path), str(tmp_path / "no-kimi"))
    assert agents[0]["bead_claims"] == ["demo-a"]


def test_backfill_herdr_only_bead_claims_resolves_kimi_transcript(tmp_path):
    session_id = "session_remote_stale_kimi"
    session_dir = tmp_path / "kimi" / "sessions" / "wd_x" / session_id
    agent_home = session_dir / "agents" / "main"
    agent_home.mkdir(parents=True)
    (agent_home / "wire.jsonl").write_text(_rp_kimi_lines(("k1", "bd update demo-a --claim", _CLAIM_OK)))
    kimi_dir = tmp_path / "kimi"
    (kimi_dir / "session_index.jsonl").write_text(json.dumps({
        "sessionId": session_id, "sessionDir": str(session_dir), "workDir": "/srv/demo",
    }) + "\n")
    (session_dir / "state.json").write_text(json.dumps({
        "id": session_id, "agents": {"main": {"homedir": str(agent_home)}},
    }))

    agents = [{
        "source": "herdr", "kind": "kimi", "session_id": session_id,
        "bead": None, "bead_tracked": True,
    }]
    rp.backfill_herdr_only_bead_claims(agents, str(tmp_path / "no-claude"), str(kimi_dir))
    assert agents[0]["bead_claims"] == ["demo-a"]


def test_backfill_herdr_only_bead_claims_skips_already_merged_entries(tmp_path):
    # An entry that already came from a session/kimi record (source="both")
    # already carries "bead_claims" from the merge -- must not be touched or
    # re-scanned.
    agents = [{
        "source": "both", "kind": "claude", "session_id": "s1",
        "bead_claims": ["demo-existing"], "bead_tracked": True,
    }]
    rp.backfill_herdr_only_bead_claims(agents, str(tmp_path), str(tmp_path))
    assert agents[0]["bead_claims"] == ["demo-existing"]


def test_backfill_herdr_only_bead_claims_untracked_kind_untouched(tmp_path):
    agents = [{
        "source": "herdr", "kind": "codex", "session_id": "s1", "bead": None, "bead_tracked": False,
    }]
    rp.backfill_herdr_only_bead_claims(agents, str(tmp_path), str(tmp_path))
    assert "bead_claims" not in agents[0]


def test_backfill_herdr_only_bead_claims_no_transcript_leaves_no_claims(tmp_path):
    agents = [{
        "source": "herdr", "kind": "claude", "session_id": "ghost", "bead": None, "bead_tracked": True,
    }]
    rp.backfill_herdr_only_bead_claims(agents, str(tmp_path), str(tmp_path / "no-kimi"))
    assert "bead_claims" not in agents[0]
    assert agents[0]["bead"] is None


def test_build_result_backfills_bead_for_herdr_only_stale_claude_pane(tmp_path, monkeypatch):
    # Full build_result() wiring: herdr reports a pane whose session has NO
    # scannable session record (scan_session_agents excludes it -- either
    # its transcript predates session_window_s here we just never let it
    # match by not writing under the projects_glob pattern at all, forcing
    # merge_remote_agent_sources to leave it herdr-only), but the transcript
    # DOES exist elsewhere for the by-id lookup to find, under a project dir
    # name that does not match the pane's cwd.
    projects_dir = tmp_path / "claude-projects"
    mismatched = projects_dir / "-mismatched-dir"
    mismatched.mkdir(parents=True)
    (mismatched / "stale-remote-session.jsonl").write_text(
        _rp_claude_lines(("t1", "bd update demo-a --claim", _CLAIM_OK, False))
    )

    def fake_collect_agents(herdr_bin, worktrees):
        return [{
            "id": "stale-remote-session", "kind": "claude", "status": "done", "cwd": "/srv/demo",
            "repo": None, "branch": None, "pane": "p1", "workspace": "w1", "title": "demo",
            "label": "demo", "focused": False, "session_id": "stale-remote-session", "bead": None,
            "bead_tracked": True, "last_activity": None, "status_since": None,
            "tokens_today": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0},
            "cost_today_usd": 0.0, "msg_count_today": 0, "subagents_active": 0, "model": None,
            "source": "herdr",
        }]

    monkeypatch.setattr(rp, "collect_agents", fake_collect_agents)

    result = rp.build_result(
        "testhost", str(projects_dir / "*" / "*.jsonl"), ["/nonexistent"], ["/"],
        "no-such-herdr", 4, 5.0, 4, str(tmp_path / "state.json"),
        kimi_dir=str(tmp_path / "no-kimi"),
    )
    matching = [a for a in result["agents"] if a["session_id"] == "stale-remote-session"]
    assert len(matching) == 1
    assert matching[0]["bead_claims"] == ["demo-a"]
    json.dumps(result)  # still JSON-serializable
