"""POST /api/bead/{id}/reply + GET /api/bead/{id}/comments (bead popup
reply/send-back/close, off by default -- see server/critdash/bead_reply.py).

Covers: config resolution/route matching, the exec-based (never-shell) bd
call primitives, every guard (disabled, allow_config_writes, bad id, not
open/no human label, empty text on send_back), the exact bd command
sequence for send_back and close, and the required injection proof (a
malicious reply body and a bead id with a shell metacharacter must never
reach a shell -- see write_fake_bd_reply/FAKE_BD_PY below, which records
real argv from a REAL asyncio.create_subprocess_exec call).
"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from critdash import bead_reply as bead_reply_mod
from critdash import config as config_mod
from critdash import detect as detect_mod
from critdash import main as main_mod
from critdash.collectors.beads import validate_label

# ---------------- fake bd (real subprocess, records argv verbatim) --------
# A tiny Python script standing in for `bd`: it appends its own argv (as a
# JSON array, one line per invocation) to $FAKE_BD_RECORD_FILE, then returns
# a canned response per subcommand from env vars. Run through the SAME
# bd_exec_argv/run_bd_exec path production code uses (real
# create_subprocess_exec, no shell) -- this is what proves the write path
# never builds a shell string out of user input, not a mock of that claim.
FAKE_BD_PY = """#!/usr/bin/env python3
import json, os, sys

record_file = os.environ.get("FAKE_BD_RECORD_FILE")
if record_file:
    with open(record_file, "a") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\\n")

args = sys.argv[1:]

if args[:1] == ["show"]:
    print(os.environ.get("FAKE_BD_SHOW_JSON", "[]"))
    sys.exit(int(os.environ.get("FAKE_BD_SHOW_EXIT", "0")))
if len(args) >= 2 and args[0] == "comments" and args[1] == "add":
    print(json.dumps({"ok": True}))
    sys.exit(int(os.environ.get("FAKE_BD_COMMENT_EXIT", "0")))
if args[:1] == ["comments"]:
    print(os.environ.get("FAKE_BD_COMMENTS_JSON", "[]"))
    sys.exit(0)
if args[:1] == ["update"]:
    print(json.dumps({"ok": True}))
    sys.exit(int(os.environ.get("FAKE_BD_UPDATE_EXIT", "0")))
if args[:1] == ["close"]:
    print(json.dumps({"ok": True}))
    sys.exit(int(os.environ.get("FAKE_BD_CLOSE_EXIT", "0")))

sys.stderr.write("fake bd: unexpected args: " + " ".join(args) + "\\n")
sys.exit(1)
"""


def write_fake_bd_reply(path):
    path.write_text(FAKE_BD_PY)
    path.chmod(0o755)
    return path


def read_recorded_argv(record_file):
    if not record_file.exists():
        return []
    lines = [ln for ln in record_file.read_text().splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def show_payload(bead_id, created_by, labels, status="open"):
    return json.dumps(
        [{"id": bead_id, "status": status, "labels": labels, "created_by": created_by, "title": "t"}]
    )


# ---------------- resolve_config / choose_route / resolve_actor -----------


def test_resolve_config_defaults_when_bead_reply_key_absent():
    cfg = bead_reply_mod.resolve_config({})
    assert cfg["enabled"] is False
    assert cfg["default_route"] == "needs-claude"
    assert ("claude", "needs-claude") in cfg["routes"]


def test_resolve_config_merges_and_drops_malformed_routes():
    cfg = bead_reply_mod.resolve_config(
        {
            "bead_reply": {
                "enabled": True,
                "routes": [["grok", "needs-grok"], ["bad-one-element"], ["x", 5], "not-a-pair"],
                "default_route": "needs-codex",
            }
        }
    )
    assert cfg["enabled"] is True
    assert cfg["routes"] == [("grok", "needs-grok")]
    assert cfg["default_route"] == "needs-codex"


@pytest.mark.parametrize(
    "created_by,expected",
    [
        ("fleetco-grok", "needs-grok"),
        ("grok-review-a", "needs-grok"),
        ("fleetco-claude", "needs-claude"),
        ("FLEETCO-CODEX", "needs-codex"),  # case-insensitive
        ("agent-example", "needs-claude"),  # no substring match -> default
    ],
)
def test_choose_route_first_match_then_default(created_by, expected):
    cfg = bead_reply_mod.resolve_config({})
    assert bead_reply_mod.choose_route(created_by, cfg["routes"], cfg["default_route"]) == expected


def test_choose_route_order_matters_first_match_wins():
    routes = [("codex", "needs-codex"), ("dex", "needs-dex")]
    # "codex" contains "dex" too -- first rule in the list must win.
    assert bead_reply_mod.choose_route("fleetco-codex", routes, "needs-claude") == "needs-codex"


def test_resolve_actor_prefers_configured_actor():
    cfg = {"actor": "critboard-owner"}
    assert bead_reply_mod.resolve_actor(cfg, ["example-human-label"]) == "critboard-owner"


def test_resolve_actor_falls_back_to_first_human_label():
    cfg = {"actor": ""}
    assert bead_reply_mod.resolve_actor(cfg, ["owner-label", "second-label"]) == "owner-label"


def test_resolve_actor_falls_back_to_fixed_default():
    cfg = {"actor": ""}
    assert bead_reply_mod.resolve_actor(cfg, []) == "critboard-human"


@pytest.mark.parametrize("label", ["needs-claude", "example-human", "a", "x_y.z-1"])
def test_validate_label_accepts_real_shapes(label):
    assert validate_label(label) is True


@pytest.mark.parametrize("label", ["", "-flag", "--json", "needs claude", "id;rm -rf /", "a" * 200])
def test_validate_label_rejects_implausible_labels(label):
    assert validate_label(label) is False


# ---------------- do_send_back / do_close against the fake bd -------------


@pytest.mark.asyncio
async def test_do_send_back_issues_comment_then_update_in_order(tmp_path, monkeypatch):
    fake_bd = write_fake_bd_reply(tmp_path / "bd")
    record_file = tmp_path / "record.jsonl"
    monkeypatch.setenv("FAKE_BD_RECORD_FILE", str(record_file))

    result = await bead_reply_mod.do_send_back(
        str(fake_bd), "", "critboard-human", "", "demo-1", "thanks, please continue",
        ["example-human"], "needs-claude",
    )
    calls = read_recorded_argv(record_file)
    assert calls == [
        ["comments", "add", "demo-1", "--", "thanks, please continue"],
        [
            "update", "demo-1",
            "--remove-label", "example-human",
            "--add-label", "needs-claude",
            "--assignee", "", "--status", "open", "--json",
        ],
    ]
    assert result["route"] == "needs-claude"
    assert result["removed_labels"] == ["example-human"]


@pytest.mark.asyncio
async def test_do_send_back_removes_every_human_label_present(tmp_path, monkeypatch):
    fake_bd = write_fake_bd_reply(tmp_path / "bd")
    record_file = tmp_path / "record.jsonl"
    monkeypatch.setenv("FAKE_BD_RECORD_FILE", str(record_file))

    await bead_reply_mod.do_send_back(
        str(fake_bd), "", "critboard-human", "", "demo-1", "go", ["example-human", "owner"], "needs-grok",
    )
    calls = read_recorded_argv(record_file)
    assert calls[1] == [
        "update", "demo-1",
        "--remove-label", "example-human",
        "--remove-label", "owner",
        "--add-label", "needs-grok",
        "--assignee", "", "--status", "open", "--json",
    ]


@pytest.mark.asyncio
async def test_do_close_with_text_adds_comment_then_closes_with_reason(tmp_path, monkeypatch):
    fake_bd = write_fake_bd_reply(tmp_path / "bd")
    record_file = tmp_path / "record.jsonl"
    monkeypatch.setenv("FAKE_BD_RECORD_FILE", str(record_file))

    result = await bead_reply_mod.do_close(
        str(fake_bd), "", "critboard-human", "", "demo-2", "all set, closing"
    )
    calls = read_recorded_argv(record_file)
    assert calls == [
        ["comments", "add", "demo-2", "--", "all set, closing"],
        ["close", "demo-2", "--reason=all set, closing", "--json"],
    ]
    assert result["comment_added"] is True
    assert result["reason"] == "all set, closing"


@pytest.mark.asyncio
async def test_do_close_without_text_skips_comment_uses_default_reason(tmp_path, monkeypatch):
    fake_bd = write_fake_bd_reply(tmp_path / "bd")
    record_file = tmp_path / "record.jsonl"
    monkeypatch.setenv("FAKE_BD_RECORD_FILE", str(record_file))

    result = await bead_reply_mod.do_close(str(fake_bd), "", "critboard-human", "", "demo-2", "")
    calls = read_recorded_argv(record_file)
    assert calls == [["close", "demo-2", f"--reason={bead_reply_mod.DEFAULT_CLOSE_REASON}", "--json"]]
    assert result["comment_added"] is False
    assert result["reason"] == bead_reply_mod.DEFAULT_CLOSE_REASON


# ---------------- HTTP endpoint: isolated app + fake bd -------------------


def _write_isolated_config(tmp_path, bd_bin, extra_sources=None, human_labels=None):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    sources = dict(config_mod.DEFAULT_SOURCES)
    sources["db_path"] = str(tmp_path / "test.db")
    sources["bd_bin"] = str(bd_bin)
    sources["beads_env"] = ""
    sources["beads_dir"] = ""
    if extra_sources:
        sources.update(extra_sources)
    (config_dir / "sources.json").write_text(json.dumps(sources))
    (config_dir / "sources.example.json").write_text(json.dumps(config_mod.DEFAULT_SOURCES))
    layout = {
        "version": 1, "title": "t",
        "human_labels": human_labels if human_labels is not None else ["example-human"],
        "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [{"id": "a", "type": "stat_row", "title": "A", "x": 0, "y": 0, "w": 4, "h": 4}],
    }
    theme = {"name": "t", "colors": {"bg": "#000000"}}
    (config_dir / "layout.json").write_text(json.dumps(layout))
    (config_dir / "theme.json").write_text(json.dumps(theme))
    return config_dir


def _build_client(tmp_path, monkeypatch, bd_bin, extra_sources=None, human_labels=None):
    config_dir = _write_isolated_config(tmp_path, bd_bin, extra_sources, human_labels)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    return TestClient(main_mod.build_app()), config_dir


@pytest.fixture
def fake_bd(tmp_path):
    return write_fake_bd_reply(tmp_path / "bd")


@pytest.fixture
def record_file(tmp_path, monkeypatch):
    rf = tmp_path / "record.jsonl"
    monkeypatch.setenv("FAKE_BD_RECORD_FILE", str(rf))
    return rf


def _enabled_sources():
    # Also keep the real beads COLLECTOR off in these tests: the POST
    # /reply handler triggers scheduler.run_once("beads") after a
    # successful write (see main.py), which would otherwise run
    # bd list/stats/ready against the SAME fake bd + record file and
    # pollute the exact-argv-sequence assertions below.
    return {
        "bead_reply": {**bead_reply_mod.DEFAULT_BEAD_REPLY, "enabled": True},
        "collectors": {"beads": {"enabled": False}},
    }


def test_get_comments_disabled_by_default_returns_403_and_makes_no_bd_call(
    tmp_path, monkeypatch, fake_bd, record_file
):
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd)  # bead_reply defaults to disabled
    resp = client.get("/api/bead/demo-1/comments")
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "bead_reply_disabled"
    assert read_recorded_argv(record_file) == []


def test_post_reply_disabled_by_default_returns_403_and_makes_no_bd_call(
    tmp_path, monkeypatch, fake_bd, record_file
):
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd)
    resp = client.post("/api/bead/demo-1/reply", json={"action": "close", "text": ""})
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "bead_reply_disabled"
    assert read_recorded_argv(record_file) == []


def test_post_reply_refused_when_allow_config_writes_false(tmp_path, monkeypatch, fake_bd, record_file):
    client, _ = _build_client(
        tmp_path, monkeypatch, fake_bd,
        extra_sources={**_enabled_sources(), "allow_config_writes": False},
    )
    resp = client.post("/api/bead/demo-1/reply", json={"action": "close", "text": "done"})
    assert resp.status_code == 403
    assert "allow_config_writes" in resp.json()["detail"]
    assert read_recorded_argv(record_file) == []


def test_post_reply_rejects_bad_bead_id(tmp_path, monkeypatch, fake_bd, record_file):
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd, extra_sources=_enabled_sources())
    resp = client.post("/api/bead/bad%20id/reply", json={"action": "close", "text": "done"})
    assert resp.status_code == 400
    assert read_recorded_argv(record_file) == []


def test_get_comments_rejects_bad_bead_id(tmp_path, monkeypatch, fake_bd, record_file):
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd, extra_sources=_enabled_sources())
    resp = client.get("/api/bead/bad%3Bid/comments")  # "bad;id" -- shell metachar, no bd call allowed
    assert resp.status_code == 400
    assert read_recorded_argv(record_file) == []


def test_post_reply_send_back_empty_text_rejected_before_any_bd_call(
    tmp_path, monkeypatch, fake_bd, record_file
):
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd, extra_sources=_enabled_sources())
    resp = client.post("/api/bead/demo-1/reply", json={"action": "send_back", "text": "   "})
    assert resp.status_code == 400
    assert read_recorded_argv(record_file) == []


def test_post_reply_refuses_bead_not_open(tmp_path, monkeypatch, fake_bd, record_file):
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd, extra_sources=_enabled_sources())
    payload = show_payload("demo-1", "fleetco-grok", ["example-human"], status="in_progress")
    monkeypatch.setenv("FAKE_BD_SHOW_JSON", payload)
    resp = client.post("/api/bead/demo-1/reply", json={"action": "close", "text": ""})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "not_actionable"
    calls = read_recorded_argv(record_file)
    assert calls == [["show", "demo-1", "--json"]]  # only the read, no write


def test_post_reply_refuses_bead_with_no_human_label(tmp_path, monkeypatch, fake_bd, record_file):
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd, extra_sources=_enabled_sources())
    payload = show_payload("demo-1", "fleetco-grok", ["needs-grok"], status="open")
    monkeypatch.setenv("FAKE_BD_SHOW_JSON", payload)
    resp = client.post("/api/bead/demo-1/reply", json={"action": "close", "text": ""})
    assert resp.status_code == 409
    calls = read_recorded_argv(record_file)
    assert calls == [["show", "demo-1", "--json"]]


def test_post_reply_send_back_happy_path_exact_bd_sequence(tmp_path, monkeypatch, fake_bd, record_file):
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd, extra_sources=_enabled_sources())
    payload = show_payload("demo-1", "fleetco-grok", ["example-human"], status="open")
    monkeypatch.setenv("FAKE_BD_SHOW_JSON", payload)
    body_in = {"action": "send_back", "text": "please pick this back up"}
    resp = client.post("/api/bead/demo-1/reply", json=body_in)
    assert resp.status_code == 200
    body = resp.json()
    assert body["route"] == "needs-grok"
    assert body["removed_labels"] == ["example-human"]
    calls = read_recorded_argv(record_file)
    assert calls == [
        ["show", "demo-1", "--json"],
        ["comments", "add", "demo-1", "--", "please pick this back up"],
        [
            "update", "demo-1",
            "--remove-label", "example-human",
            "--add-label", "needs-grok",
            "--assignee", "", "--status", "open", "--json",
        ],
    ]


def test_post_reply_send_back_default_route_for_unmatched_actor(tmp_path, monkeypatch, fake_bd, record_file):
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd, extra_sources=_enabled_sources())
    monkeypatch.setenv(
        "FAKE_BD_SHOW_JSON", show_payload("demo-1", "agent-example", ["example-human"], status="open")
    )
    resp = client.post("/api/bead/demo-1/reply", json={"action": "send_back", "text": "go"})
    assert resp.status_code == 200
    assert resp.json()["route"] == "needs-claude"  # default_route -- no substring matched "agent-example"


def test_post_reply_close_with_text(tmp_path, monkeypatch, fake_bd, record_file):
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd, extra_sources=_enabled_sources())
    payload = show_payload("demo-1", "fleetco-claude", ["example-human"], status="open")
    monkeypatch.setenv("FAKE_BD_SHOW_JSON", payload)
    body_in = {"action": "close", "text": "handled, closing"}
    resp = client.post("/api/bead/demo-1/reply", json=body_in)
    assert resp.status_code == 200
    assert resp.json()["comment_added"] is True
    calls = read_recorded_argv(record_file)
    assert calls == [
        ["show", "demo-1", "--json"],
        ["comments", "add", "demo-1", "--", "handled, closing"],
        ["close", "demo-1", "--reason=handled, closing", "--json"],
    ]


def test_post_reply_close_without_text(tmp_path, monkeypatch, fake_bd, record_file):
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd, extra_sources=_enabled_sources())
    payload = show_payload("demo-1", "fleetco-claude", ["example-human"], status="open")
    monkeypatch.setenv("FAKE_BD_SHOW_JSON", payload)
    resp = client.post("/api/bead/demo-1/reply", json={"action": "close", "text": ""})
    assert resp.status_code == 200
    assert resp.json()["comment_added"] is False
    calls = read_recorded_argv(record_file)
    assert calls == [
        ["show", "demo-1", "--json"],
        ["close", "demo-1", f"--reason={bead_reply_mod.DEFAULT_CLOSE_REASON}", "--json"],
    ]


def test_get_comments_returns_route_preview_and_human_labels_present(
    tmp_path, monkeypatch, fake_bd, record_file
):
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd, extra_sources=_enabled_sources())
    payload = show_payload("demo-1", "fleetco-codex", ["example-human"], status="open")
    monkeypatch.setenv("FAKE_BD_SHOW_JSON", payload)
    comments = [{"id": "c1", "author": "x", "text": "hi", "created_at": "2026-01-01T00:00:00Z"}]
    monkeypatch.setenv("FAKE_BD_COMMENTS_JSON", json.dumps(comments))
    resp = client.get("/api/bead/demo-1/comments")
    assert resp.status_code == 200
    body = resp.json()
    assert body["route_preview"] == "needs-codex"
    assert body["human_labels_present"] == ["example-human"]
    assert body["comments"][0]["text"] == "hi"


def test_config_hand_edit_enabling_bead_reply_takes_effect_without_restart(
    tmp_path, monkeypatch, fake_bd, record_file
):
    client, config_dir = _build_client(tmp_path, monkeypatch, fake_bd)  # starts disabled
    resp = client.get("/api/bead/demo-1/comments")
    assert resp.status_code == 403

    # Hand-edit sources.json on disk (no restart, no rebuilding the client).
    sources = json.loads((config_dir / "sources.json").read_text())
    sources["bead_reply"] = {**bead_reply_mod.DEFAULT_BEAD_REPLY, "enabled": True}
    (config_dir / "sources.json").write_text(json.dumps(sources))

    payload = show_payload("demo-1", "fleetco-claude", ["example-human"], status="open")
    monkeypatch.setenv("FAKE_BD_SHOW_JSON", payload)
    resp2 = client.get("/api/bead/demo-1/comments")
    assert resp2.status_code == 200


def test_snapshot_settings_exposes_bead_reply_enabled(tmp_path, monkeypatch, fake_bd):
    client, _ = _build_client(tmp_path / "a", monkeypatch, fake_bd)
    assert client.get("/api/snapshot").json()["settings"]["bead_reply_enabled"] is False
    client2, _ = _build_client(tmp_path / "b", monkeypatch, fake_bd, extra_sources=_enabled_sources())
    assert client2.get("/api/snapshot").json()["settings"]["bead_reply_enabled"] is True


# ---------------- required injection proof ---------------------------------


def test_injection_reply_text_and_bead_id_never_reach_a_shell(tmp_path, monkeypatch, fake_bd, record_file):
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd, extra_sources=_enabled_sources())

    pwned_marker = tmp_path / "PWNED_critboard"
    pwned_marker2 = tmp_path / "PWNED2"
    assert not pwned_marker.exists()
    assert not pwned_marker2.exists()

    malicious_text = f'"; touch {pwned_marker}; echo "$(id)` $(touch {pwned_marker2})'

    # 1. A bead id containing a shell metacharacter must be rejected with
    #    400 before any subprocess ever runs.
    bad_id = "demo-1;touch-pwned"
    resp_bad_id = client.post(
        f"/api/bead/{quote(bad_id, safe='')}/reply",
        json={"action": "send_back", "text": "hello"},
    )
    assert resp_bad_id.status_code == 400
    assert read_recorded_argv(record_file) == []
    assert not pwned_marker.exists()
    assert not pwned_marker2.exists()

    # 2. A malicious reply body must arrive at bd byte-for-byte verbatim
    #    (proving argv, not shell text) and create no file.
    monkeypatch.setenv(
        "FAKE_BD_SHOW_JSON", show_payload("demo-1", "fleetco-claude", ["example-human"], status="open")
    )
    resp = client.post(
        "/api/bead/demo-1/reply", json={"action": "send_back", "text": malicious_text}
    )
    assert resp.status_code == 200
    calls = read_recorded_argv(record_file)
    comment_call = next(c for c in calls if c[:2] == ["comments", "add"])
    assert comment_call == ["comments", "add", "demo-1", "--", malicious_text]
    assert not pwned_marker.exists()
    assert not pwned_marker2.exists()


def test_bd_exec_argv_never_touches_a_shell_when_no_env_file():
    from critdash.collectors.beads import bd_exec_argv

    argv = bd_exec_argv("", "/path/to/bd", ["comments", "add", "id-1", "text; rm -rf /"])
    assert argv == ["/path/to/bd", "comments", "add", "id-1", "text; rm -rf /"]
    assert "bash" not in argv


def test_bd_exec_argv_sources_env_file_via_positional_params(tmp_path):
    from critdash.collectors.beads import bd_exec_argv

    env_file = tmp_path / "env"
    env_file.write_text("export BEADS_ACTOR=x\n")
    argv = bd_exec_argv(str(env_file), "/path/to/bd", ["comments", "add", "id-1", "hi"])
    assert argv[0] == "bash"
    assert argv[1] == "-c"
    # the fixed script text never contains the env path or user args --
    # they travel as $1, $2, ... positional parameters instead.
    assert str(env_file) not in argv[2]
    assert argv[3] == "_"
    assert argv[4] == str(env_file)
    assert argv[5] == "/path/to/bd"
    assert argv[6:] == ["comments", "add", "id-1", "hi"]


@pytest.mark.parametrize("text", ["-f/etc/hostname", "--file=/etc/hostname", "--help", "-"])
def test_reply_text_starting_with_dash_is_never_parsed_as_a_bd_flag(
    tmp_path, monkeypatch, fake_bd, record_file, text
):
    """Argument injection, not shell injection: bd reads a bare argv element
    starting with "-" as a flag. Verified against real bd that
    `bd comments add <id> -f<path>` (and `--file=<path>`) posts that local
    file's contents as the comment -- a reply could copy any file the
    dashboard can read into the shared beads DB. The comment text must
    follow "--", and a close reason must be bound to its flag as a single
    "--reason=<text>" token."""
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd, extra_sources=_enabled_sources())
    monkeypatch.setenv(
        "FAKE_BD_SHOW_JSON", show_payload("demo-1", "fleetco-claude", ["example-human"], status="open")
    )

    resp = client.post("/api/bead/demo-1/reply", json={"action": "send_back", "text": text})
    assert resp.status_code == 200
    comment_call = next(c for c in read_recorded_argv(record_file) if c[:2] == ["comments", "add"])
    assert comment_call[-2:] == ["--", text]

    record_file.write_text("")
    resp = client.post("/api/bead/demo-1/reply", json={"action": "close", "text": text})
    assert resp.status_code == 200
    calls = read_recorded_argv(record_file)
    close_call = next(c for c in calls if c[:1] == ["close"])
    assert f"--reason={text}" in close_call
    assert text not in close_call  # never a standalone argv element


# ---------------- GET/POST /api/settings/bead-reply -------------------------
# The settings-panel toggle: "enabled" is the ONLY settable key here.
# routes/actor/default_route stay hand-edit-JSON-only, but are reported back
# read-only so the panel can show what the popup will actually do.


def test_get_settings_bead_reply_shape(tmp_path, monkeypatch, fake_bd):
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd)  # bead_reply defaults to disabled
    resp = client.get("/api/settings/bead-reply")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["actor"] == "critboard-human"  # no configured actor -> fixed fallback
    assert body["default_route"] == "needs-claude"
    assert body["beads_configured"] is True  # fake_bd resolves fine, beads_dir unset


def test_post_settings_bead_reply_enabled_true_persists_to_disk_and_memory(tmp_path, monkeypatch, fake_bd):
    client, config_dir = _build_client(tmp_path, monkeypatch, fake_bd)
    resp = client.post("/api/settings/bead-reply", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True

    on_disk = json.loads((config_dir / "sources.json").read_text())
    assert on_disk["bead_reply"]["enabled"] is True

    # In-memory update: reflected on the very next request, no restart, no
    # separate config.reload_sources() call needed.
    assert client.get("/api/settings/bead-reply").json()["enabled"] is True
    assert client.get("/api/snapshot").json()["settings"]["bead_reply_enabled"] is True


def test_post_settings_bead_reply_enabled_false_persists(tmp_path, monkeypatch, fake_bd):
    client, config_dir = _build_client(tmp_path, monkeypatch, fake_bd, extra_sources=_enabled_sources())
    resp = client.post("/api/settings/bead-reply", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    on_disk = json.loads((config_dir / "sources.json").read_text())
    assert on_disk["bead_reply"]["enabled"] is False
    assert client.get("/api/snapshot").json()["settings"]["bead_reply_enabled"] is False


def test_post_settings_bead_reply_rejects_non_boolean(tmp_path, monkeypatch, fake_bd):
    client, config_dir = _build_client(tmp_path, monkeypatch, fake_bd)
    before = (config_dir / "sources.json").read_bytes()
    resp = client.post("/api/settings/bead-reply", json={"enabled": "yes"})
    assert resp.status_code == 400
    assert "errors" in resp.json()["detail"]
    assert (config_dir / "sources.json").read_bytes() == before


@pytest.mark.parametrize("bad_body", [{"routes": []}, {"actor": "x"}, {"default_route": "y"}])
def test_post_settings_bead_reply_rejects_unknown_keys_and_writes_nothing(
    tmp_path, monkeypatch, fake_bd, bad_body
):
    client, config_dir = _build_client(tmp_path, monkeypatch, fake_bd)
    before = (config_dir / "sources.json").read_bytes()
    resp = client.post("/api/settings/bead-reply", json=bad_body)
    assert resp.status_code == 400
    assert "unknown key" in resp.json()["detail"]
    assert (config_dir / "sources.json").read_bytes() == before


def test_post_settings_bead_reply_refused_when_writes_disabled(tmp_path, monkeypatch, fake_bd):
    client, config_dir = _build_client(
        tmp_path, monkeypatch, fake_bd, extra_sources={"allow_config_writes": False}
    )
    before = (config_dir / "sources.json").read_bytes()
    resp = client.post("/api/settings/bead-reply", json={"enabled": True})
    assert resp.status_code == 403
    assert "allow_config_writes" in resp.json()["detail"]
    assert (config_dir / "sources.json").read_bytes() == before


def test_post_settings_bead_reply_toggle_preserves_custom_routes_and_actor(tmp_path, monkeypatch, fake_bd):
    # CRITICAL: bead_reply is an object that also holds actor/routes/
    # default_route -- a naive `doc["bead_reply"] = {"enabled": ...}` would
    # silently wipe these. Prove they survive a toggle both on disk and in
    # what the endpoint itself reports back.
    custom = {
        "enabled": False,
        "actor": "custom-actor",
        "routes": [["mycompany", "needs-mycompany"]],
        "default_route": "needs-mycompany",
    }
    client, config_dir = _build_client(tmp_path, monkeypatch, fake_bd, extra_sources={"bead_reply": custom})

    resp = client.post("/api/settings/bead-reply", json={"enabled": True})
    assert resp.status_code == 200

    on_disk = json.loads((config_dir / "sources.json").read_text())
    assert on_disk["bead_reply"] == {**custom, "enabled": True}

    get_resp = client.get("/api/settings/bead-reply").json()
    assert get_resp["actor"] == "custom-actor"
    assert get_resp["default_route"] == "needs-mycompany"

    # Toggle back off -- routes/actor/default_route still intact.
    client.post("/api/settings/bead-reply", json={"enabled": False})
    on_disk2 = json.loads((config_dir / "sources.json").read_text())
    assert on_disk2["bead_reply"] == custom


def test_post_settings_bead_reply_works_when_bead_reply_key_absent(tmp_path, monkeypatch, fake_bd):
    config_dir = _write_isolated_config(tmp_path, fake_bd)
    sources = json.loads((config_dir / "sources.json").read_text())
    del sources["bead_reply"]
    (config_dir / "sources.json").write_text(json.dumps(sources))
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    client = TestClient(main_mod.build_app())

    resp = client.post("/api/settings/bead-reply", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    on_disk = json.loads((config_dir / "sources.json").read_text())
    assert on_disk["bead_reply"] == {"enabled": True}


def test_snapshot_bead_reply_enabled_flips_without_restart_via_settings_post(tmp_path, monkeypatch, fake_bd):
    client, _ = _build_client(tmp_path, monkeypatch, fake_bd)
    assert client.get("/api/snapshot").json()["settings"]["bead_reply_enabled"] is False
    resp = client.post("/api/settings/bead-reply", json={"enabled": True})
    assert resp.status_code == 200
    assert client.get("/api/snapshot").json()["settings"]["bead_reply_enabled"] is True


def test_get_settings_bead_reply_reports_beads_not_configured_when_bd_missing(tmp_path, monkeypatch):
    # Neutralize detect.py's PATH/candidate-dir fallback scan -- without
    # this, a dev host that genuinely has `bd` installed (this repo's own
    # dev host does, at ~/.local/bin) would find it regardless of the
    # nonsense bd_bin path below. See test_main_helpers.py's
    # _no_binaries_anywhere for the same pattern.
    monkeypatch.setattr(detect_mod, "BINARY_CANDIDATE_DIRS", [])
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: None)
    missing_bd = tmp_path / "no-such-bd-binary"
    client, _ = _build_client(tmp_path, monkeypatch, missing_bd)
    resp = client.get("/api/settings/bead-reply")
    assert resp.status_code == 200
    assert resp.json()["beads_configured"] is False
