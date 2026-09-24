"""Derive each coding-agent session's currently-claimed bead from its OWN
transcript -- never a guess. Beads records claims by ACTOR name (e.g.
`myhost-claude`), and one actor is shared by many concurrent sessions, so
actor alone can never say which session holds which bead. But each
session's transcript records the exact `bd` commands it ran and their
results, so THAT is the source of truth this module reads.

Two verified transcript formats, each behind the same small interface (an
"extractor" with one `feed(state, lines)` method), so adding a third
provider (codex, grok, cursor, ...) later is a new extractor class, not a
rewrite of this module. See `BEAD_TRACKED_KINDS` for which kinds are wired
up; any other kind is intentionally NOT parsed here (server/critdash/main.py
and the JS card use that to say "bead not tracked for <kind>" rather than
falsely claiming "no active bead").

  - Claude Code (~/.claude/projects/<proj>/<session>.jsonl): a Bash tool
    call is an item in a line's `message.content[]` list, type "tool_use",
    keys {id, name: "Bash", input: {command, description}}. Its result is
    an item in a LATER line's `message.content[]`, type "tool_result", keys
    {tool_use_id, is_error, content} where content is a string or a list of
    {type: "text", text} blocks. Verified live on this host 2026-09-23.

  - Kimi (~/.kimi-code/sessions/*/agents/*/wire.jsonl): verified live on
    this host 2026-09-23 by inspecting real session files (structure only
    recorded here -- no real ids, paths, or command/output text copied out
    of any transcript). One JSON object per line, compact (no spaces). An
    assistant turn with tool calls is a line {"type":
    "agent.message.appended", "message": {"message": {"role": "assistant",
    "toolCalls": [{"type": "function", "id": <str>, "name": "Bash",
    "arguments": <JSON-encoded STRING, itself an object with a "command"
    key (+ optional "timeout")>}]}}}. Its result is a LATER line {"type":
    "agent.message.appended", "message": {"message": {"role": "tool",
    "toolCallId": <matches toolCalls[].id above>, "content": [{"type":
    "text", "text": <the command's merged stdout+stderr, same free-text
    shape as Claude's tool_result content>}]}}}. Unlike Claude, the Kimi
    tool-result message carries NO explicit success/failure flag (the
    message's only keys, verified live, are content/role/toolCallId) -- so
    success here is read from the SAME bd-output text markers used for
    Claude (see `_resolve_success_ids`), not from a flag.

Cost: incremental. Each transcript path gets one small `_FileState` cached
across ticks (inode, size, byte offset, a tiny `events` list of already-
resolved successful claim/release actions, and a handful of pending tool
calls awaiting their result line). A later tick reads and JSON-parses only
the bytes appended since the last tick, and even then only lines that
survive a cheap byte-substring prefilter (a candidate tool_use line must
contain `"Bash"` and the substring `bd`; a candidate tool_result line is
only parsed when there is a pending call to match it against). Truncation
or rotation (current size < cached size, or a changed inode) resets that
one file's state and rescans from byte 0 -- the file is small relative to a
whole transcript re-read, since only the O(dozens) bd-related lines ever
touch `events`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime

from .beads import validate_bead_id

# ---------------------------------------------------------------------------
# bd output markers. A call's SUCCESS/FAILURE is still read from is_error and
# these two failure markers (a lost claim race prints "already claimed by
# <actor>" with a non-zero exit; a genuine bd error line starts "Error:") --
# but which BEAD ID a call affected is NEVER read from output text anymore
# (see _resolve_success_ids): only literal id tokens already present in the
# command's own argv (validated by validate_bead_id, imported from beads.py)
# are ever recorded. bd's own "✓ Updated issue: <id>" / "✓ Closed <id>"
# success lines used to be parsed to resolve a claim's real id -- that let a
# result line printed by an UNRELATED call resolve a non-literal ($id) claim
# to a real id (a phantom-claim bug); no such text is parsed for ids now.
# ---------------------------------------------------------------------------
_ALREADY_CLAIMED_RE = re.compile(r"already claimed", re.IGNORECASE)
_ERROR_RE = re.compile(r"(?m)^Error:")

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_TIMEOUT_DURATION_RE = re.compile(r"^\d+[smh]?$")

# `bd update`/`bd close` flags that take a value -- everything else that
# starts with "-" is treated as a boolean flag (its presence recorded, no
# token consumed as its value). Taken from `bd update --help`/`bd close
# --help` on this host, 2026-09-23.
_VALUE_FLAGS = {
    "--acceptance", "--add-label", "-a", "--assignee", "--append-notes",
    "--await-id", "--body-file", "-d", "--description", "--design",
    "--design-file", "--due", "--defer", "-e", "--estimate",
    "--external-ref", "--metadata", "--notes", "--parent", "-p",
    "--priority", "--remove-label", "-r", "--reason", "--reason-file",
    "--session", "--set-labels", "--set-metadata", "--spec-id", "-s",
    "--status", "--title", "-t", "--type", "--unset-metadata", "--actor",
    "--db", "-C", "--directory", "--dolt-auto-commit",
}


@dataclass
class _PendingCall:
    command: str


@dataclass
class _FileState:
    inode: int
    size: int = 0
    offset: int = 0
    tail_buf: bytes = b""
    pending: dict = field(default_factory=dict)  # tool_call_id -> _PendingCall
    events: list = field(default_factory=list)  # (seq, action, bead_id, ts)
    seq: int = 0


_HEREDOC_OPEN_RE = re.compile(r"<<(-)?")
_HEREDOC_WORD_RE = re.compile(r"[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _preprocess_command(command: str) -> str | None:
    """Single quote-aware pass over the WHOLE (possibly multi-line, wrapped)
    command text -- run BEFORE any tokenizing -- that:

    1. Drops heredoc BODIES entirely: from the line after an unquoted
       `<<DELIM` / `<<-DELIM` / `<<'DELIM'` / `<<"DELIM"` marker through the
       terminator line, inclusive (a `<<-` delimiter may be indented with
       tabs, stripped before comparing). A literal `--claim` line inside a
       heredoc body is never even seen by the tokenizer.
    2. Drops the CONTENTS of any unquoted `$( ... )` or backtick command
       substitution (nesting/quotes inside are balanced, not just the first
       `)`/backtick). A `bd` call inside a substitution is a subshell's own
       command, never this session's -- `id=$(bd create ...)` is a create,
       never a claim, so it must not even be tokenized as a candidate call.

    Everything else -- crucially, the text of any quoted argument (e.g. a
    multi-line `-d "..."` description) -- passes through BYTE FOR BYTE, so
    the tokenizer sees exactly the quoting a real shell would: text inside a
    quote is never split into separate segments no matter what it contains
    (this is the Defect-1 fix -- the old code split on physical newlines
    BEFORE honouring quotes, so a quoted multi-line description got sliced
    into separate "commands").

    Returns None on an unterminated quote, `$(...)`, backtick span, or
    heredoc -- the caller skips the whole command rather than guess."""
    out: list[str] = []
    i, n = 0, len(command)
    in_squote = in_dquote = False
    while i < n:
        c = command[i]
        if in_squote:
            out.append(c)
            i += 1
            if c == "'":
                in_squote = False
            continue
        if in_dquote:
            if c == "\\" and i + 1 < n:
                out.append(c)
                out.append(command[i + 1])
                i += 2
                continue
            out.append(c)
            i += 1
            if c == '"':
                in_dquote = False
            continue
        if c == "'":
            in_squote = True
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_dquote = True
            out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(c)
            out.append(command[i + 1])
            i += 2
            continue
        if c == "`":
            j = i + 1
            while j < n and command[j] != "`":
                j += 2 if command[j] == "\\" and j + 1 < n else 1
            if j >= n:
                return None
            i = j + 1
            continue
        if c == "$" and i + 1 < n and command[i + 1] == "(":
            depth = 1
            j = i + 2
            sq = dq = False
            while j < n and depth > 0:
                cj = command[j]
                if sq:
                    if cj == "'":
                        sq = False
                    j += 1
                    continue
                if dq:
                    if cj == "\\" and j + 1 < n:
                        j += 2
                        continue
                    if cj == '"':
                        dq = False
                    j += 1
                    continue
                if cj == "'":
                    sq = True
                elif cj == '"':
                    dq = True
                elif cj == "\\" and j + 1 < n:
                    j += 1
                elif cj == "(":
                    depth += 1
                elif cj == ")":
                    depth -= 1
                j += 1
            if depth != 0:
                return None
            i = j
            continue
        m = _HEREDOC_OPEN_RE.match(command, i)
        if m:
            wm = _HEREDOC_WORD_RE.match(command, m.end())
            if wm:
                delim = wm.group(2)
                strip_tabs = bool(m.group(1))
                out.append(command[i:wm.end()])
                i = wm.end()
                eol = command.find("\n", i)
                if eol == -1:
                    out.append(command[i:])
                    i = n
                    break
                out.append(command[i:eol + 1])
                i = eol + 1
                found = False
                while i < n:
                    line_end = command.find("\n", i)
                    line_end_excl = line_end if line_end != -1 else n
                    line = command[i:line_end_excl]
                    cmp_line = line.lstrip("\t") if strip_tabs else line
                    i = (line_end + 1) if line_end != -1 else n
                    if cmp_line == delim:
                        found = True
                        break
                if not found:
                    return None
                continue
        out.append(c)
        i += 1
    if in_squote or in_dquote:
        return None
    return "".join(out)


_CONTROL_TOKENS = frozenset({"&&", "||", ";", "|", "&"})
_NEWLINE_ONLY_RE = re.compile(r"^\n+$")
# A shell redirection: an optional 1-2 digit fd number immediately followed
# by an operator token made purely of '<'/'>'/'&' (shlex groups adjacent
# punctuation_chars into one token, so "2>&1" tokenizes as '2', '>&', '1'),
# then the redirection's own target token. Stripped from a segment before
# it's handed to _parse_bd_call so a target/fd like the "1" in "2>&1" is
# never mistaken for a positional bead-id argument.
_REDIR_OP_RE = re.compile(r"^&?[<>]{1,2}&?$")
_REDIR_FD_RE = re.compile(r"^\d{1,2}$")


def _strip_redirections(tokens: list[str]) -> list[str]:
    out: list[str] = []
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if _REDIR_FD_RE.match(tok) and i + 1 < n and _REDIR_OP_RE.match(tokens[i + 1]):
            i += 1
            continue
        if _REDIR_OP_RE.match(tok):
            i += 1
            if i < n:
                i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _split_bd_segments(command: str) -> list[list[str]] | None:
    """Tokenize the WHOLE (possibly compound, wrapped, multi-line) command
    respecting shell quoting FIRST (see _preprocess_command), then split the
    resulting token stream into segments at control operators that occur
    OUTSIDE quotes: &&, ||, ;, |, &, and unquoted newlines (bare, unwrapped
    commands on separate lines are separate commands, same as a `;` between
    them). Redirections (2>&1, >out.log, ...) are stripped out of each
    segment before it's returned. Returns None -- never a naive fallback --
    if the command can't be tokenized at all (unterminated quote/
    substitution/heredoc): the whole command is then skipped, counting
    nothing, rather than risk misreading it."""
    pre = _preprocess_command(command)
    if pre is None:
        return None
    try:
        lex = shlex.shlex(pre, posix=True, punctuation_chars=";&|()<>\n")
        lex.whitespace_split = True
        # Verified live (2026-09-23): with punctuation_chars set, shlex's
        # default `whitespace` (' \t\r\n') still silently swallows an
        # unquoted newline as plain whitespace UNLESS it's removed from
        # `whitespace` -- once removed, an unquoted "\n" (or a run of them)
        # is emitted as its own token instead of being dropped, while a
        # newline INSIDE a quoted string still stays part of that one
        # token (quoting is handled before whitespace/punctuation rules).
        lex.whitespace = lex.whitespace.replace("\n", "")
        tokens = list(lex)
    except ValueError:
        return None
    segments: list[list[str]] = []
    seg: list[str] = []
    for tok in tokens:
        if tok in _CONTROL_TOKENS or _NEWLINE_ONLY_RE.match(tok):
            if seg:
                segments.append(_strip_redirections(seg))
            seg = []
        else:
            seg.append(tok)
    if seg:
        segments.append(_strip_redirections(seg))
    return [s for s in segments if s]


def _find_bd_invocation(segment: list[str]) -> list[str] | None:
    """Within one tokenized segment, skip a leading `env`, `timeout <n>`,
    and `VAR=val` prefixes (in any order/mix -- `BEADS_ACTOR=z bd ...`,
    `timeout 60 bd ...`, `timeout 60 BEADS_ACTOR=z bd ...`), then return the
    bd subcommand's own argv (e.g. ["update", "i", "--claim"]) if the
    segment invokes a `bd` binary -- bare name or any path whose basename is
    "bd" (e.g. "/abs/path/bd"). Returns None for a segment that never
    reaches a bd invocation at all: `export BEADS_ACTOR=y` alone, or a
    `grep`/`echo` that merely mentions the text "bd" or "--claim" (its first
    real token is grep/echo, not bd, so it is never even considered a
    candidate -- this is what keeps `grep -- '--claim' file` and `echo "bd
    update x --claim"` from ever being counted)."""
    i, n = 0, len(segment)
    while i < n:
        tok = segment[i]
        if tok == "env":
            i += 1
            continue
        if tok == "timeout":
            i += 1
            if i < n and _TIMEOUT_DURATION_RE.match(segment[i]):
                i += 1
            continue
        if _ENV_ASSIGN_RE.match(tok):
            i += 1
            continue
        break
    if i >= n:
        return None
    if segment[i] == "export":
        # "export VAR=val" with nothing bd-shaped after it in THIS segment
        # (a real "export VAR=val; bd ..." is already two segments, split
        # on ";" above).
        return None
    base = segment[i].rsplit("/", 1)[-1]
    if base != "bd":
        return None
    return segment[i + 1 :]


def _parse_bd_call(argv: list[str]) -> tuple[str, list[str], dict] | None:
    """Parse a bd subcommand's own argv into (subcommand, positional ids,
    flags). "update"/"close"/"done"/"assign" are the only subcommands
    relevant here -- any other (create, show, list, ready, ...) can't claim
    or release.

    `bd assign <id> <name>` (verified live via `bd assign --help`, this
    host, 2026-09-23: "Shorthand for 'bd update <id> --assignee <name>'")
    takes exactly two positionals, id then name (name may be "" to
    unassign) -- parsed separately from update/close/done since its second
    positional is NEVER a bead id and must not be validated/recorded as
    one."""
    if not argv:
        return None
    if argv[0] == "assign":
        positionals = [t for t in argv[1:] if t != "--"]
        if len(positionals) < 2:
            return None
        return "assign", [positionals[0]], {"assignee": positionals[1]}
    if argv[0] not in ("update", "close", "done"):
        return None
    subcmd = argv[0]
    ids: list[str] = []
    flags: dict = {}
    i, n = 1, len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--":
            i += 1
            continue
        if tok.startswith("--") and "=" in tok:
            name, _, val = tok.partition("=")
            flags[name] = val
            i += 1
            continue
        if len(tok) > 1 and tok[0] == "-" and not re.match(r"^-\d", tok):
            if tok in _VALUE_FLAGS:
                val = argv[i + 1] if i + 1 < n else ""
                flags[tok] = val
                i += 2
            else:
                flags[tok] = True
                i += 1
            continue
        ids.append(tok)
        i += 1
    return subcmd, ids, flags


def _classify_bd_call(subcmd: str, ids: list[str], flags: dict) -> str | None:
    """Returns "claim", "release", or None (a call with no bearing on
    claim state, e.g. `bd update i --priority 1`). `bd assign <id> <name>`
    is ALWAYS a release for this session, whatever `<name>` is -- assigning
    to "" (unassign) obviously releases it, but assigning to someone else
    equally means this session no longer holds it, and actor alone can
    never tell us whether "someone else" happens to be this same session's
    own shared actor (see module docstring) -- so a claim is never invented
    from an assign, only ever a release."""
    if subcmd == "assign":
        return "release" if ids else None
    if subcmd in ("close", "done"):
        return "release" if ids else None
    # subcmd == "update"
    if flags.get("--claim"):
        return "claim"
    status = flags.get("--status")
    if status is None:
        status = flags.get("-s")
    if status is not None:
        return "claim" if str(status).strip().lower().replace("-", "_") == "in_progress" else "release"
    assignee = flags.get("--assignee")
    if assignee is None:
        assignee = flags.get("-a")
    if assignee == "":
        return "release"
    return None


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if isinstance(p, str))
    return ""


def _resolve_success_ids(result_text: str, is_error: bool, attempted_ids: list[str]) -> list[str]:
    """Did this bd call succeed? `attempted_ids` are already-literal ids
    taken straight from the command's own argv (see _apply_command) -- this
    function only ever ACCEPTS or REJECTS that exact list, based on
    is_error and known failure-text markers (a lost claim race prints
    "already claimed by <actor>"; a genuine bd error starts "Error:").
    NEVER infers or substitutes an id from `result_text` (Defect 2): bd's
    own "Updated issue: <id>"/"Closed <id>" success lines used to be parsed
    to resolve which id a call touched, which let an UNRELATED call's
    result line resolve a non-literal ($id) claim to a real id -- a phantom
    claim. Missing a variable-based claim is acceptable; inventing one from
    output text is not."""
    if is_error:
        return []
    if _ALREADY_CLAIMED_RE.search(result_text) or _ERROR_RE.search(result_text):
        return []
    return attempted_ids


def _apply_command(
    state: _FileState, command: str, is_error: bool, result_text: str, ts: float | None
) -> None:
    segments = _split_bd_segments(command)
    if segments is None:
        return  # couldn't be tokenized at all -- skip the whole command
    for seg in segments:
        argv = _find_bd_invocation(seg)
        if argv is None:
            continue
        parsed = _parse_bd_call(argv)
        if parsed is None:
            continue
        subcmd, ids, flags = parsed
        # Defect 2: every id argument must be a LITERAL token matching the
        # strict bead-id pattern (validate_bead_id, shared with beads.py) --
        # a variable ($id), command substitution, glob, or anything else
        # non-literal skips the WHOLE call, never just that one id.
        if not ids or not all(validate_bead_id(i) for i in ids):
            continue
        action = _classify_bd_call(subcmd, ids, flags)
        if action is None:
            continue
        for bid in _resolve_success_ids(result_text, is_error, ids):
            state.seq += 1
            state.events.append((state.seq, action, bid, ts))


def _claude_ts(doc: dict) -> float | None:
    ts = doc.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class ClaudeExtractor:
    """Bash tool_use/tool_result pairs in a Claude Code session jsonl."""

    kind = "claude"

    def feed(self, state: _FileState, lines: list[bytes]) -> None:
        for raw in lines:
            if not raw:
                continue
            has_bash = b'"Bash"' in raw
            has_result = b'"tool_result"' in raw
            if not has_bash and not (has_result and state.pending):
                continue
            try:
                doc = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(doc, dict):
                continue
            msg = doc.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                itype = item.get("type")
                if itype == "tool_use" and has_bash and item.get("name") == "Bash":
                    tool_id = item.get("id")
                    command = (item.get("input") or {}).get("command")
                    if tool_id and isinstance(command, str) and "bd" in command:
                        state.pending[tool_id] = _PendingCall(command=command)
                elif itype == "tool_result" and has_result:
                    tool_id = item.get("tool_use_id")
                    pc = state.pending.pop(tool_id, None) if tool_id else None
                    if pc is None:
                        continue
                    is_error = bool(item.get("is_error"))
                    text = _result_text(item.get("content"))
                    _apply_command(state, pc.command, is_error, text, _claude_ts(doc))


class KimiExtractor:
    """Bash toolCalls/tool-role message pairs in a Kimi wire.jsonl."""

    kind = "kimi"

    def feed(self, state: _FileState, lines: list[bytes]) -> None:
        for raw in lines:
            if not raw:
                continue
            has_bash = b'"Bash"' in raw
            # Real Kimi output is compact JSON ("role":"tool", no space),
            # but the prefilter checks the two substrings independently so
            # it degrades safely (favors a false-positive JSON parse over a
            # missed event) if a writer ever spaces its separators.
            has_tool_role = b'"role"' in raw and b'"tool"' in raw
            if not has_bash and not (has_tool_role and state.pending):
                continue
            try:
                doc = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(doc, dict) or doc.get("type") != "agent.message.appended":
                continue
            msg = (doc.get("message") or {}).get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            ts_ms = doc.get("time")
            ts = (ts_ms / 1000.0) if isinstance(ts_ms, int | float) else None
            if role == "assistant" and has_bash:
                for tc in msg.get("toolCalls") or []:
                    if not isinstance(tc, dict) or tc.get("name") != "Bash":
                        continue
                    tool_id = tc.get("id")
                    args_raw = tc.get("arguments")
                    args = None
                    if isinstance(args_raw, str):
                        try:
                            args = json.loads(args_raw)
                        except ValueError:
                            args = None
                    elif isinstance(args_raw, dict):
                        args = args_raw
                    command = args.get("command") if isinstance(args, dict) else None
                    if tool_id and isinstance(command, str) and "bd" in command:
                        state.pending[tool_id] = _PendingCall(command=command)
            elif role == "tool" and has_tool_role:
                tool_id = msg.get("toolCallId")
                pc = state.pending.pop(tool_id, None) if tool_id else None
                if pc is None:
                    continue
                text = _result_text(msg.get("content"))
                # No explicit success/failure flag here (unlike Claude's
                # is_error) -- verified live: the tool-role message's only
                # keys are content/role/toolCallId. is_error=False is passed
                # unconditionally; the same failure-TEXT markers Claude's
                # path uses (see _resolve_success_ids) still apply.
                _apply_command(state, pc.command, False, text, ts)


_EXTRACTORS = {"claude": ClaudeExtractor(), "kimi": KimiExtractor()}
BEAD_TRACKED_KINDS = frozenset(_EXTRACTORS)


def _scan_one_file(path: str, cache: dict[str, _FileState], extractor) -> _FileState | None:
    try:
        st = os.stat(path)
    except OSError:
        cache.pop(path, None)
        return None
    state = cache.get(path)
    if state is None or state.inode != st.st_ino or st.st_size < state.size:
        state = _FileState(inode=st.st_ino)
        cache[path] = state
    if st.st_size <= state.offset:
        state.size = st.st_size
        return state
    try:
        with open(path, "rb") as f:
            f.seek(state.offset)
            chunk = f.read()
    except OSError:
        return state
    data = state.tail_buf + chunk
    parts = data.split(b"\n")
    state.tail_buf = parts[-1]
    lines = parts[:-1]
    state.offset += len(chunk)
    state.size = st.st_size
    extractor.feed(state, lines)
    return state


def _event_key(seq: int, ts: float | None) -> tuple[float, int]:
    # Real wall-clock time orders correctly ACROSS files (e.g. Kimi's main
    # agent + subagent wire.jsonl, each with its own independent seq
    # counter); `seq` only breaks a same-timestamp tie within one file.
    return (ts if ts is not None else float("-inf"), seq)


def _unreleased_claims(events: list[tuple]) -> list[tuple[str, float | None]]:
    """Every bead this session has claimed and not (yet) released, MOST
    RECENT CLAIM FIRST -- replaces the old "single most recent claim, then
    null if released" rule (Defect 3): a session that holds several beads at
    once (claims A, then later claims B while A is still open) must expose
    BOTH, so the caller (agents.py's _apply_bead_cross_check) can pick
    whichever one is still genuinely in_progress right now, not just
    whichever was claimed most recently. A bead claimed more than once by
    this session uses its most recent claim's ordering; "unreleased" means
    no release of that same id happened after that claim (a claim of a
    DIFFERENT id released later does not disqualify it)."""
    latest_claim: dict[str, tuple[int, float | None]] = {}
    for seq, action, bid, ts in events:
        if action != "claim":
            continue
        prev = latest_claim.get(bid)
        if prev is None or _event_key(seq, ts) > _event_key(*prev):
            latest_claim[bid] = (seq, ts)
    unreleased: list[tuple[str, float | None, tuple[float, int]]] = []
    for bid, (seq, ts) in latest_claim.items():
        key = _event_key(seq, ts)
        released_after = any(
            a == "release" and rid == bid and _event_key(rseq, rts) > key
            for rseq, a, rid, rts in events
        )
        if not released_after:
            unreleased.append((bid, ts, key))
    unreleased.sort(key=lambda c: c[2], reverse=True)
    return [(bid, ts) for bid, ts, _key in unreleased]


def resolve_session_bead(
    kind: str, paths: list[str], cache: dict[str, _FileState]
) -> list[tuple[str, float | None]]:
    """Every (bead_id, claim_ts) this session has an unreleased claim on,
    ordered most-recent-claim-first (see _unreleased_claims) -- empty list,
    never None, when there's nothing (or `kind` isn't tracked). Given every
    transcript path that session writes to (Claude: one jsonl; Kimi: one
    wire.jsonl per agent -- main plus any subagents it dispatched, merged by
    real timestamp since each file's own event ordinal isn't comparable
    across files). `cache` is a plain dict the caller keeps across polls,
    keyed by absolute path -- callers should scope one cache dict per
    collector instance and only ever pass paths for sessions currently
    considered live, so it never grows to cover every transcript ever
    written."""
    extractor = _EXTRACTORS.get(kind)
    if extractor is None or not paths:
        return []
    combined: list[tuple] = []
    for path in paths:
        state = _scan_one_file(path, cache, extractor)
        if state is not None:
            combined.extend(state.events)
    return _unreleased_claims(combined)
