#!/usr/bin/env bash
# check-public.sh -- regression guard for the open-source CritBoard
# (critfusion/critboard) repo. Greps every GIT-TRACKED file for personal/infrastructure data that
# should never ship publicly: real hostnames, real absolute home-dir paths,
# private/tailnet IPs, credential-shaped strings, and this project's old
# bead-id prefix. Exits 1 with a file:line list of every finding, exits 0
# when clean.
#
# Run standalone from anywhere in the repo:
#   bash scripts/check-public.sh
#
# Optional ground-truth mode derives the name list from THIS machine (repo
# roots, local beads data, GitHub org) instead of relying on a hand-written
# list -- opt-in only, see the "ground-truth" block below:
#   CHECK_PUBLIC_GROUND_TRUTH=1 bash scripts/check-public.sh
#   bash scripts/check-public.sh --ground-truth
#
# This is the thing that stops a sanitized leak from coming back: run it
# before every commit that touches fixtures, docs, or config defaults.

set -uo pipefail

# Every `"${ARR[@]}"` expansion below of an array that can legitimately be
# empty (LITERALS, LOCAL_OVERRIDES, FILES, ...) is written as
# `"${ARR[@]+"${ARR[@]}"}"` instead. This is not decoration: bash versions
# before 4.4 -- including 3.2, the version macOS ships and will not upgrade
# past (Apple will not ship a newer, GPLv3-licensed bash) -- treat an EMPTY
# array's `[@]` expansion as an unset variable under `set -u`/`nounset` and
# abort with "unbound variable", even though the array itself was declared.
# Confirmed live under a real bash 3.2.0 build: `declare -a X=(); set -u;
# for i in "${X[@]}"; do :; done` exits with "X[@]: unbound variable". The
# `${ARR[@]+word}` form only substitutes `word` when ARR is set at all
# (regardless of element count), which sidesteps the bug on every bash
# version this script supports while still iterating zero times over a
# genuinely empty array.
repo_root="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$repo_root" ]; then
    echo "check-public.sh: not inside a git repository -- nothing to check against." >&2
    exit 1
fi
cd "$repo_root"

# Tracked and untracked-but-not-ignored files. Scans the full tree that would
# be shipped to public (including untracked files about to be committed).
# config/sources.json remains excluded: gitignored by design, machine-local,
# holding real paths/hosts is its whole job.
# NUL-delimited read loop instead of `mapfile` (bash 4+ only) -- macOS ships
# bash 3.2 and will not ship a GPLv3 bash 4/5, so this script must stay
# 3.2-compatible. `read -d ''` (empty delimiter -> NUL) works on bash 3.2.
declare -a FILES=()
while IFS= read -r -d '' _f; do
    FILES+=("$_f")
done < <(git ls-files --cached --others --exclude-standard -z)

found=0
report() { # file lineno reason
    printf '%s:%s: %s\n' "$1" "$2" "$3"
    found=1
}

grep_pattern() { # file pattern reason [-i] [-P] [--allow <string>...]
                  # (-P selects PCRE for lookaround; without it the pattern runs as ERE.
                  # -E/-P are mutually exclusive to grep, so pick exactly one engine
                  # flag here rather than passing both. --allow strings are substring-matched and skipped.)
    local file="$1" pattern="$2" reason="$3" engine="-E"
    shift 3
    local extra=() allow_strings=()

    # Parse flags and collect --allow patterns
    while [ $# -gt 0 ]; do
        case "$1" in
            -P) engine="-P" ;;
            -i) extra+=("$1") ;;
            --allow)
                shift
                [ $# -gt 0 ] && allow_strings+=("$1")
                ;;
            *) extra+=("$1") ;;
        esac
        shift
    done

    while IFS=: read -r lineno rest; do
        # A line containing the literal word FAKE is this codebase's own
        # convention for a deliberately-fabricated credential used to test
        # redaction/parsing logic (see server/tests/test_quota.py,
        # test_analytics.py) -- never a real leaked secret. Skip it here
        # instead of weakening the credential-shape patterns themselves.
        case "$rest" in *FAKE*) continue ;; esac

        # Skip lines matching allow-list strings
        for allow_str in "${allow_strings[@]+"${allow_strings[@]}"}"; do
            if [[ "$rest" == *"$allow_str"* ]]; then
                continue 2  # Continue outer while loop
            fi
        done

        [ -n "$lineno" ] && report "$file" "$lineno" "$reason"
    done < <(grep -n "${extra[@]+"${extra[@]}"}" "$engine" "$pattern" -- "$file" 2>/dev/null)
}

# -- literal patterns: shipped rules are generic + dynamic hostname check ---
# Shipped literals are empty. Machine-specific patterns are loaded from a local
# overrides file (scripts/check-public.local) if it exists -- see step 1 below.
declare -a LITERALS=()

# Load machine-local overrides (not published, .gitignored)
declare -a LOCAL_OVERRIDES=()
local_overrides_file="$(dirname "$0")/check-public.local"
loaded_local=0
if [ -f "$local_overrides_file" ]; then
    while IFS= read -r line; do
        # Skip blank lines and comments
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        LOCAL_OVERRIDES+=("$line")
    done < "$local_overrides_file"
    loaded_local=1
fi

# The actual hostname of whatever machine runs this check, short and FQDN
# form. This is what makes the guard self-renewing: it protects against
# THIS host's real name leaking into the tree without anyone having to
# remember to add a new literal every time the dev machine changes (that's
# exactly how a maintainer's hostname leaked into 35 files the first time -- this makes
# the next one impossible). Skipped entirely if it resolves to a generic/
# placeholder-looking name (localhost, empty) so a throwaway CI runner's
# hostname doesn't produce noise.
_live_hostname="$(hostname -s 2>/dev/null || hostname 2>/dev/null || true)"
_live_fqdn="$(hostname -f 2>/dev/null || true)"
declare -a HOST_LITERALS=()
for h in "$_live_hostname" "$_live_fqdn"; do
    case "$h" in
        ""|localhost|localhost.localdomain) continue ;;
    esac
    HOST_LITERALS+=("$h")
done

# -- optional: derive the private-name list from THIS machine ---------------
# A hand-written list is exactly how a real client/repo name survives an
# audit: it ships until someone remembers to add it. This mode instead reads
# ground truth at scan time -- directory names under this machine's repo
# roots, repo values/labels from local beads data, and repo names from the
# GitHub org -- the same three sources a human sweep would use. Opt-in only
# (env var or flag): a third-party clone has no beads server and no GitHub
# auth, so every source here is skipped, not failed, when unavailable, and
# the shipped default (no flag/env, e.g. `make check`) never touches this
# block at all.
#
# Enable with:
#   CHECK_PUBLIC_GROUND_TRUTH=1 bash scripts/check-public.sh
#   bash scripts/check-public.sh --ground-truth
# Force a fresh derive (skip the cache): add CHECK_PUBLIC_REFRESH=1.
ground_truth_mode=0
for _arg in "$@"; do
    [ "$_arg" = "--ground-truth" ] && ground_truth_mode=1
done
[ "${CHECK_PUBLIC_GROUND_TRUTH:-0}" = "1" ] && ground_truth_mode=1

ground_truth_pattern=""
if [ "$ground_truth_mode" -eq 1 ]; then
    cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}/critboard-check-public"
    cache_file="$cache_dir/ground-truth.list"
    cache_ttl="${CHECK_PUBLIC_CACHE_TTL:-3600}"  # seconds

    use_cache=0
    if [ -f "$cache_file" ] && [ "${CHECK_PUBLIC_REFRESH:-0}" != "1" ]; then
        _mtime="$(stat -c %Y "$cache_file" 2>/dev/null || stat -f %m "$cache_file" 2>/dev/null || echo 0)"
        _age=$(( $(date +%s) - _mtime ))
        [ "$_age" -lt "$cache_ttl" ] && use_cache=1
    fi

    if [ "$use_cache" -eq 0 ]; then
        # Small, named stop-list: product vocabulary this project ships on
        # purpose (its own routing-label names) plus the one owner name
        # that has been explicitly reviewed and accepted (see report). Every
        # entry here is something a human reviewed and said is not private
        # -- not a token added because it happened to fail the scan.
        declare -a STOP_LIST=(
            dashboard fleet medium profile smoke handoff critboard
            __pycache__ readme.md
            needs-codex needs-grok needs-claude needs-opencode
            grok-review waiting-review
            bryan
        )

        declare -a repo_roots
        if [ -n "${CHECK_PUBLIC_REPO_ROOTS:-}" ]; then
            IFS=':' read -r -a repo_roots <<< "$CHECK_PUBLIC_REPO_ROOTS"
        else
            repo_roots=("$HOME/repos" "/srv/work/repos" "$HOME/work" "/srv/work/projects")
        fi

        raw_names_file="$(mktemp)"
        trap 'rm -f "$raw_names_file"' EXIT

        # Source 1: directory names under the configured repo roots.
        for root in "${repo_roots[@]+"${repo_roots[@]}"}"; do
            [ -d "$root" ] || continue
            ls -1 "$root" 2>/dev/null >> "$raw_names_file"
        done

        # Source 2: repo values + labels from local beads data. Skipped
        # entirely if `bd` isn't on PATH or the call fails for any reason --
        # beads is an optional workflow, per collectors/beads.py.
        if command -v bd >/dev/null 2>&1; then
            bd_env="$HOME/.config/beads/env"
            bd_json=""
            if [ -f "$bd_env" ]; then
                bd_json="$(. "$bd_env" 2>/dev/null && bd list --json --all --limit 0 2>/dev/null)" || bd_json=""
            else
                bd_json="$(bd list --json --all --limit 0 2>/dev/null)" || bd_json=""
            fi
            if [ -n "$bd_json" ]; then
                printf '%s' "$bd_json" | python3 -c '
import sys, json
try:
    items = json.load(sys.stdin)
except ValueError:
    items = []
if isinstance(items, dict):
    items = items.get("issues") or items.get("items") or []
for it in items or []:
    if not isinstance(it, dict):
        continue
    r = it.get("repo")
    if r:
        print(r)
    for l in (it.get("labels") or []):
        print(l)
' 2>/dev/null >> "$raw_names_file" || true
            fi
        fi

        # Source 3: repo names from the GitHub org. Skipped entirely if `gh`
        # isn't on PATH or isn't authenticated.
        if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
            gh_org="${CHECK_PUBLIC_GH_ORG:-critfusion}"
            gh repo list "$gh_org" --limit 200 --json name --jq '.[].name' 2>/dev/null >> "$raw_names_file" || true
        fi

        mkdir -p "$cache_dir" 2>/dev/null || true
        tr 'A-Z' 'a-z' < "$raw_names_file" \
            | sed 's/\.git$//' \
            | grep -vE '^$|^\.|^wt-|^repo$' \
            | awk 'length($0) >= 4' \
            | sort -u \
            > "${cache_file}.tmp" 2>/dev/null || : > "${cache_file}.tmp"

        if [ -s "${cache_file}.tmp" ]; then
            printf '%s\n' "${STOP_LIST[@]}" | tr 'A-Z' 'a-z' | sort -u > "${cache_file}.stop"
            comm -23 "${cache_file}.tmp" "${cache_file}.stop" > "$cache_file" 2>/dev/null \
                || mv "${cache_file}.tmp" "$cache_file"
            rm -f "${cache_file}.tmp" "${cache_file}.stop"
        else
            mv "${cache_file}.tmp" "$cache_file"
        fi
        rm -f "$raw_names_file"
        trap - EXIT
    fi

    declare -a GROUND_TRUTH=()
    if [ -f "$cache_file" ]; then
        # `read` loop instead of `mapfile` (bash 4+ only) -- see the FILES
        # read loop above for why. `|| [ -n "$_gt_line" ]` picks up a final
        # line even if the cache file has no trailing newline.
        while IFS= read -r _gt_line || [ -n "$_gt_line" ]; do
            [ -n "$_gt_line" ] && GROUND_TRUTH+=("$_gt_line")
        done < "$cache_file"
    fi

    if [ "${#GROUND_TRUTH[@]}" -gt 0 ]; then
        # One combined alternation per file beats one grep per derived name
        # per file -- this is what keeps a few hundred derived names cheap.
        declare -a _esc_terms=()
        for _t in "${GROUND_TRUTH[@]}"; do
            [ -n "$_t" ] || continue
            _esc_terms+=("$(printf '%s' "$_t" | sed -E 's/[.[\*^$+?(){}|\\]/\\&/g')")
        done
        ground_truth_pattern="$(IFS='|'; echo "${_esc_terms[*]}")"
    fi
fi

# config/layout.json (and its shipped config/layout.example.json) holds
# personal settings the settings panel writes: title and human_labels (the
# bead labels that mean "a person owns this", e.g. a maintainer's real
# first name). A literal-string grep can't catch an arbitrary owner's name
# -- that requires adding every name to scripts/check-public.local by hand,
# which is exactly the gap that let a real name ship in a tracked
# config/layout.json in the first place (it never showed up in that local
# override list). This check instead reads the JSON structure: ANY tracked
# layout config whose human_labels is non-empty, or whose title differs
# from the shipped default below, holds a personal value that must never be
# committed -- regardless of what the name or title actually is. The
# default is hardcoded here (not read back from layout.example.json) so a
# corrupted/personalized example can't validate itself.
_LAYOUT_DEFAULT_TITLE="CritBoard"
check_layout_json() { # file
    local file="$1"
    python3 - "$file" "$_LAYOUT_DEFAULT_TITLE" <<'PYEOF'
import json
import sys

path, default_title = sys.argv[1], sys.argv[2]
try:
    with open(path) as fh:
        doc = json.load(fh)
except (OSError, ValueError):
    sys.exit(0)
if not isinstance(doc, dict):
    sys.exit(0)

labels = doc.get("human_labels")
if isinstance(labels, list) and len(labels) > 0:
    print(f"human_labels|human_labels is non-empty ({labels!r}) in a tracked layout config -- a real name would ship publicly")

title = doc.get("title")
if isinstance(title, str) and title != default_title:
    print(f"title|title {title!r} differs from the shipped default {default_title!r} in a tracked layout config -- a personalized title would ship publicly")
PYEOF
}

for f in "${FILES[@]+"${FILES[@]}"}"; do
    [ -f "$f" ] || continue
    # This script names its own patterns in plain text -- never scan itself.
    [ "$f" = "scripts/check-public.sh" ] && continue

    case "$f" in
        config/layout.json|config/layout.example.json|*/config/layout.json|*/config/layout.example.json)
            while IFS='|' read -r _kind _reason; do
                [ -n "$_kind" ] || continue
                lineno="$(grep -n "\"$_kind\"" "$f" 2>/dev/null | head -n1 | cut -d: -f1)"
                report "$f" "${lineno:-1}" "$_reason"
            done < <(check_layout_json "$f")
            ;;
    esac
    # config/sources.json is gitignored and machine-local BY DESIGN (see
    # .gitignore and server/critdash/config.py) -- holding this machine's
    # real hosts/paths is its whole job, never scanned here. It may still
    # show up in `git ls-files` until it's untracked with `git rm --cached`;
    # this exclusion makes that transition harmless either way.
    [ "$f" = "config/sources.json" ] && continue
    # Same for check-public.local -- .gitignored, machine-local, holds
    # machine-specific patterns that should never be scanned.
    [ "$f" = "scripts/check-public.local" ] && continue

    # Process shipped generic patterns (currently empty, but slot is reserved
    # for generic rules that make sense to ship publicly)
    for entry in "${LITERALS[@]+"${LITERALS[@]}"}"; do
        pattern="${entry%%|*}"
        reason="${entry#*|}"
        # The public GitHub repo slug critfusion/critboard is allowed (it's a public identifier, not a leak)
        if [ "$pattern" = "critfusion" ]; then
            grep_pattern "$f" "$pattern" "$reason" -i --allow "critfusion/critboard"
        else
            grep_pattern "$f" "$pattern" "$reason" -i
        fi
    done

    # Process machine-local overrides (loaded from scripts/check-public.local)
    for entry in "${LOCAL_OVERRIDES[@]+"${LOCAL_OVERRIDES[@]}"}"; do
        pattern="${entry%%|*}"
        reason="${entry#*|}"
        # The public GitHub repo slug critfusion/critboard is allowed (it's a public identifier, not a leak)
        if [ "$pattern" = "critfusion" ]; then
            grep_pattern "$f" "$pattern" "$reason" -i --allow "critfusion/critboard"
        else
            grep_pattern "$f" "$pattern" "$reason" -i
        fi
    done

    for h in "${HOST_LITERALS[@]+"${HOST_LITERALS[@]}"}"; do
        [ -n "$h" ] || continue
        grep_pattern "$f" "$(printf '%s' "$h" | sed -E 's/[.[\*^$]/\\&/g')" \
            "this machine's real hostname ($h) -- see the dynamic HOST_LITERALS check" -i
    done

    if [ "$ground_truth_mode" -eq 1 ] && [ -n "$ground_truth_pattern" ]; then
        grep_pattern "$f" "$ground_truth_pattern" \
            "matches a name derived from this machine's repo roots/beads/GitHub org (--ground-truth mode) -- confirm it is not a real private name, or extend the stop-list if it's generic" -i
    fi

    # a real user's home dir, e.g. /home/alice -- but not the /home/user or
    # /home/user2 placeholders used throughout the sanitized fixtures/docs.
    grep_pattern "$f" '/home/(?!user2?\b)[A-Za-z0-9_.-]+' \
        "real-looking /home/<user> path (use /home/user or /home/user2)" -P

    # private IPv4 ranges (RFC 1918). Fixtures/docs use hostnames or
    # 127.0.0.1/0.0.0.0 placeholders instead of a real LAN address, so
    # nothing needs to be excluded here.
    grep_pattern "$f" \
        '\b(10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3})\b' \
        "private IPv4 address" -P

    # tailnet / CGNAT range (RFC 6598)
    grep_pattern "$f" \
        '\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b' \
        "tailnet-range (RFC 6598) address" -P

    # common live-credential shapes
    grep_pattern "$f" 'sk-[A-Za-z0-9]{16,}' "OpenAI-style API key (sk-...)"
    grep_pattern "$f" 'ghp_[A-Za-z0-9]{20,}' "GitHub personal access token (ghp_...)"
    grep_pattern "$f" 'AKIA[0-9A-Z]{16}' "AWS access key id (AKIA...)"
    grep_pattern "$f" 'BEGIN (OPENSSH|RSA|EC|DSA) PRIVATE KEY' "private key material"
done

echo
if [ "$found" -ne 0 ]; then
    echo "check-public.sh: FOUND personal/infrastructure data in the tree (see above)." >&2
    exit 1
fi
local_msg=""
if [ "$loaded_local" -eq 1 ]; then
    local_msg=" (+ $(printf '%s\n' "${LOCAL_OVERRIDES[@]+"${LOCAL_OVERRIDES[@]}"}" | wc -l) machine-local overrides)"
else
    local_msg=" (no local overrides file)"
fi
gt_msg=" (ground-truth mode off)"
if [ "$ground_truth_mode" -eq 1 ]; then
    gt_msg=" (ground-truth mode on, ${#GROUND_TRUTH[@]} derived names)"
fi
echo "check-public.sh: clean -- no personal/infrastructure data found in $(printf '%s\n' "${FILES[@]+"${FILES[@]}"}" | wc -l) files (tracked + untracked)$local_msg$gt_msg."
exit 0
