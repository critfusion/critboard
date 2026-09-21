import json

import pytest

from critdash import remote_probe as rp
from critdash.collectors import analytics as local_an


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
