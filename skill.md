---
name: ai-session-export
description: >-
  Export AI coding session transcripts from OpenCode, Claude Code, Codex, Google
  Antigravity, Cursor, DeepSeek Harness, and Second Mind into a unified Markdown
  archive. Run as a CLI or periodic cron job.
---

# AI Session Export Skill

Export session transcripts from multiple AI coding tools into one stable Markdown
archive for browsing, semantic search, and downstream workflows.

## When To Use

- Export or sync AI session history into Markdown files
- Backfill sessions from a specific date
- Integrate with a periodic/cron job for daily incremental export
- Add a new session source adapter

## Prerequisites

- Python 3.11+
- Dependencies: none beyond the standard library (sqlite3, json, pathlib)
- The DeepSeek Harness source needs the `zstd` binary on PATH for compressed session logs
- For tests: `pytest` (install via `uv pip install -e '.[dev]'`)

## Commands

All commands run from the project root.

```bash
# Export all sources (incremental — only new sessions since last run)
python export_sessions.py

# Export a specific source
python export_sessions.py --source antigravity
python export_sessions.py --source codex
python export_sessions.py --source cursor
python export_sessions.py --source dsh

# Full re-export (ignore incremental cursor)
python export_sessions.py --full

# Only sessions from a date onward
python export_sessions.py --since-date 2026-06-01

# Dry run (count without writing)
python export_sessions.py --dry-run

# Override data paths
python export_sessions.py --opencode-db /path/to/opencode.db
python export_sessions.py --antigravity-dir /path/to/brain
python export_sessions.py --codex-dir /path/to/codex/sessions
python export_sessions.py --cursor-db /path/to/state.vscdb
python export_sessions.py --dsh-sessions-dir /path/to/.dsh/sessions
```

The Antigravity source scans 2.0, IDE, and CLI by default. `--antigravity-dir`
retains the legacy single-root override and treats that root as IDE-compatible.

The default private output root is `~/.local/share/ai-session-export/`. Override
it with `--base-dir` and keep real transcripts outside public repositories.

## Output Contract

Each session is one Markdown file:

```markdown
---
source: opencode
session_id: "ses_abc123"
title: "Debug websocket reconnection"
date: "2026-06-29"
message_count: 2
project_directory: "/home/user/project"
models_used: ["claude-sonnet-4.6"]
turn_models: ["claude-sonnet-4.6", "claude-sonnet-4.6"]
---
# Debug websocket reconnection

## User [14:30]

Can you look at the websocket reconnection logic?

## Assistant [14:30]

I'll examine the reconnection handler...
```

Frontmatter fields: `source`, optional `surface`, `session_id`, `title`, `date`, `message_count`,
optional `project_directory`, optional `models_used`, and optional `turn_models`.
`turn_models` is a JSON array aligned one-to-one with the rendered turn sections;
unknown entries are `null`. Do not infer turn attribution from session-level
`models_used`.
Antigravity emits `surface` as `"2"`, `"ide"`, or `"cli"`.

## Source Data Locations

| Source | Default Path | Format |
|---|---|---|
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite |
| Claude Code | `~/.claude/projects/**/*.jsonl` | JSONL |
| Codex | `~/.codex/sessions/**/*.jsonl`, `~/.codex/archived_sessions/*.jsonl` | JSONL |
| Antigravity 2.0 | `~/.gemini/antigravity/brain/*/.system_generated/logs/transcript_full.jsonl` | JSONL |
| Antigravity IDE | `~/.gemini/antigravity-ide/brain/*/.system_generated/logs/transcript_full.jsonl` | JSONL |
| Antigravity CLI | `~/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript_full.jsonl` | JSONL |
| Cursor | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` | SQLite |
| DeepSeek Harness | `~/.dsh/sessions/*/*/session.jsonl*` | Zstandard-compressed JSONL |
| Second Mind | `./second_mind_export.json` | JSON |

## Adding a New Source

Create `src/ai_session_export/sources/<name>.py` with an `export_<name>()` function
following the existing adapter signature. Register it in `sources/__init__.py`,
`cli.py`, and `state.py`. Write adapter tests with synthetic fixtures.

## Live Tests

Live end-to-end tests are opt-in:

```bash
AI_SESSION_EXPORT_LIVE=1 python -m pytest tests/ -v -m live_e2e
```

They export 7 days of real local data to a temp directory. Never enabled in CI.

## Validation

```bash
python -m pytest tests/ -v
```
