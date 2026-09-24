"""Session-transcript bead extraction (server/critdash/collectors/bead_sessions.py).

All fixtures here are synthetic -- invented session ids ("demo-1", "demo-2"),
invented paths, invented bead ids ("demo-a", "demo-b", ...), invented actor
names ("demo-actor-grok" etc.). None of this is captured real transcript
content; see the module docstring for what was actually verified live and
where.

`resolve_session_bead` returns the ORDERED list of (bead_id, claim_ts) this
session has an unreleased claim on, most-recent-claim-first -- never a
single (bead, ts) pair (Defect 3). Most tests below only care about the
FIRST (or only) entry, so `_first` unwraps that for readability.
"""

from __future__ import annotations

import json
import sqlite3

from critdash.collectors.bead_sessions import (
    BEAD_TRACKED_KINDS,
    resolve_session_bead,
)

# ---------------------------------------------------------------------------
# Claude Code line builders
# ---------------------------------------------------------------------------


def claude_tool_use(tool_id: str, command: str, ts: str = "2026-09-23T00:00:00.000Z") -> str:
    return json.dumps({
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": "Bash",
                         "input": {"command": command, "description": "run"}}],
        },
    })


def claude_tool_result(
    tool_id: str, text: str, is_error: bool = False, ts: str = "2026-09-23T00:00:01.000Z"
) -> str:
    return json.dumps({
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id,
                         "is_error": is_error, "content": text}],
        },
    })


def claude_lines(*pairs: tuple[str, str, str, bool]) -> str:
    """Each pair: (tool_id, command, result_text, is_error)."""
    lines = []
    for tool_id, command, result_text, is_error in pairs:
        lines.append(claude_tool_use(tool_id, command))
        lines.append(claude_tool_result(tool_id, result_text, is_error))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Kimi line builders
# ---------------------------------------------------------------------------


def kimi_tool_call(tool_id: str, command: str, time_ms: int = 1000) -> str:
    return json.dumps({
        "type": "agent.message.appended",
        "time": time_ms,
        "message": {"message": {"role": "assistant", "toolCalls": [
            {"type": "function", "id": tool_id, "name": "Bash",
             "arguments": json.dumps({"command": command})},
        ]}},
    })


def kimi_tool_result(tool_id: str, text: str, time_ms: int = 1001) -> str:
    return json.dumps({
        "type": "agent.message.appended",
        "time": time_ms,
        "message": {"message": {"role": "tool", "toolCallId": tool_id,
                                 "content": [{"type": "text", "text": text}]}},
    })


def kimi_lines(*pairs: tuple[str, str, str]) -> str:
    """Each pair: (tool_id, command, result_text)."""
    lines = []
    t = 1000
    for tool_id, command, result_text in pairs:
        lines.append(kimi_tool_call(tool_id, command, t))
        lines.append(kimi_tool_result(tool_id, result_text, t + 1))
        t += 10
    return "\n".join(lines) + "\n"


CLAIM_OK = "✓ Updated issue: demo-a — demo title"
CLAIM_OK_B = "✓ Updated issue: demo-b — demo title b"
CLOSE_OK = "✓ Closed demo-a — demo title"
ASSIGN_OK = "✓ Assigned demo-a — demo title to demo-actor-alice"
UNASSIGN_OK = "✓ Unassigned demo-a — demo title"
ALREADY_CLAIMED = "Error: already claimed by demo-actor-other"


def write(path, content: str):
    path.write_text(content)
    return str(path)


def _first(claims: list[tuple[str, float | None]]) -> str | None:
    return claims[0][0] if claims else None


def _ids(claims: list[tuple[str, float | None]]) -> list[str]:
    return [bid for bid, _ts in claims]


# ---------------------------------------------------------------------------
# Claude extractor -- basic claim/release shapes
# ---------------------------------------------------------------------------


def test_claude_plain_claim(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --claim", CLAIM_OK, False),
    ))
    claims = resolve_session_bead("claude", [p], {})
    assert _first(claims) == "demo-a"
    assert claims[0][1] is not None


def test_claude_status_in_progress_claim(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --status in_progress", CLAIM_OK, False),
    ))
    assert _first(resolve_session_bead("claude", [p], {})) == "demo-a"


def test_claude_compound_wrapped_command(tmp_path):
    cmd = (
        "cd /srv/demo/repo && . ~/.config/beads/env; export BEADS_ACTOR=demo-actor-y; "
        "timeout 60 bd update demo-a --claim 2>&1 | tail -1"
    )
    p = write(tmp_path / "s.jsonl", claude_lines(("t1", cmd, CLAIM_OK, False)))
    assert _first(resolve_session_bead("claude", [p], {})) == "demo-a"


def test_claude_absolute_bd_path_and_actor_prefix(tmp_path):
    cmd = "BEADS_ACTOR=demo-actor-z /home/user/.local/bin/bd update demo-a --claim"
    p = write(tmp_path / "s.jsonl", claude_lines(("t1", cmd, CLAIM_OK, False)))
    assert _first(resolve_session_bead("claude", [p], {})) == "demo-a"


def test_claude_newline_separated_commands(tmp_path):
    cmd = "export BEADS_ACTOR=demo-actor-y\nbd update demo-a --claim"
    p = write(tmp_path / "s.jsonl", claude_lines(("t1", cmd, CLAIM_OK, False)))
    assert _first(resolve_session_bead("claude", [p], {})) == "demo-a"


def test_claude_two_real_newline_separated_commands_each_counted(tmp_path):
    cmd = "bd update demo-a --claim\nbd update demo-b --claim"
    p = write(tmp_path / "s.jsonl", claude_lines(("t1", cmd, CLAIM_OK + "\n" + CLAIM_OK_B, False)))
    claims = resolve_session_bead("claude", [p], {})
    assert _ids(claims) == ["demo-b", "demo-a"]


def test_claude_failed_claim_is_error_ignored(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --claim", ALREADY_CLAIMED, True),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_claude_is_error_true_overrides_success_looking_text(tmp_path):
    # Text alone looks like a clean success -- only is_error marks it failed
    # (isolates the is_error guard from the text-marker guards below).
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --claim", CLAIM_OK, True),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_claude_failed_claim_already_claimed_text_ignored(tmp_path):
    # is_error False but the text itself says the race was lost.
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --claim", "already claimed by demo-actor-other", False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_claude_claim_then_close_is_null(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --claim", CLAIM_OK, False),
        ("t2", "bd close demo-a", CLOSE_OK, False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_claude_close_several_ids(tmp_path):
    close_text = "✓ Closed demo-a — t\n✓ Closed demo-b — t"
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --claim", CLAIM_OK, False),
        ("t2", "bd update demo-b --claim", CLAIM_OK_B, False),
        ("t3", "bd close demo-a demo-b", close_text, False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_claude_claim_a_then_claim_b_gives_both_newest_first(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --claim", CLAIM_OK, False),
        ("t2", "bd update demo-b --claim", CLAIM_OK_B, False),
    ))
    claims = resolve_session_bead("claude", [p], {})
    assert _ids(claims) == ["demo-b", "demo-a"]


def test_claude_release_via_status_change(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --claim", CLAIM_OK, False),
        ("t2", "bd update demo-a --status open", "✓ Updated issue: demo-a — t", False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_claude_release_via_assignee_clear(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --claim", CLAIM_OK, False),
        ("t2", 'bd update demo-a --assignee ""', "✓ Updated issue: demo-a — t", False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_claude_echo_and_grep_not_counted(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "grep -- '--claim' notes.txt", "notes.txt:1: --claim seen", False),
        ("t2", 'echo "bd update demo-a --claim"', "bd update demo-a --claim", False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_claude_lookalike_binary_not_counted_as_bd(tmp_path):
    # basename must be exactly "bd" -- a lookalike ("notbd") that happens to
    # take the same-shaped argv is not a bd invocation.
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "notbd update demo-a --claim", CLAIM_OK, False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_claude_not_tracked_gets_no_events_for_unrelated_command(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --priority 1", "✓ Updated issue: demo-a — t", False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_incremental_scan_picks_up_appended_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(claude_tool_use("t1", "bd update demo-a --claim") + "\n")
    cache = {}
    assert resolve_session_bead("claude", [str(p)], cache) == []  # result line not written yet
    with open(p, "a") as f:
        f.write(claude_tool_result("t1", CLAIM_OK) + "\n")
    assert _first(resolve_session_bead("claude", [str(p)], cache)) == "demo-a"
    # offset should have advanced past the first read -- verify by checking
    # the cached state didn't reparse from 0 (tail_buf empty, size matches).
    state = cache[str(p)]
    assert state.offset == p.stat().st_size


def test_truncation_triggers_rescan(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(claude_lines(
        ("t1", "bd update demo-a --claim", CLAIM_OK, False),
        ("t2", "bd update demo-a --priority 1", "✓ Updated issue: demo-a — t", False),
    ))
    cache = {}
    assert _first(resolve_session_bead("claude", [str(p)], cache)) == "demo-a"
    # Truncate to a strictly SHORTER file (the spec's own trigger: "size
    # shrank or inode changed -> rescan") holding a different claim.
    p.write_text(claude_lines(("t9", "bd update demo-b --claim", CLAIM_OK_B, False)))
    assert _first(resolve_session_bead("claude", [str(p)], cache)) == "demo-b"


def test_rotation_inode_change_triggers_rescan(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(claude_lines(("t1", "bd update demo-a --claim", CLAIM_OK, False)))
    cache = {}
    assert _first(resolve_session_bead("claude", [str(p)], cache)) == "demo-a"
    p.unlink()
    p.write_text(claude_lines(("t9", "bd update demo-b --claim", CLAIM_OK_B, False)))
    assert _first(resolve_session_bead("claude", [str(p)], cache)) == "demo-b"


# ---------------------------------------------------------------------------
# Defect 1: quoting must be honoured BEFORE splitting on control operators /
# newlines -- text inside a quoted string is never a command, however much
# it looks like one.
# ---------------------------------------------------------------------------


def test_defect1_quoted_multiline_description_with_embedded_fake_claims(tmp_path):
    # The exact shape from the real, verified incident (synthetic ids/actor):
    # one Bash call runs `id=$(bd create ...)`, then `bd update "$id" -d
    # "<handoff text>"` where the handoff text is a DOUBLE-QUOTED, multi-line
    # description containing handoff instructions for another agent,
    # including lines that themselves look exactly like a claim -- one with a
    # non-literal id ($id) and one with a LITERAL id (demo-9). Neither may
    # ever be counted: they are text inside a quoted string, not commands
    # this session ran.
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
    result_text = "✓ Updated issue: demo-x — handoff bead"
    p = write(tmp_path / "s.jsonl", claude_lines(("t1", cmd, result_text, False)))
    assert resolve_session_bead("claude", [p], {}) == []


def test_defect1_heredoc_body_with_literal_claim_not_counted(tmp_path):
    cmd = "cat <<EOF\nbd update demo-1 --claim\nEOF\n"
    p = write(tmp_path / "s.jsonl", claude_lines(("t1", cmd, "ok", False)))
    assert resolve_session_bead("claude", [p], {}) == []


def test_defect1_dash_heredoc_body_with_literal_claim_not_counted(tmp_path):
    cmd = "cat <<-EOF\n\tbd update demo-1 --claim\n\tEOF\n"
    p = write(tmp_path / "s.jsonl", claude_lines(("t1", cmd, "ok", False)))
    assert resolve_session_bead("claude", [p], {}) == []


def test_defect1_echo_quoted_claim_text_not_counted(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", 'echo "bd update demo-1 --claim"', "bd update demo-1 --claim", False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_defect1_grep_claim_text_not_counted(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "grep -- '--claim' f", "f:1: --claim", False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_defect1_printf_claim_text_not_counted(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "printf '%s' \"line one\nline two --claim\"", "line one\nline two --claim", False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_defect1_quoted_literal_id_claim_counted(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", 'bd update "demo-1" --claim', "✓ Updated issue: demo-1 — t", False),
    ))
    assert _first(resolve_session_bead("claude", [p], {})) == "demo-1"


def test_defect1_timeout_pipe_redirection_claim_counted(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "timeout 60 bd update demo-1 --claim 2>&1 | tail -1", "✓ Updated issue: demo-1 — t", False),
    ))
    assert _first(resolve_session_bead("claude", [p], {})) == "demo-1"


def test_defect1_cd_and_env_wrapped_claim_counted(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "cd x && . env; export A=b; bd update demo-1 --claim", "✓ Updated issue: demo-1 — t", False),
    ))
    assert _first(resolve_session_bead("claude", [p], {})) == "demo-1"


def test_defect1_command_substitution_create_not_counted(tmp_path):
    # id=$(bd create ...) is a create inside a subshell -- never a claim,
    # regardless of what the substitution's own output says.
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", 'id=$(bd create "x" --json)', "some-id-123", False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_defect1_command_substitution_as_id_argument_skipped(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update $(cat f) --claim", "✓ Updated issue: demo-1 — t", False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


# ---------------------------------------------------------------------------
# Defect 2: non-literal id arguments never count, and the id is NEVER
# resolved from tool_result text.
# ---------------------------------------------------------------------------


def test_defect2_bare_dollar_var_id_skipped(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update $BID --claim", "✓ Updated issue: demo-a — t", False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_defect2_braced_dollar_var_id_skipped(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", 'bd update "${ID}" --claim', "✓ Updated issue: demo-a — t", False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_defect2_never_infers_id_from_result_text_when_id_missing(tmp_path):
    # No id argument at all -- must not fall back to whatever id the (fake,
    # attacker-controlled-shaped) result text happens to mention.
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update --claim", "✓ Updated issue: demo-a — t", False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


# ---------------------------------------------------------------------------
# bd assign -- must count as a RELEASE (unassign OR assign-to-anyone)
# ---------------------------------------------------------------------------


def test_assign_empty_after_claim_releases(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --claim", CLAIM_OK, False),
        ("t2", 'bd assign demo-a ""', UNASSIGN_OK, False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_assign_to_someone_after_claim_releases(tmp_path):
    # Assigning to anyone else (or in fact anyone at all -- actor alone can't
    # tell us whether "someone" is this session's own shared actor) releases
    # this session's claim; never invents a re-claim from an assign.
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --claim", CLAIM_OK, False),
        ("t2", "bd assign demo-a demo-actor-alice", ASSIGN_OK, False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_assign_quoted_literal_id_still_releases(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --claim", CLAIM_OK, False),
        ("t2", 'bd assign "demo-a" ""', UNASSIGN_OK, False),
    ))
    assert resolve_session_bead("claude", [p], {}) == []


def test_assign_nonliteral_id_skipped(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --claim", CLAIM_OK, False),
        ("t2", 'bd assign "$id" ""', UNASSIGN_OK, False),
    ))
    # the assign is skipped (non-literal id), so the earlier claim survives.
    assert _first(resolve_session_bead("claude", [p], {})) == "demo-a"


# ---------------------------------------------------------------------------
# Defect 3: expose every unreleased claim, ordered newest first -- not just
# the single most recent one.
# ---------------------------------------------------------------------------


def test_defect3_claim_a_then_b_both_unreleased_b_first(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --claim", CLAIM_OK, False),
        ("t2", "bd update demo-b --claim", CLAIM_OK_B, False),
    ))
    claims = resolve_session_bead("claude", [p], {})
    assert _ids(claims) == ["demo-b", "demo-a"]


def test_defect3_claim_a_claim_b_release_b_leaves_only_a(tmp_path):
    p = write(tmp_path / "s.jsonl", claude_lines(
        ("t1", "bd update demo-a --claim", CLAIM_OK, False),
        ("t2", "bd update demo-b --claim", CLAIM_OK_B, False),
        ("t3", "bd close demo-b", "✓ Closed demo-b — t", False),
    ))
    claims = resolve_session_bead("claude", [p], {})
    assert _ids(claims) == ["demo-a"]


# ---------------------------------------------------------------------------
# Kimi extractor
# ---------------------------------------------------------------------------


def test_kimi_plain_claim(tmp_path):
    p = write(tmp_path / "wire.jsonl", kimi_lines(
        ("k1", "bd update demo-a --claim", CLAIM_OK),
    ))
    claims = resolve_session_bead("kimi", [p], {})
    assert _first(claims) == "demo-a"
    assert claims[0][1] == 1.001


def test_kimi_claim_then_close_null(tmp_path):
    p = write(tmp_path / "wire.jsonl", kimi_lines(
        ("k1", "bd update demo-a --claim", CLAIM_OK),
        ("k2", "bd close demo-a", CLOSE_OK),
    ))
    assert resolve_session_bead("kimi", [p], {}) == []


def test_kimi_already_claimed_ignored(tmp_path):
    p = write(tmp_path / "wire.jsonl", kimi_lines(
        ("k1", "bd update demo-a --claim", "already claimed by demo-actor-other"),
    ))
    assert resolve_session_bead("kimi", [p], {}) == []


def test_kimi_multiple_agent_wire_paths_merge_by_time(tmp_path):
    main_p = write(tmp_path / "main.jsonl", kimi_lines(
        ("k1", "bd update demo-a --claim", CLAIM_OK),
    ))
    sub_p = write(tmp_path / "sub.jsonl", "\n".join([
        kimi_tool_call("k2", "bd update demo-b --claim", 5000),
        kimi_tool_result("k2", CLAIM_OK_B, 5001),
    ]) + "\n")
    claims = resolve_session_bead("kimi", [main_p, sub_p], {})
    assert _ids(claims) == ["demo-b", "demo-a"]  # later real timestamp first


def test_kimi_assign_release(tmp_path):
    p = write(tmp_path / "wire.jsonl", kimi_lines(
        ("k1", "bd update demo-a --claim", CLAIM_OK),
        ("k2", 'bd assign demo-a ""', UNASSIGN_OK),
    ))
    assert resolve_session_bead("kimi", [p], {}) == []


# ---------------------------------------------------------------------------
# tracked-kinds
# ---------------------------------------------------------------------------


def test_bead_tracked_kinds():
    assert BEAD_TRACKED_KINDS == frozenset({"claude", "kimi", "codex", "grok", "cursor"})


def test_unknown_kind_returns_empty_list(tmp_path):
    p = write(tmp_path / "x.jsonl", "not used\n")
    assert resolve_session_bead("opencode", [p], {}) == []


# ---------------------------------------------------------------------------
# Codex line builders -- one item_completed line IS the (command, result)
# pair (see bead_sessions.py's module docstring for the verified shape).
# ---------------------------------------------------------------------------


def codex_item_completed(
    command: str, exit_code: int = 0, status: str = "completed",
    stdout: str = "ok", ts_ms: int = 1000, timestamp: str = "2026-09-23T00:00:00.000Z",
) -> str:
    return json.dumps({
        "type": "event_msg",
        "timestamp": timestamp,
        "payload": {
            "type": "item_completed",
            "started_at_ms": ts_ms - 5,
            "completed_at_ms": ts_ms,
            "item": {
                "type": "CommandExecution",
                "command": ["/bin/bash", "-lc", command],
                "status": status,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": "",
                "aggregated_output": stdout,
            },
        },
    })


def codex_lines(*items: tuple[str, int, str, str]) -> str:
    """Each item: (command, exit_code, status, stdout)."""
    lines = []
    t = 1000
    for command, exit_code, status, stdout in items:
        lines.append(codex_item_completed(command, exit_code, status, stdout, t))
        t += 10
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Codex extractor
# ---------------------------------------------------------------------------


def test_codex_plain_claim(tmp_path):
    p = write(tmp_path / "rollout.jsonl", codex_lines(
        ("bd update demo-a --claim", 0, "completed", CLAIM_OK),
    ))
    claims = resolve_session_bead("codex", [p], {})
    assert _first(claims) == "demo-a"
    assert claims[0][1] is not None


def test_codex_compound_wrapped_command(tmp_path):
    p = write(tmp_path / "rollout.jsonl", codex_lines(
        ("cd /demo/work && bd update demo-a --claim", 0, "completed", CLAIM_OK),
    ))
    assert _first(resolve_session_bead("codex", [p], {})) == "demo-a"


def test_codex_quoted_handoff_text_not_counted(tmp_path):
    p = write(tmp_path / "rollout.jsonl", codex_lines(
        ('echo "bd update demo-a --claim"', 0, "completed", "bd update demo-a --claim"),
    ))
    assert resolve_session_bead("codex", [p], {}) == []


def test_codex_failed_claim_exit_code_ignored(tmp_path):
    # stdout deliberately does NOT contain "Error:"/"already claimed" -- this
    # pins success/failure to `status`/`exit_code` alone (see
    # CodexExtractor.feed), not the shared text-marker fallback every
    # extractor also has.
    p = write(tmp_path / "rollout.jsonl", codex_lines(
        ("bd update demo-a --claim", 1, "failed", "permission denied doing thing"),
    ))
    assert resolve_session_bead("codex", [p], {}) == []


def test_codex_claim_then_close_is_null(tmp_path):
    p = write(tmp_path / "rollout.jsonl", codex_lines(
        ("bd update demo-a --claim", 0, "completed", CLAIM_OK),
        ("bd close demo-a", 0, "completed", CLOSE_OK),
    ))
    assert resolve_session_bead("codex", [p], {}) == []


def test_codex_nonliteral_id_skipped(tmp_path):
    p = write(tmp_path / "rollout.jsonl", codex_lines(
        ("bd update $id --claim", 0, "completed", CLAIM_OK),
    ))
    assert resolve_session_bead("codex", [p], {}) == []


def test_codex_non_bash_lc_command_shape_skipped(tmp_path):
    # command is a list but not the verified ["/bin/bash", "-lc", <script>]
    # shape -- skipped rather than guessed at.
    line = json.dumps({
        "type": "event_msg",
        "timestamp": "2026-09-23T00:00:00.000Z",
        "payload": {
            "type": "item_completed",
            "completed_at_ms": 1000,
            "item": {
                "type": "CommandExecution",
                "command": ["bd", "update", "demo-a", "--claim"],
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": CLAIM_OK,
            },
        },
    })
    p = write(tmp_path / "rollout.jsonl", line + "\n")
    assert resolve_session_bead("codex", [p], {}) == []


# ---------------------------------------------------------------------------
# Grok line builders -- command in chat_history.jsonl, pass/fail verdict in
# the SEPARATE events.jsonl (see bead_sessions.py's module docstring).
# ---------------------------------------------------------------------------


def grok_assistant_call(tool_id: str, command: str) -> str:
    return json.dumps({
        "type": "assistant",
        "content": "demo turn",
        "tool_calls": [{
            "id": tool_id, "name": "run_terminal_command",
            "arguments": json.dumps({"command": command, "description": "run"}),
        }],
        "model_id": "demo-model",
    })


def grok_tool_result_pruned(tool_id: str) -> str:
    # chat_history.jsonl's own result text is untrustworthy once pruned --
    # every fixture uses the real observed placeholder to prove events.jsonl
    # (not this) is what decides success/failure.
    return json.dumps({"type": "tool_result", "tool_call_id": tool_id,
                        "content": "[Tool result omitted — too old]"})


def grok_tool_completed(tool_id: str, outcome: str, ts: str) -> str:
    return json.dumps({"ts": ts, "type": "tool_completed", "tool_name": "run_terminal_command",
                        "duration_ms": 12, "outcome": outcome, "tool_call_id": tool_id})


def grok_session(tmp_path, name: str, *pairs: tuple[str, str, str]) -> list[str]:
    """Each pair: (tool_id, command, outcome). Returns [chat_history_path,
    events_path] in the fixed order GrokExtractor/_scan_grok_group require."""
    chat_lines: list[str] = []
    event_lines: list[str] = []
    for i, (tool_id, command, outcome) in enumerate(pairs):
        chat_lines.append(grok_assistant_call(tool_id, command))
        chat_lines.append(grok_tool_result_pruned(tool_id))
        event_lines.append(grok_tool_completed(tool_id, outcome, f"2026-09-23T00:00:{i:02d}.000Z"))
    d = tmp_path / name
    d.mkdir()
    chat_p = write(d / "chat_history.jsonl", "\n".join(chat_lines) + "\n")
    events_p = write(d / "events.jsonl", "\n".join(event_lines) + "\n")
    return [chat_p, events_p]


# ---------------------------------------------------------------------------
# Grok extractor
# ---------------------------------------------------------------------------


def test_grok_plain_claim(tmp_path):
    paths = grok_session(tmp_path, "s1", ("g1", "bd update demo-a --claim", "success"))
    claims = resolve_session_bead("grok", paths, {})
    assert _first(claims) == "demo-a"
    assert claims[0][1] is not None


def test_grok_compound_wrapped_command(tmp_path):
    paths = grok_session(tmp_path, "s1", ("g1", "cd /demo/work && bd update demo-a --claim", "success"))
    assert _first(resolve_session_bead("grok", paths, {})) == "demo-a"


def test_grok_quoted_handoff_text_not_counted(tmp_path):
    paths = grok_session(tmp_path, "s1", ("g1", 'echo "bd update demo-a --claim"', "success"))
    assert resolve_session_bead("grok", paths, {}) == []


def test_grok_failed_claim_outcome_error_ignored(tmp_path):
    # chat_history.jsonl's own (pruned) result text looks like neither
    # success nor a known failure marker -- only events.jsonl's "outcome"
    # decides this, proving the join is load-bearing, not decorative.
    paths = grok_session(tmp_path, "s1", ("g1", "bd update demo-a --claim", "error"))
    assert resolve_session_bead("grok", paths, {}) == []


def test_grok_claim_then_close_is_null(tmp_path):
    paths = grok_session(
        tmp_path, "s1",
        ("g1", "bd update demo-a --claim", "success"),
        ("g2", "bd close demo-a", "success"),
    )
    assert resolve_session_bead("grok", paths, {}) == []


def test_grok_nonliteral_id_skipped(tmp_path):
    paths = grok_session(tmp_path, "s1", ("g1", "bd update $id --claim", "success"))
    assert resolve_session_bead("grok", paths, {}) == []


def test_grok_missing_events_file_gives_no_claims(tmp_path):
    # Only chat_history.jsonl exists (e.g. events.jsonl not found by the
    # caller's lookup) -- resolve_session_bead requires exactly 2 paths.
    chat_p = write(
        tmp_path / "chat_history.jsonl", grok_assistant_call("g1", "bd update demo-a --claim") + "\n"
    )
    assert resolve_session_bead("grok", [chat_p], {}) == []


# ---------------------------------------------------------------------------
# Cursor db builder -- store.db's `blobs` table (see bead_sessions.py's
# module docstring for the verified schema).
# ---------------------------------------------------------------------------


def cursor_db(tmp_path, name: str, *pairs: tuple[str, str, bool]) -> str:
    """Each pair: (tool_call_id, command, is_error). Writes a synthetic
    store.db with one assistant tool-call blob and one tool tool-result
    blob per pair, in insertion order (CursorExtractor reads `ORDER BY
    rowid`, so insertion order IS read order here)."""
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


# ---------------------------------------------------------------------------
# Cursor extractor
# ---------------------------------------------------------------------------


def test_cursor_plain_claim(tmp_path):
    p = cursor_db(tmp_path, "store.db", ("c1", "bd update demo-a --claim", False))
    claims = resolve_session_bead("cursor", [p], {})
    assert _first(claims) == "demo-a"


def test_cursor_compound_wrapped_command(tmp_path):
    p = cursor_db(tmp_path, "store.db", ("c1", "cd /demo/work && bd update demo-a --claim", False))
    assert _first(resolve_session_bead("cursor", [p], {})) == "demo-a"


def test_cursor_quoted_handoff_text_not_counted(tmp_path):
    p = cursor_db(tmp_path, "store.db", ("c1", 'echo "bd update demo-a --claim"', False))
    assert resolve_session_bead("cursor", [p], {}) == []


def test_cursor_failed_claim_is_error_ignored(tmp_path):
    p = cursor_db(tmp_path, "store.db", ("c1", "bd update demo-a --claim", True))
    assert resolve_session_bead("cursor", [p], {}) == []


def test_cursor_claim_then_close_is_null(tmp_path):
    p = cursor_db(
        tmp_path, "store.db",
        ("c1", "bd update demo-a --claim", False),
        ("c2", "bd close demo-a", False),
    )
    assert resolve_session_bead("cursor", [p], {}) == []


def test_cursor_nonliteral_id_skipped(tmp_path):
    p = cursor_db(tmp_path, "store.db", ("c1", "bd update $id --claim", False))
    assert resolve_session_bead("cursor", [p], {}) == []


def test_cursor_binary_blob_rows_skipped_safely(tmp_path):
    # A real store.db is roughly 2/3 non-UTF8 binary "pointer" blobs (see
    # module docstring) -- must not crash or be mistaken for a message row.
    p = cursor_db(tmp_path, "store.db", ("c1", "bd update demo-a --claim", False))
    con = sqlite3.connect(p)
    con.execute("INSERT INTO blobs (id, data) VALUES (?, ?)", ("demo-binary", b"\x00\x01\xff\xfe\x02"))
    con.commit()
    con.close()
    assert _first(resolve_session_bead("cursor", [p], {})) == "demo-a"


def test_cursor_missing_db_gives_no_claims(tmp_path):
    assert resolve_session_bead("cursor", [str(tmp_path / "missing.db")], {}) == []
