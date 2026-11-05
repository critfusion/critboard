"""Pin the full output of remote_probe.aggregate_file and
collect_usage_and_analytics on one mixed fixture, so a refactor of either
function cannot change what the probe ships.

The golden file tests/fixtures/remote_probe_pin.json was written from the
code before the split into helpers. The scenario covers: duplicate message
ids within and across files, sidechain sessions, the flat cache-write
fallback, tool errors with and without a matching tool_use, error example
ties, trouble files, API errors, malformed and blank lines, a subagent file
(tool data dropped), a legacy cache entry (old "buckets" key, no "msgs"),
a cached second call and a changed file on a third call.

Regenerate only on purpose: PIN_REGENERATE=1 pytest tests/test_remote_probe_pin.py
"""

import json
import os
from pathlib import Path

from critdash import remote_probe as rp

GOLDEN = Path(__file__).parent / "fixtures" / "remote_probe_pin.json"
SEP = rp._SEP


def _usage(mid, model, ts, session="sA", sidechain=False, uuid=None, flat_cw=None, **tok):
    usage = {
        "input_tokens": tok.get("i", 3),
        "output_tokens": tok.get("o", 7),
        "cache_read_input_tokens": tok.get("cr", 11),
    }
    if flat_cw is None:
        usage["cache_creation"] = {
            "ephemeral_5m_input_tokens": tok.get("c5", 13),
            "ephemeral_1h_input_tokens": tok.get("c1", 17),
        }
    else:
        usage["cache_creation_input_tokens"] = flat_cw
    doc = {
        "type": "assistant", "timestamp": ts, "isSidechain": sidechain,
        "message": {"id": mid, "model": model, "usage": usage,
                    "content": [{"type": "text", "text": "hi"}]},
    }
    if session:
        doc["sessionId"] = session
    if uuid:
        doc["uuid"] = uuid
    return json.dumps(doc)


def _tool_use(uuid, ts, tid, name, tool_input=None):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "timestamp": ts, "sessionId": "sA",
        "message": {"content": [
            "not a dict",
            {"type": "text", "text": "x"},
            {"type": "tool_use", "id": tid, "name": name, "input": tool_input or {}},
        ]},
    })


def _tool_result(uuid, ts, tid, is_error, content):
    block = {"type": "tool_result", "is_error": is_error, "content": content}
    if tid is not None:
        block["tool_use_id"] = tid
    return json.dumps({
        "type": "user", "uuid": uuid, "timestamp": ts, "sessionId": "sA",
        "message": {"content": [block]},
    })


def _api_error(uuid, ts, error="rate_limit", status=429):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "timestamp": ts, "isApiErrorMessage": True,
        "error": error, "apiErrorStatus": status,
        "message": {"content": [{"type": "text", "text": "limit hit"}]},
    })


FILE_A = [
    _usage("m1", "claude-opus-5", "2026-09-18T10:05:00Z", uuid="a1"),
    _usage("m1", "claude-opus-5", "2026-09-18T10:06:00Z", uuid="a2"),  # dup in file
    _usage("m2", "claude-opus-5", "2026-09-18T10:30:00Z", sidechain=True, flat_cw=19, uuid="a3"),
    _usage("m5", "claude-sonnet-5", "2026-09-18T11:00:00Z", session=None, uuid="a4"),
    "{not json",
    "",
    "   ",
    _tool_use("a5", "2026-09-18T10:07:00Z", "t1", "Edit", {"file_path": "a.py"}),
    _tool_use("a6", "2026-09-18T10:08:00Z", "t2", "Bash", {"command": "ls"}),
    _tool_use("a7", "2026-09-18T10:08:30Z", "t9", None),
    _tool_result("a8", "2026-09-18T10:09:00Z", "t1", True, "String to replace not found in file."),
    _tool_result("a9", "2026-09-18T10:10:00Z", "t2", True,
                 [{"type": "text", "text": "Permission for this action was denied by the user."}]),
    _tool_result("a10", "2026-09-18T10:11:00Z", "nope", True, "Something else broke badly"),
    _tool_result("a11", "2026-09-18T10:12:00Z", None, True, "String to replace not found again."),
    _tool_result("a11b", "2026-09-18T10:12:00Z", None, True, "String to replace not found, same second."),
    _tool_result("a12", "2026-09-18T10:13:00Z", "t9", False, "fine"),
    _tool_result("a13", "2026-09-19T09:00:00Z", None, True, ""),
    _api_error("a14", "2026-09-18T12:00:00Z"),
    _api_error("a15", "2026-09-18T12:30:00Z"),
    _api_error("a16", "2026-09-18T12:15:00Z", error="unknown", status=500),
    json.dumps({"type": "user", "timestamp": "2026-09-18T10:00:00Z"}),  # no uuid
    json.dumps({"type": "user", "uuid": "a17", "timestamp": "2026-09-18T10:00:00Z",
                "message": {"content": "plain string"}}),
]

FILE_B = [
    _usage("m1", "claude-opus-5", "2026-09-18T10:05:00Z", uuid="b1"),  # dup across files
    _usage("m3", "claude-haiku-5", "2026-09-19T08:00:00Z", session="sB", uuid="b2", i=1, o=2),
    _tool_use("b3", "2026-09-19T08:01:00Z", "t3", "Read", {"file_path": "/x/b.py"}),
    _tool_result("b4", "2026-09-19T08:02:00Z", "t3", True, "File does not exist."),
    _tool_result("b5", "2026-09-18T10:12:00Z", None, True, "String to replace not found tie."),
    _api_error("b6", "2026-09-18T11:00:00Z"),
]

FILE_B_APPEND = [
    _usage("m6", "claude-haiku-5", "2026-09-19T09:00:00Z", session="sB", uuid="b7"),
    _tool_use("b8", "2026-09-19T09:01:00Z", "t4", "Write", {"file_path": "c.py"}),
    _tool_result("b9", "2026-09-19T09:02:00Z", "t4", True, "File has not been read yet."),
]

SUBAGENT = [
    _usage("m4", "claude-sonnet-5", "2026-09-18T10:40:00Z", sidechain=True, uuid="s1"),
    _usage("m2", "claude-opus-5", "2026-09-18T10:30:00Z", sidechain=True, uuid="s2"),  # dup of A
    _tool_use("s3", "2026-09-18T10:41:00Z", "t5", "Edit", {"file_path": "sub.py"}),
    _tool_result("s4", "2026-09-18T10:42:00Z", "t5", True, "String to replace not found."),
    _api_error("s5", "2026-09-18T10:43:00Z"),
]

LEGACY_ENTRY = {
    "project": "legacy",
    "buckets": {
        f"2026-09-17T09{SEP}claude-opus-5": {
            "input": 1, "output": 2, "cache_read": 3, "cache_write_5m": 4, "cache_write_1h": 5, "messages": 1,
        },
        f"2026-09-18T10{SEP}claude-opus-5": {
            "input": 10, "output": 20, "cache_read": 30,
            "cache_write_5m": 40, "cache_write_1h": 50, "messages": 2,
        },
    },
    "tools": {f"2026-09-18{SEP}Edit": {"calls": 4, "errors": 2}},
    "errors": {
        f"2026-09-18{SEP}string_not_found{SEP}Edit": {"count": 2, "last_seen": "2026-09-18T23:00:00Z"},
    },
    "error_examples": {
        "string_not_found": {"example": "legacy example", "last_seen": "2026-09-18T10:12:00Z"},
    },
    "trouble_files": {f"2026-09-18{SEP}a.py{SEP}Edit": {"errors": 2}},
    "api_errors": {f"2026-09-18{SEP}rate_limit": {"count": 3, "last_seen": "2026-09-18T01:00:00Z"}},
    "sessions": {
        f"2026-09-18{SEP}sA{SEP}0{SEP}claude-opus-5": {
            "project": "legacy", "input": 5, "output": 5, "cache_read": 5,
            "cache_write_5m": 5, "cache_write_1h": 5, "messages": 1,
        },
    },
}


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def _normalize_state(state: dict, root: Path) -> dict:
    files = {}
    for path, entry in sorted(state.get("files", {}).items()):
        rel = os.path.relpath(path, root)
        files[rel] = {k: v for k, v in entry.items() if k not in ("inode", "size", "mtime")}
    return {**state, "files": files}


def _scenario(tmp_path: Path) -> dict:
    projects = tmp_path / "projects"
    file_a = projects / "-home-user-repos-demo" / "s1.jsonl"
    file_b = projects / "-home-user-repos-other" / "s2.jsonl"
    file_c = projects / "-home-user-repos-legacy" / "s3.jsonl"
    sub = projects / "-home-user-repos-demo" / "sA" / "subagents" / "agent-1.jsonl"
    _write(file_a, FILE_A)
    _write(file_b, FILE_B)
    _write(file_c, ["ignored: the cache entry is reused"])
    _write(sub, SUBAGENT)

    st = os.stat(file_c)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "kimi_files": {"keep": "me"},
        "files": {
            str(file_c): {"inode": st.st_ino, "size": st.st_size, "mtime": st.st_mtime, **LEGACY_ENTRY},
        },
    }))
    pattern = str(projects / "*" / "*.jsonl")

    out = {
        "aggregate_a": rp.aggregate_file(str(file_a), "demo"),
        "aggregate_sub": rp.aggregate_file(str(sub), "demo"),
        "aggregate_missing": rp.aggregate_file(str(tmp_path / "missing.jsonl"), "x"),
    }
    out["first"] = rp.collect_usage_and_analytics(pattern, state_file=str(state_file))
    out["first_state"] = _normalize_state(json.loads(state_file.read_text()), tmp_path)
    out["second"] = rp.collect_usage_and_analytics(pattern, state_file=str(state_file))
    out["second_state"] = _normalize_state(json.loads(state_file.read_text()), tmp_path)
    _write(file_b, FILE_B + FILE_B_APPEND)
    out["third"] = rp.collect_usage_and_analytics(pattern, state_file=str(state_file))
    out["third_state"] = _normalize_state(json.loads(state_file.read_text()), tmp_path)
    # Round-trip through JSON: the golden file stores lists, not tuples.
    return json.loads(json.dumps(out))


def test_remote_probe_usage_output_matches_golden(tmp_path):
    got = _scenario(tmp_path)
    if os.environ.get("PIN_REGENERATE") == "1":
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=False) + "\n")
    want = json.loads(GOLDEN.read_text())
    for key in want:
        assert got[key] == want[key], key
        # dict and list order is part of the shipped payload
        assert json.dumps(got[key]) == json.dumps(want[key]), f"{key}: order changed"
    assert set(got) == set(want)
