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
# This is the thing that stops a sanitized leak from coming back: run it
# before every commit that touches fixtures, docs, or config defaults.

set -uo pipefail

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
mapfile -t FILES < <(git ls-files --cached --others --exclude-standard -z | tr '\0' '\n')

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
        for allow_str in "${allow_strings[@]}"; do
            if [[ "$rest" == *"$allow_str"* ]]; then
                continue 2  # Continue outer while loop
            fi
        done

        [ -n "$lineno" ] && report "$file" "$lineno" "$reason"
    done < <(grep -n "${extra[@]}" "$engine" "$pattern" -- "$file" 2>/dev/null)
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

for f in "${FILES[@]}"; do
    [ -f "$f" ] || continue
    # This script names its own patterns in plain text -- never scan itself.
    [ "$f" = "scripts/check-public.sh" ] && continue
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
    for entry in "${LITERALS[@]}"; do
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
    for entry in "${LOCAL_OVERRIDES[@]}"; do
        pattern="${entry%%|*}"
        reason="${entry#*|}"
        # The public GitHub repo slug critfusion/critboard is allowed (it's a public identifier, not a leak)
        if [ "$pattern" = "critfusion" ]; then
            grep_pattern "$f" "$pattern" "$reason" -i --allow "critfusion/critboard"
        else
            grep_pattern "$f" "$pattern" "$reason" -i
        fi
    done

    for h in "${HOST_LITERALS[@]-}"; do
        [ -n "$h" ] || continue
        grep_pattern "$f" "$(printf '%s' "$h" | sed -E 's/[.[\*^$]/\\&/g')" \
            "this machine's real hostname ($h) -- see the dynamic HOST_LITERALS check" -i
    done

    # a real user's home dir, e.g. /home/alice -- but not the /home/user or
    # /home/user2 placeholders used throughout the sanitized fixtures/docs.
    grep_pattern "$f" '/home/(?!user2?\b)[A-Za-z0-9_.-]+' \
        "real-looking /home/<user> path (use /home/user or /home/user2)" -P

    # private IPv4 ranges (RFC 1918), excluding the two fictional example
    # addresses the sanitized fixtures deliberately use in place of the real
    # LAN/tailnet IPs that used to be there (10.0.0.5, 100.64.0.5 below).
    grep_pattern "$f" \
        '\b(10\.(?!0\.0\.5\b)[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3})\b' \
        "private IPv4 address" -P

    # tailnet / CGNAT range 100.64.0.0/10, excluding the fictional example above
    grep_pattern "$f" \
        '\b100\.(?!64\.0\.5\b)(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b' \
        "tailnet-range (100.64.0.0/10) address" -P

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
    local_msg=" (+ $(printf '%s\n' "${LOCAL_OVERRIDES[@]}" | wc -l) machine-local overrides)"
else
    local_msg=" (no local overrides file)"
fi
echo "check-public.sh: clean -- no personal/infrastructure data found in $(printf '%s\n' "${FILES[@]}" | wc -l) files (tracked + untracked)$local_msg."
exit 0
