# dashboard — agent pointer

Project root: `~/work/dashboard` (CritBoard, :9999).

Fleet CoS peers (`codex` / `claude` / `grok`) are reachable via beads labels
(`needs-codex`, `needs-grok`, `needs-claude`, `grok-review`). Do not ask the
human to relay. Protocol: `~/team-build/rules/02-beads-handoff.md`.

`. ~/.config/beads/env` then `export BEADS_ACTOR=<your localhost-* actor>`.
