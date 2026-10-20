---
name: ai-session-export
description: >-
  Export AI coding session transcripts from OpenCode, Claude Code, Codex, Google
  Antigravity, Cursor, Cursor CLI, DeepSeek Harness, Gemini CLI, Grok Build, and Second Mind into a unified Markdown
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
# Export all default sources (incremental — only new sessions since last run)
# Second Mind is opt-in and not part of "all".
python export_sessions.py

# Export a specific source
python export_sessions.py --source antigravity
python export_sessions.py --source second-mind
python export_sessions.py --source codex
python export_sessions.py --source cursor
python export_sessions.py --source cursor-cli
python export_sessions.py --source dsh
python export_sessions.py --source gemini
python export_sessions.py --source grok

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
python export_sessions.py --cursor-cli-dir /path/to/.cursor/chats
python export_sessions.py --dsh-sessions-dir /path/to/.dsh/sessions
python export_sessions.py --gemini-dir /path/to/.gemini/tmp
python export_sessions.py --grok-sessions-dir /path/to/.grok/sessions

# Messages recovered from a Claude Code cloud transcript (see recovery section)
python export_sessions.py --source claude-code --claude-backfill-dir /path/to/backfill
```

The Antigravity source scans 2.0, IDE, and CLI by default. `--antigravity-dir`
retains the legacy single-root override and treats that root as IDE-compatible.

The Cursor CLI source (`--source cursor-cli`, frontmatter `source: cursor_cli`)
is separate from the Cursor IDE source and is part of `all`. It orders turns by
the latest root snapshot rather than SQLite `rowid`, and stitches in
pre-compaction turns from older snapshots.

The default private output root is `~/.local/share/ai-session-export/`. Override
it with `--base-dir` and keep real transcripts outside public repositories.

Caveat: `--since-date` combined with `--full` re-writes the archive file of every
matching session using only the transcript content currently on disk. If the
session date changed, the archive may be renamed to the corrected date prefix.
If a source file has since lost history (for example a truncated Claude Code
transcript, see below), the archive loses it too. Back up the archive or recover
the missing messages into a backfill file first.

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
`date` is the first turn's calendar date in the exporter host's local timezone;
`[HH:MM]` turn headers use the same local clock. `turn_models` is a JSON array
aligned one-to-one with the rendered turn sections; unknown entries are `null`.
Do not infer turn attribution from session-level `models_used`.
Antigravity emits `surface` as `"2"`, `"ide"`, or `"cli"`.

Claude Code may add optional transcript-integrity keys after `turn_models`:

```yaml
transcript_truncated: true
truncation_signals: ["parent_uuid_missing", "starts_with_continuation_summary"]
earliest_known_activity: "2026-06-27T08:30:00Z"
backfilled_messages: 12
```

`transcript_truncated` and `truncation_signals` appear only when the local
transcript looks like it starts mid-session. Signals:
`parent_uuid_missing` (the first record's `parentUuid` is not in the file),
`starts_with_continuation_summary` (the first user message is a compaction
summary), and `subagent_activity_before_transcript` (the session's
`subagents/*.jsonl` files start before the main transcript).
`earliest_known_activity` is the earliest subagent timestamp when that signal
fires. `backfilled_messages` counts messages merged from a backfill file.
Sessions without signals or backfill render exactly as before.

## Recovering a truncated Claude Code Remote session

Claude Code sessions driven through Claude Code Remote or Remote Control can
have their local `~/.claude/projects/<project>/<session_id>.jsonl` rewritten so
it starts at a later compaction boundary. Everything before that point exists
only in the cloud transcript. The exporter detects this, adds the
`transcript_truncated` keys above, and prints one stderr warning per affected
session:

```text
[claude_code] <session_id>: transcript truncated (local transcript starts at <ISO time>; signals: <signal, ...>); see skill.md "Recovering a truncated Claude Code Remote session"
```

An agent with the Claude Code Remote MCP tools can recover the missing messages:

1. **Get the cloud session id.** It differs from the local session id. From
   inside the affected session, `get_session` with no id describes the current
   session. Otherwise use `list_sessions` and match by title and time.
2. **Find the earliest local uuid.** It is the `uuid` of the first record in the
   local jsonl that has one.
3. **Page backwards through the cloud transcript.** Call
   `list_events(session_id=<cloud id>, before_id=<earliest local uuid>, kinds=["user"], limit=100)`,
   then repeat with `before_id=<first_id of the previous page>` until
   `has_more` is false or you reach the start you need. Each page can cover only
   about 10-15 minutes of an active session. The `kinds` filter runs after the
   page is read, so a page can return few or no events while `has_more` is
   still true; keep paging. `kinds=["user"]` still includes tool results.
4. **Parse with a script.** Results are large and are usually saved to files
   rather than shown inline. Keep only human-typed text: a string `content`, or
   blocks with `type == "text"`. Drop tool results, image placeholders, task
   notifications, system reminders, subagent hand-backs, skill bodies, and
   compaction summaries. Include assistant text too if you page with
   `kinds=["assistant"]`.
5. **Write the backfill file.** One JSON object per line, named by the LOCAL
   session id: `<backfill-dir>/<local_session_id>.jsonl`. The default
   backfill dir is `<base-dir>/claude_code_backfill/` (override with
   `--claude-backfill-dir`).

   ```json
   {"created_at": "2026-06-27T08:30:00Z", "role": "user", "text": "..."}
   ```

   `role` is `user` or `assistant` and defaults to `user`.
6. **Re-export.** Run `python export_sessions.py --source claude-code --full`.

On export, backfill messages earlier than the first local message are sorted,
de-duplicated, and prepended. Messages that overlap the local range are ignored
(local wins). The frontmatter `date` moves to the earliest message and
`backfilled_messages` records the count. Incremental runs re-export a session
whenever its backfill file changes, even if the local transcript did not.

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
| Cursor CLI | `~/.cursor/chats/*/*/store.db` + `meta.json` | SQLite (content-addressed blobs, protobuf root snapshots) + JSON |
| DeepSeek Harness | `~/.dsh/sessions/*/*/session[.vN].jsonl[.zstd]` | JSONL or Zstandard; select the newest generation per session directory (currently V4) |
| Gemini CLI | `~/.gemini/tmp/*/chats/session-*.json` or `.jsonl` | JSON / JSONL |
| Grok Build | `~/.grok/sessions/*/*/updates.jsonl` + `summary.json` | JSONL + JSON |
| Second Mind | `./second_mind_export.json` | JSON |

Second Mind is opt-in: it is excluded from the default `all` run and only
exports with an explicit `--source second-mind`.

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
