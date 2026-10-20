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
| Cursor CLI (`cursor agent`) | `~/.cursor/chats/*/*/store.db` and `meta.json` |
| DeepSeek Harness | `~/.dsh/sessions/*/*/session[.vN].jsonl[.zstd]` |
| Gemini CLI | `~/.gemini/tmp/*/chats/session-*.json` or `.jsonl` |
| Grok Build | `~/.grok/sessions/*/*/updates.jsonl` and `summary.json` |
| Second Mind | `second_mind_export.json` |

Second Mind is opt-in: it is excluded from the default `all` run and only
exports with an explicit `--source second-mind`.

## Quick Start

```bash
# Install
uv pip install -e '.[dev]'

# Export all default sources (incremental)
# Second Mind is opt-in and not part of "all".
python export_sessions.py

# Export specific source
python export_sessions.py --source antigravity
python export_sessions.py --source second-mind
python export_sessions.py --source codex
python export_sessions.py --source cursor
python export_sessions.py --source cursor-cli
python export_sessions.py --source dsh
python export_sessions.py --source gemini
python export_sessions.py --source grok

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

The DeepSeek Harness source reads the newest immutable generation in each
session directory (`session.jsonl` for version 0, `session.vN.jsonl` for later
generations; either may be plain JSONL or `*.zstd`). Older generations stay on
disk after a format migration and are not exported. Two encodings of the same
version resolve by latest mtime. Session directory ids are not a single
namespace, so discovery accepts any id shape. It keeps assembled
`assistant/message` events and human `user/message` events (not streaming
chunks). When `data.source` is an object, only `source.kind` of `user` is
kept; legacy events that omit `source` are still exported. Other kinds,
including `agent-instructions`, `runtime-context`, `user-approval`, and
`model-selection`, are dropped even when their text is nonempty. Titles come
from `session/title` events, and models are attributed per message from
`assistant/message` `source` (falling back to `request/header`), back-filling
the user turns that triggered each response.
It drops subagent-child sessions and `<system-reminder>` instruction
injections, tolerates torn trailing records (including a truncated final
Zstandard frame), and rewrites one stable file per growing live session.
Decompression shells out to the `zstd` binary.

## Recovering a truncated Claude Code Remote session

Claude Code sessions driven through Claude Code Remote or Remote Control can
have their local transcript rewritten so it starts at a later compaction
boundary; the earlier part survives only in the cloud transcript. The Claude
Code adapter detects three signals: the first record's `parentUuid` points
outside the file (`parent_uuid_missing`), the first user message is a
compaction summary (`starts_with_continuation_summary`), or the session's
`subagents/*.jsonl` files start earlier than the main transcript
(`subagent_activity_before_transcript`). Affected archives gain
`transcript_truncated: true`, `truncation_signals`, and, when known,
`earliest_known_activity`. The CLI prints one warning per affected session to
stderr.

To recover, an agent with the Claude Code Remote MCP tools finds the cloud
session id (`get_session` with no id describes the current session; otherwise
`list_sessions`). It then pages backwards with
`list_events(session_id=<cloud id>, before_id=<earliest local uuid>, kinds=["user"], limit=100)`,
continuing with `before_id=<first_id of the previous page>`. Pages are large,
cover only about 10-15 minutes of an active session, and are filtered after
being read, so parse them with a script. `kinds=["user"]` still includes tool
results: keep only human-typed text (string content or `type == "text"`
blocks) and drop tool results, image placeholders, task notifications, system
reminders, subagent hand-backs, skill bodies, and compaction summaries.
Write the recovered messages, one JSON object per line, to
`<backfill-dir>/<local_session_id>.jsonl`:

```json
{"created_at": "2026-06-27T08:30:00Z", "role": "user", "text": "..."}
```

The backfill dir defaults to `<base-dir>/claude_code_backfill/` and can be
overridden with `--claude-backfill-dir`. Re-run
`python export_sessions.py --source claude-code --full`. Messages earlier than
the first local message are sorted, de-duplicated, and prepended; overlapping
ones are ignored. `date` moves to the earliest message and
`backfilled_messages: N` is added. Incremental runs re-export a session when
its backfill file changes. See `skill.md` for the step-by-step procedure.

Note that `--since-date` combined with `--full` re-writes matching archives from
the transcript content currently on disk. When the session date changed, the
archive may be renamed to the corrected date prefix. Recover truncated sessions
before running it.

## Gemini CLI

Export sessions created by the Gemini CLI (`google-gemini/gemini-cli`).

- **CLI Options**: `--source gemini --gemini-dir <path>` (specifies the temporary project root containing recordings).
- **Implementation Details**:
  - Reads session JSON and JSONL records, utilizing `chatRecordingService.ts` and `chatRecordingTypes.ts` schema structures.
  - Uses `displayContent` when supplied.
  - Replays message replacements, metadata checkpoints, and rewind actions to reconstruct the final state.
  - Skips internal thoughts, tools, and subagents.
  - Fills the User model from the next response metadata.
  - Does not attempt to convert project hashes back to a local working directory (`cwd`).

## Grok Build

Export sessions created by the Grok Build platform (`xai-org/grok-build`).

- **CLI Options**: `--source grok --grok-sessions-dir <path>` (specifies the directory holding Grok sessions).
- **Implementation Details**:
  - Parses `updates.jsonl` and `summary.json` based on `session/storage/mod.rs` and `session/persistence.rs` format mappings.
  - Supports both timestamped method/params payloads and legacy event types.
  - Stitches streaming text chunks into unified messages.
  - Follows session rewinds correctly to maintain clean chronological outputs.
  - Omits thoughts, tool execution steps, hidden host prompts, and nested subagent sessions.
  - Uses the summary model as session-level inventory only; never assigns it to earlier turns.

## Cursor CLI

Export sessions created by the Cursor CLI agent (`cursor agent`). This is a
separate source from the Cursor IDE: the storage formats and session id
namespaces do not overlap. Archives use `source: cursor_cli` and the
`cursor_cli/` output directory.

- **CLI Options**: `--source cursor-cli --cursor-cli-dir <path>` (defaults to `~/.cursor/chats`). Included in `all`.
- **Implementation Details**:
  - Each session is `~/.cursor/chats/<md5(cwd)>/<session-uuid>/` with a content-addressed `store.db` (`blobs.id == sha256(data)`) and an optional `meta.json`.
  - Opens closed stores with `mode=ro&immutable=1` and live stores (a `store.db-wal` is present) with `mode=ro`, so the source directory is never modified.
  - Turn order comes from the latest root snapshot (repeated protobuf field 1 of `latestRootBlobId`), never from `rowid`. Repeated references render once per occurrence; blobs the live branch no longer references (rewinds) are ignored.
  - After a context compaction, the pre-compaction turns exist only in older root snapshots. They are stitched back in front of the latest root. If the snapshots cannot be ordered safely, the export falls back to the latest root and reports a warning.
  - Keeps only `<user_query>` text from human turns and `text` parts from assistant turns. Drops `<user_info>`, reminders, notifications, compaction summaries, system prompts, reasoning, tool calls, and tool results. Subagent sessions are skipped.
  - Takes models from the system prompt fingerprint, the date from `meta.json` `createdAtMs` (falling back to the store metadata), and the project directory from `meta.json` `cwd` or a `Workspace Path:` line whose md5 matches the workspace directory name.
  - Incremental state records the root blob id along with the update timestamp, because sessions without `meta.json` have no live-updated timestamp.


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
`date` is the first turn's calendar date in the exporter host's local timezone;
`[HH:MM]` turn headers use the same local clock.
Antigravity records additionally include `surface: "2"`, `surface: "ide"`, or
`surface: "cli"`. Claude Code records may include the optional
`transcript_truncated`, `truncation_signals`, `earliest_known_activity`, and
`backfilled_messages` keys described above; they are omitted otherwise.

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
