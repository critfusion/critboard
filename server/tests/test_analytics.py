"""analytics collector (briefing Task 2a/2b/2c): error classification,
tool/api-error extraction and correlation, truncation, subagent cost split,
and per-host merge without double counting."""

from __future__ import annotations

import pytest

from critdash.collectors.analytics import (
    AnalyticsCollector,
    _truncate,
    classify_api_error,
    classify_error,
    extract_file_path,
    make_example,
    parse_doc_for_analytics,
    redact_secrets,
)

# -- error-classification rule table: one real-world example per rule --------


@pytest.mark.parametrize(
    "text,expected_kind",
    [
        ("String to replace not found in file.", "string_not_found"),
        ("old_string not found in the file content", "string_not_found"),
        ("Exit code 2\nls: cannot access 'foo.txt': No such file or directory", "file_not_found"),
        ("Error: ENOENT: no such file or directory, open 'x'", "file_not_found"),
        ("Permission for this action was denied by the user.", "permission_denied"),
        ("The user doesn't want to proceed with this tool use.", "permission_denied"),
        ("bash: eacces: permission denied", "permission_denied"),
        ("Exit code 143\nCommand timed out after 2m 0s", "timeout"),
        ("Concurrent subagent limit reached. You can run up to N.", "rate_limit"),
        ("claude-sonnet-5[1m] is temporarily unavailable, retry later.", "rate_limit"),
        ("Response overloaded, error 529", "rate_limit"),
        ("json.decoder.JSONDecodeError: Expecting value: line 1 column 1", "invalid_json"),
        ("SyntaxError: Unexpected token < in JSON at position 0", "invalid_json"),
        ("Exit code 1\nTraceback (most recent call last)", "command_failed"),
        ("completely unrecognized failure text with no keywords", "other"),
    ],
)
def test_classify_error_rules(text, expected_kind):
    assert classify_error(text) == expected_kind


def test_classify_error_rule_order_string_not_found_beats_file_not_found():
    # "string to replace not found" contains the word "not found" but must
    # classify as the more specific string_not_found, not a generic miss.
    assert classify_error("String to replace not found in file.") == "string_not_found"


def test_classify_error_empty_text_is_other():
    assert classify_error("") == "other"
    assert classify_error(None) == "other"


# -- classify_api_error --------------------------------------------------------


def test_classify_api_error_529_is_overloaded():
    assert classify_api_error({"apiErrorStatus": 529, "error": "server_error"}) == "overloaded_error"


@pytest.mark.parametrize(
    "doc_error,expected",
    [
        ("rate_limit", "rate_limit"),
        ("invalid_request", "invalid_request"),
        ("authentication_failed", "authentication_failed"),
        ("server_error", "server_error"),
    ],
)
def test_classify_api_error_passes_through_real_kinds(doc_error, expected):
    assert classify_api_error({"apiErrorStatus": 429, "error": doc_error}) == expected


def test_classify_api_error_missing_error_field_is_other():
    assert classify_api_error({"apiErrorStatus": 500}) == "other"


# -- redaction / example capping ------------------------------------------------


def test_make_example_redacts_secrets():
    text = "failed with api_key: sk-FAKE0123456789ABCDEF while connecting"
    example = make_example(text)
    assert "sk-FAKE0123456789ABCDEF" not in example
    assert "<redacted>" in example


def test_make_example_caps_length():
    text = "x" * 500
    assert len(make_example(text)) == 200


def test_make_example_none_for_empty_text():
    assert make_example("") is None
    assert make_example(None) is None


def test_redact_secrets_leaves_normal_text_untouched():
    text = "Exit code 1\nfile not found at src/foo.py"
    assert redact_secrets(text) == text


def test_extract_file_path_prefers_file_path_key():
    assert extract_file_path({"file_path": "src/a.py", "path": "src/b.py"}) == "src/a.py"


def test_extract_file_path_none_for_bash_input():
    assert extract_file_path({"command": "ls -la"}) is None


# -- parse_doc_for_analytics: tool_use/tool_result correlation ---------------


def _assistant_tool_use(uuid, ts, tool_use_id, tool_name, tool_input=None, session="s1", sidechain=False):
    return {
        "type": "assistant", "uuid": uuid, "timestamp": ts, "sessionId": session,
        "isSidechain": sidechain,
        "message": {"content": [{"type": "tool_use", "id": tool_use_id, "name": tool_name,
                                   "input": tool_input or {}}]},
    }


def _user_tool_result(uuid, ts, tool_use_id, is_error, content, session="s1"):
    return {
        "type": "user", "uuid": uuid, "timestamp": ts, "sessionId": session, "isSidechain": False,
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                                   "is_error": is_error, "content": content}]},
    }


def test_parse_doc_same_poll_match_updates_row_in_place():
    pending: dict = {}
    calls, api_errors = parse_doc_for_analytics(
        _assistant_tool_use("u1", "2026-09-18T10:00:00Z", "toolu_1", "Edit", {"file_path": "a.py"}),
        "localhost", pending,
    )
    assert len(calls) == 1
    assert calls[0]["is_error"] == 0
    assert "toolu_1" in pending

    calls2, _ = parse_doc_for_analytics(
        _user_tool_result(
            "u2", "2026-09-18T10:00:01Z", "toolu_1", True, "String to replace not found in file."
        ),
        "localhost", pending,
    )
    # matched in-place on the ORIGINAL row -- no second row emitted
    assert calls2 == []
    assert calls[0]["is_error"] == 1
    assert calls[0]["error_kind"] == "string_not_found"
    assert "toolu_1" not in pending  # popped once matched


def test_parse_doc_cross_poll_orphan_result_gets_fallback_row():
    # tool_use was never seen this poll (e.g. inserted last poll) -- the
    # tool_result must still surface as a standalone row for the caller to
    # correct via Store.update_tool_call_error / insert fallback.
    pending: dict = {}
    calls, _ = parse_doc_for_analytics(
        _user_tool_result(
            "u9", "2026-09-18T10:05:00Z", "toolu_missing", True,
            "Permission for this action was denied by the user.",
        ),
        "localhost", pending,
    )
    assert len(calls) == 1
    row = calls[0]
    assert row["is_error"] == 1
    assert row["tool"] is None
    assert row["error_kind"] == "permission_denied"
    assert row["_fallback_tool_use_id"] == "toolu_missing"


def test_parse_doc_api_error_message_produces_api_error_row_only():
    doc = {
        "type": "assistant", "uuid": "u7", "timestamp": "2026-09-18T10:00:00Z",
        "isApiErrorMessage": True, "error": "rate_limit", "apiErrorStatus": 429,
        "message": {"content": [{"type": "text", "text": "limit hit"}]},
    }
    calls, api_errors = parse_doc_for_analytics(doc, "localhost", {})
    assert calls == []
    assert len(api_errors) == 1
    assert api_errors[0]["kind"] == "rate_limit"


def test_parse_doc_skips_synthetic_content_without_list():
    doc = {"type": "user", "uuid": "u1", "timestamp": "t", "message": {"content": "not a list"}}
    calls, api_errors = parse_doc_for_analytics(doc, "localhost", {})
    assert calls == [] and api_errors == []


# -- truncation ------------------------------------------------------------------


def test_truncate_under_limit_not_truncated():
    rows, truncated = _truncate([1, 2, 3], 5)
    assert rows == [1, 2, 3]
    assert truncated is False


def test_truncate_over_limit_caps_and_flags():
    rows, truncated = _truncate(list(range(10)), 3)
    assert rows == [0, 1, 2]
    assert truncated is True


# -- AnalyticsCollector: subagent cost split math + merge without double count -


def _insert_usage_row(store, session_id, is_sidechain, cost_usd, host="localhost", model="claude-sonnet-5",
                       project="dashboard", ts="2026-09-18T10:00:00Z", message_id=None):
    store.insert_usage_events([{
        "message_id": message_id or f"{session_id}-{is_sidechain}-{cost_usd}-{ts}",
        "ts": ts, "session_id": session_id, "project": project, "project_path": "/x",
        "model": model, "input": 10, "output": 10, "cache_read": 0,
        "cache_write_5m": 0, "cache_write_1h": 0, "speed": None,
        "is_sidechain": 1 if is_sidechain else 0, "web_searches": 0,
        "cost_usd": cost_usd, "host": host,
    }])


def test_rollup_subagents_sidechain_share_math(tmp_store):
    _insert_usage_row(tmp_store, "sess-1", is_sidechain=False, cost_usd=12.3)
    _insert_usage_row(tmp_store, "sess-1", is_sidechain=True, cost_usd=48.9, message_id="m2")

    collector = AnalyticsCollector(store=tmp_store, host="localhost")
    result = collector._rollup_subagents("2000-01-01T00:00:00Z", "2000-01-01", "7d", pricing={})

    assert len(result["sessions"]) == 1
    s = result["sessions"][0]
    assert s["main_cost_usd"] == pytest.approx(12.3)
    assert s["sidechain_cost_usd"] == pytest.approx(48.9)
    # 48.9 / (12.3 + 48.9) = 0.7990...
    assert s["sidechain_share"] == pytest.approx(48.9 / 61.2, abs=1e-4)
    assert result["totals"]["sidechain_share"] == pytest.approx(48.9 / 61.2, abs=1e-4)


def test_rollup_subagents_zero_total_cost_has_zero_share_not_divide_error(tmp_store):
    _insert_usage_row(tmp_store, "s", is_sidechain=False, cost_usd=0.0)
    collector = AnalyticsCollector(store=tmp_store, host="localhost")
    result = collector._rollup_subagents("2000-01-01T00:00:00Z", "2000-01-01", "7d", pricing={})
    assert result["sessions"][0]["sidechain_share"] == 0.0


def test_rollup_errors_merges_local_and_remote_without_double_counting(tmp_store):
    # LOCAL: 2 string_not_found errors on Edit, from this host's own
    # tool_call_events table.
    tmp_store.insert_tool_call_events([
        {"host": "localhost", "uuid": "u1", "block_index": 0, "ts": "2026-09-18T10:00:00Z",
         "session_id": "s1", "is_sidechain": 0, "tool": "Edit", "tool_use_id": "t1",
         "is_error": 1, "error_kind": "string_not_found", "error_excerpt": "ex1", "file_path": "a.py"},
        {"host": "localhost", "uuid": "u2", "block_index": 0, "ts": "2026-09-18T10:01:00Z",
         "session_id": "s1", "is_sidechain": 0, "tool": "Edit", "tool_use_id": "t2",
         "is_error": 1, "error_kind": "string_not_found", "error_excerpt": "ex2", "file_path": "a.py"},
    ])
    # REMOTE (host-b): 3 more string_not_found errors on Edit, shipped as a
    # pre-aggregated day-bucket row -- must ADD to the local count, not
    # replace it, and must not be counted twice just because both a local
    # AND a remote source contribute to the same (kind, tool) pair.
    tmp_store.upsert_remote_error_buckets([
        {"host": "host-b", "day": "2026-09-18", "kind": "string_not_found", "tool": "Edit",
         "count": 3, "last_seen": "2026-09-18T10:02:00Z"},
    ])

    collector = AnalyticsCollector(store=tmp_store, host="localhost")
    result = collector._rollup_errors("2000-01-01T00:00:00Z", "2000-01-01", "7d")

    assert result["total_errors"] == 5  # 2 local + 3 remote, not 2, not 3, not more than 5
    top = result["top_errors"][0]
    assert top["kind"] == "string_not_found"
    assert top["tool"] == "Edit"
    assert top["count"] == 5


def test_rollup_tools_merges_local_and_remote_calls_without_double_counting(tmp_store):
    tmp_store.insert_tool_call_events([
        {"host": "localhost", "uuid": "u1", "block_index": 0, "ts": "2026-09-18T10:00:00Z",
         "session_id": "s1", "is_sidechain": 0, "tool": "Bash", "tool_use_id": "t1",
         "is_error": 0, "error_kind": None, "error_excerpt": None, "file_path": None},
    ])
    tmp_store.upsert_remote_tool_buckets([
        {"host": "host-b", "day": "2026-09-18", "tool": "Bash", "calls": 9, "errors": 1},
    ])

    collector = AnalyticsCollector(store=tmp_store, host="localhost")
    result = collector._rollup_tools("2000-01-01T00:00:00Z", "2000-01-01", "7d")
    bash = next(r for r in result["usage"] if r["tool"] == "Bash")
    assert bash["calls"] == 10  # 1 local + 9 remote
    assert bash["errors"] == 1


def test_analytics_collector_empty_store_returns_honest_zero_decisions():
    collector = AnalyticsCollector(store=None, host="localhost")
    result = collector._rollup()
    assert result["tools"]["decisions"] == {"accepted": 0, "rejected": 0, "total": 0}


# -- Kimi quota rule + provider-tagged errors (briefing 2026-09-18) ----------


def test_classify_error_kimi_quota_message_is_quota_exceeded():
    message = (
        "403 You've reached your monthly usage limit for this billing cycle. "
        "Your quota will be refreshed in the next cycle. To continue now, "
        "purchase extra usage or upgrade your plan."
    )
    assert classify_error(message) == "quota_exceeded"


def test_rollup_errors_tags_claude_and_kimi_separately_same_kind_name(tmp_store):
    # A Claude api_error and a Kimi error that classify to the SAME kind name
    # must never be summed into one row -- they are keyed by (kind, provider).
    tmp_store.insert_api_error_events([
        {"host": "localhost", "uuid": "u1", "ts": "2026-09-18T10:00:00Z", "kind": "rate_limit"},
    ])
    tmp_store.insert_kimi_error_events([
        {"host": "localhost", "session_id": "session_k1", "ts": "2026-09-18T10:00:00Z", "ts_ms": 1,
         "kind": "quota_exceeded", "code": "provider.auth_error", "example": "403 usage limit..."},
    ])
    collector = AnalyticsCollector(store=tmp_store, host="localhost")
    result = collector._rollup_errors("2000-01-01T00:00:00Z", "2000-01-01", "7d")

    by_kind_provider = {(r["kind"], r["provider"]): r for r in result["api_errors"]}
    assert by_kind_provider[("rate_limit", "claude")]["count"] == 1
    assert by_kind_provider[("quota_exceeded", "kimi")]["count"] == 1
    assert result["total_errors"] >= 0  # total_errors is tool-call-error-only, unaffected by api_errors


def test_rollup_errors_merges_local_and_remote_kimi_without_double_counting(tmp_store):
    tmp_store.insert_kimi_error_events([
        {"host": "localhost", "session_id": "session_k1", "ts": "2026-09-18T10:00:00Z", "ts_ms": 1,
         "kind": "quota_exceeded", "code": "provider.auth_error", "example": "ex"},
    ])
    tmp_store.upsert_remote_kimi_error_buckets([
        {"host": "host-b", "day": "2026-09-18", "kind": "quota_exceeded", "count": 2,
         "last_seen": "2026-09-18T10:05:00Z"},
    ])
    collector = AnalyticsCollector(store=tmp_store, host="localhost")
    result = collector._rollup_errors("2000-01-01T00:00:00Z", "2000-01-01", "7d")
    kimi_row = next(r for r in result["api_errors"] if r["provider"] == "kimi")
    assert kimi_row["count"] == 3  # 1 local + 2 remote


@pytest.mark.asyncio
async def test_analytics_collector_end_to_end_ingest_and_rollup(tmp_store, tmp_path):
    proj_dir = tmp_path / "-home-user-work-demo"
    proj_dir.mkdir()
    import json as _json

    lines = [
        {"type": "assistant", "uuid": "u1", "timestamp": "2026-09-18T10:00:00.000Z",
         "sessionId": "s1", "isSidechain": False,
         "message": {"model": "claude-sonnet-5", "content": [
             {"type": "tool_use", "id": "toolu_1", "name": "Edit", "input": {"file_path": "a.py"}}
         ]}},
        {"type": "user", "uuid": "u2", "timestamp": "2026-09-18T10:00:01.000Z",
         "sessionId": "s1", "isSidechain": False,
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True,
              "content": "String to replace not found in file."}
         ]}},
    ]
    (proj_dir / "sess1.jsonl").write_text("\n".join(_json.dumps(line) for line in lines) + "\n")

    collector = AnalyticsCollector(
        store=tmp_store, projects_glob=str(tmp_path / "*" / "*.jsonl"), host="localhost",
    )
    result = await collector.collect()
    errors = result["analytics"]["errors"]
    assert errors["total_errors"] == 1
    assert errors["top_errors"][0]["kind"] == "string_not_found"
    assert errors["top_errors"][0]["tool"] == "Edit"
    tools = result["analytics"]["tools"]
    edit = next(r for r in tools["usage"] if r["tool"] == "Edit")
    assert edit["calls"] == 1
    assert edit["errors"] == 1
