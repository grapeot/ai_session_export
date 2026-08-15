# AI Session Export

Export AI coding session transcripts from multiple tools into a unified Markdown archive for browsing, search, and downstream workflows.

## Supported Sources

| Source | Data Location |
|---|---|
| OpenCode | `~/.local/share/opencode/opencode.db` |
| Claude Code | `~/.claude/projects/**/*.jsonl` |
| Codex | `~/.codex/sessions/**/*.jsonl`, `~/.codex/archived_sessions/*.jsonl` |
| Google Antigravity 2.0 | `~/.gemini/antigravity/brain/*/.system_generated/logs/transcript_full.jsonl` |
| Google Antigravity IDE | `~/.gemini/antigravity-ide/brain/*/.system_generated/logs/transcript_full.jsonl` |
| Google Antigravity CLI | `~/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript_full.jsonl` |
| Cursor | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` |
| DeepSeek Harness | `~/.dsh/sessions/*/*/session.jsonl*` |
| Second Mind | `second_mind_export.json` |

## Quick Start

```bash
# Install
uv pip install -e '.[dev]'

# Export all sources (incremental)
python export_sessions.py

# Export specific source
python export_sessions.py --source antigravity
python export_sessions.py --source codex
python export_sessions.py --source cursor
python export_sessions.py --source dsh

# Full re-export (ignore incremental state)
python export_sessions.py --full

# Export only recent sessions
python export_sessions.py --since-date 2026-06-01

# Dry run
python export_sessions.py --dry-run
```

By default, output and incremental state are stored under
`~/.local/share/ai-session-export/`. Use `--base-dir` and `--state-file` to
target another private archive. Never write real session exports into a public
repository.

`--source antigravity` scans all three Antigravity surfaces by default. They share
the `antigravity/` output directory and `source: antigravity`, while optional
frontmatter `surface` identifies `"2"`, `"ide"`, or `"cli"`. The legacy
`--antigravity-dir /path/to/brain` override scans one IDE-compatible root.

The DeepSeek Harness source reads each session's append-only event log
(`session.jsonl.zstd`, or plain `session.jsonl` when compression is disabled;
session directory ids are not a single namespace, so discovery accepts any id
shape). It keeps `user/message` and `assistant/message` events (final assembled
turns, not streaming chunks), takes titles from `session/title` events, and
attributes models per message from `assistant/message` `source` (falling back
to `request/header`), back-filling the user turns that triggered each response.
It drops subagent-child sessions and `<system-reminder>` instruction
injections, tolerates torn trailing records (including a truncated final
Zstandard frame), and rewrites one stable file per growing live session.
Decompression shells out to the `zstd` binary.

## Output Format

Each session is exported as a Markdown file with YAML frontmatter:

```markdown
---
source: opencode
session_id: "ses-example"
title: "Fix the bug in auth.py"
date: "2026-06-29"
message_count: 3
turn_models: ["gpt-example", "gpt-example", "gpt-example"]
---
# Fix the bug in auth.py

## User [16:38]

Fix the bug in auth.py

## Assistant [16:38]

I'll look at the auth.py file first.

## Assistant [16:39]

The bug is on line 42.
```

When a source can attribute models per turn, `turn_models` is a JSON array aligned
one-to-one with the rendered `User` and `Assistant` sections. Unknown entries are
`null`; the field is omitted when every turn is unknown. `models_used` remains a
session-level inventory and must not be used to guess per-turn attribution.
Antigravity records additionally include `surface: "2"`, `surface: "ide"`, or
`surface: "cli"`.

## Installation as a Coding Agent Skill

This project is designed to be used as a skill by AI coding agents (Codex, Claude Code, Cursor, OpenCode, etc.).

1. Clone or download this repository.
2. Point your AI agent at `skill.md` in the project root — it contains the workflow instructions.
3. If your workspace has a skills index (e.g., `rules/skills/INDEX.md`), add an entry pointing to this project's `skill.md`.

## Testing

```bash
# Unit + integration tests
python -m pytest tests/ -v

# Live end-to-end tests (requires real local data)
AI_SESSION_EXPORT_LIVE=1 python -m pytest tests/ -v -m live_e2e
```

## License

MIT
