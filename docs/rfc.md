# Technical Architecture — AI Session Export

## Architecture

The project is organised in four layers, each with a single responsibility. Data flows strictly downward: a top-level entry point parses arguments, the package's adapters read source data into a shared record, and a shared renderer writes Markdown. Tests sit beside the package and exercise every layer except the thin shell wrapper.

| Layer | Location | Responsibility |
|---|---|---|
| 1. Shell entrypoints | `scripts/`, `export_sessions.py` | Thin wrappers that put `src/` on `sys.path` and call into the package. `export_sessions.py` is the canonical entrypoint; `scripts/` is reserved for future shell helpers. |
| 2. CLI wrapper | `src/ai_session_export/cli.py` | Argument parsing, source dispatch, state load/save, and result printing. Holds no source-specific logic. |
| 3. Source package | `src/ai_session_export/` with `sources/` sub-package | The reusable library: shared models, renderer, state, utils, and one adapter file per source. |
| 4. Tests | `tests/` | Unit tests, source-adapter tests, an integration test, and opt-in live end-to-end tests. |

The deliberate design rule is that no source-specific knowledge leaks upward. `cli.py` knows only that each source has an `export_*` function with a common shape; all parsing lives inside `sources/<name>.py`.

## Shared Record

Two `NamedTuple`s in `models.py` are the lingua franca every adapter must produce:

```python
class MessageTurn(NamedTuple):
    role: str            # "user" or "assistant"
    content: str
    time_created: int | None  # ms epoch of the turn, if known
    model: str | None    # target/responder model, if attributable

class SessionRecord(NamedTuple):
    source: str          # e.g. "antigravity"
    session_id: str
    title: str
    date: str            # "YYYY-MM-DD"
    messages: list[MessageTurn]
    project_directory: str = ""
    models_used: list[str] = []
    surface: str = ""      # optional product surface, currently Antigravity only
```

`NamedTuple` is chosen over `dataclass` for its immutability and trivial serialisability — a parsed session is a value object that should never be mutated between parsing and rendering. Adapters may replace an immutable turn while resolving delayed attribution, such as Claude's next assistant response or Codex's following turn context. Adapters are free to carry private intermediate types (for example `ParsedAntigravitySession` and `ParsedClaudeSession`) that bundle a `SessionRecord` together with a source-specific cursor field, but those intermediate types never cross into the renderer.

## Shared Renderer

`markdown.py::render_markdown(session: SessionRecord) -> str` is the single function that turns a `SessionRecord` into the final file bytes. Every adapter calls it; none writes Markdown directly. This guarantees the Markdown Output Contract (see `prd.md`) is enforced in exactly one place.

The renderer is a straight-line builder: it assembles the frontmatter lines (conditionally adding `surface`, `project_directory`, `models_used`, and `turn_models`), then iterates `messages` emitting `## User` / `## Assistant` headers with an optional `[HH:MM]` suffix derived from `time_created` via `utils.ms_to_hhmm`. `surface` is currently emitted only for Antigravity and distinguishes `"2"`, `"ide"`, and `"cli"` without changing the stable top-level `source: antigravity`. `turn_models` is a JSON array aligned one-to-one with those sections; unknown entries are `null`, and the field is omitted when every turn is unknown. These optional fields extend the contract without changing headings or message bodies, so older consumers can ignore them safely. All string frontmatter values pass through `utils.yaml_string`, which JSON-quotes them so YAML-special characters cannot break the block.

## Source Adapters

Each adapter follows the same contract: a function `export_<name>(output_dir, state, *, source-specific-kwargs...) -> dict` that returns at least `{"source": ..., "scanned": N, "exported": N}` (Second Mind returns `total` instead of `scanned`). Within that contract, each adapter is free to implement its own parsing.

| Adapter | Data Source | Parsing Approach |
|---|---|---|
| `second_mind.py` | A single JSON array export at `second_mind_export.json`. | `json.loads` the whole file; each element is one conversation with `title`, `created_at`, and a `messages` list of `{role, content}`. Drops any non-`user`/`assistant` role. Incremental cursor is the count of conversations already seen (`last_export_count`). |
| `opencode.py` | SQLite database at `~/.local/share/opencode/opencode.db`, opened read-only via the `file:...?mode=ro` URI. | Joins `session` -> `message` -> `part`. Text parts (`json_extract(data, '$.type') == 'text'`) are concatenated per message; each message's native model metadata is retained on its turn. Sessions with zero user turns are skipped. Noise titles (sub-agent chatter) are filtered via `utils.should_skip_session`. Cursor is `session.time_created` (`last_session_time`). |
| `claude_code.py` | JSONL session files under `~/.claude/projects/**/*.jsonl`, plus `history.jsonl` for human-readable titles. | Iterates session files (excluding anything under a `subagents/` path). Each line is one event; `user` and `assistant` events produce turns, `isSidechain` events are skipped. Assistant content is an array that may mix `text` and `tool_use` items — only `text` items survive. `tool_result` user messages produce empty text and are dropped. A following assistant model is assigned to pending user turns. Titles are chosen from the history file when a meaningful `display` exists, otherwise from the first user message. Cursor is the max event timestamp (`last_timestamp`). |
| `codex.py` | Rollout JSONL under `~/.codex/sessions/` and `~/.codex/archived_sessions/`, plus `session_index.jsonl` for titles. | Keeps only `event_msg.user_message` and `event_msg.agent_message`. It drops developer instructions, reasoning, tool calls/results, token accounting, and world state. `session_meta` supplies id/cwd and `turn_context` supplies the current model, including delayed backfill when context follows a user event. Per-session state updates one stable Markdown file as an active rollout grows; unchanged source mtimes skip reparsing. |
| `cursor.py` | A single SQLite database at `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`. | See the dedicated section below. Composer enumeration is derived from `bubbleId:` keys; `composerHeaders` supplies metadata. |
| `antigravity.py` | JSONL transcripts under the Antigravity 2.0, IDE, and CLI brain roots. | One JSON object per line per "step". See the dedicated section below. Incremental state is isolated by surface and session, using source mtime/size fingerprints and stable output filenames. |

## Cursor Adapter Design

Cursor keeps its chat history in a single SQLite database, `state.vscdb`, rather than per-session files. Three tables are relevant:

| Table | Role |
|---|---|
| `composerHeaders` | One row per composer (session). `composerId` is the primary key; `value` is a JSON blob carrying `name` (title), `workspaceIdentifier.uri.fsPath` (project directory), and `createdAt`/`lastUpdatedAt`. `isSubagent` marks sub-agent composers. |
| `cursorDiskKV` | A key/value store. Message content lives under keys shaped `bubbleId:<composerId>:<bubbleId>`; each value is one bubble (message). |
| `ItemTable` | VSCode-style UI state; irrelevant to transcripts. |

A bubble's JSON carries `type` (1 = user, 2 = assistant), `text` (plain body), `richText` (a Lexical editor tree used as a fallback when `text` is empty), `modelInfo.modelName` (the responding model, recorded on the user bubble), and `createdAt` (ISO timestamp used for ordering). Assistant bubbles may also carry `thinking` and `codeBlocks`, which the adapter drops like other sources drop reasoning and tool payloads.

Two structural quirks drive the adapter design:

1. **Composer id namespaces do not fully overlap.** The `composerId` values that appear inside `bubbleId:` keys and the ones listed in `composerHeaders` are largely the same but not identical. Some composers have bubbles but no header row (old or deleted sessions), and some header rows have no bubbles (empty sessions). The adapter therefore enumerates sessions from `bubbleId:` key prefixes — the messages are the authoritative record — and uses `composerHeaders` only to supply title and project directory, falling back to the first user message when no header exists.

2. **Model attribution sits on the user bubble.** Cursor records the responding model as `modelInfo.modelName` on the user bubble (the model selected to answer that turn) and leaves most assistant bubbles unannotated. The adapter therefore mirrors Claude Code: it captures the model from a user turn and carries it forward to that turn's assistant responses, so `turn_models` stays aligned without fabricating attribution on assistant-only sequences.

The adapter opens the database read-only via `file:...?mode=ro`. It enumerates composer ids with a grouped `substr` over `bubbleId:` keys while computing each composer's max `createdAt` in the same query. Incremental state is per-session (like Codex): each composer id stores `latest_timestamp` and `output_file`, where `latest_timestamp` is the maximum of the header's `lastUpdatedAt` and the composer's max bubble `createdAt`. Unchanged composers whose output file already exists are skipped. Sub-agent composers (`isSubagent = 1`) are skipped entirely because they contain agent-to-agent chatter rather than user dialogue.

## Antigravity Adapter Design

Antigravity currently exposes three independently installed product surfaces with separate local namespaces:

| Surface | Default brain root | Product boundary |
|---|---|---|
| `2` | `~/.gemini/antigravity/brain/` | Antigravity 2.0 standalone desktop command center. This namespace was also used by the pre-2.0 integrated IDE, so old files may have legacy provenance. |
| `ide` | `~/.gemini/antigravity-ide/brain/` | Standalone Antigravity IDE after the product split. |
| `cli` | `~/.gemini/antigravity-cli/brain/` | Independent terminal client launched with `agy`. |

Each subdirectory is one session, named by an opaque id, and contains a `.system_generated/logs/` directory whose `transcript_full.jsonl` is the only file with human-readable dialogue. Other artefacts in a session directory (`.pb` protobuf files, snapshots, etc.) are binary and have no published schema; the adapter ignores them. The three surfaces share one output directory and `source: antigravity`, while the optional `surface` frontmatter field preserves their product identity.

The transcript is JSONL, one object per "step". Each step has the keys `step_index`, `source`, `type`, `status`, `created_at` (ISO 8601), and optionally `content` and `tool_calls`. The adapter keeps only two step kinds and drops everything else:

| Step `type` | Step `source` | Kept as | Notes |
|---|---|---|---|
| `USER_INPUT` | `USER_EXPLICIT` | `user` turn | Other `USER_INPUT` sources (e.g. resumed context) are ignored. |
| `PLANNER_RESPONSE` | `MODEL` | `assistant` turn | The model's narrated plan text. |
| `CONVERSATION_HISTORY` | any | dropped | Re-injected context, not original dialogue. |
| `CODE_ACTION` | any | dropped | The applied edit payload, not narrative. |
| any other | any | dropped | Tool execution, thinking, status, etc. |

Within a kept `USER_INPUT` step, the `content` field is wrapped in XML-like tags. A typical payload looks like:

```
<USER_REQUEST>
Fix the bug in auth.py
</USER_REQUEST>
<ADDITIONAL_METADATA>
Active Document: /home/example/projects/demo/auth.py
</ADDITIONAL_METADATA>
```

The adapter runs a `re.DOTALL` capture of `<USER_REQUEST>(.*?)</USER_REQUEST>`, joins multiple matches with blank lines, and discards everything else (including the `<ADDITIONAL_METADATA>` block). If no `<USER_REQUEST>` tag is present, the raw stripped content is used as a fallback. This guarantees the exported user turn contains only what the human actually typed, not the surrounding IDE context.

Other adapter decisions:

- The session title is the first line of the first kept user message, truncated to 120 characters.
- `models_used` is always an empty list for Antigravity — model identity is not surfaced in the transcript steps.
- The session `date` is the calendar date of the earliest step that has a parseable `created_at`.
- A session with no kept messages, a zero `latest_timestamp_ms`, or no `started_at` is skipped entirely.
- Every JSONL line must decode to a JSON object with string fields where required. Syntax, shape, and field-type errors are isolated to that session and returned as privacy-safe structured warnings containing only the surface, line number, and error summary.
- A malformed session is recorded with `status: failed`, is retried on the next run, and never blocks valid sessions from other surfaces. The CLI reports the failure count and exits non-zero after successful sessions and state have been written.

## Incremental Export

A single JSON state file (default `.export_state.json` next to the export root) tracks how far each source has been consumed. Its shape is:

```json
{
  "second_mind": {"last_export_count": 12},
  "opencode": {"last_session_time": 1719648000000},
  "claude_code": {"last_timestamp": 1719648000000},
  "antigravity": {
    "last_timestamp": 1719648000000,
    "legacy_cursor_migrated": true,
    "surfaces": {
      "ide": {
        "sessions": {
          "example-id": {
            "status": "complete",
            "latest_timestamp": 1719648000000,
            "output_file": "20260629_example.md",
            "source_mtime_ns": 123,
            "source_size": 456
          }
        }
      }
    }
  },
  "codex": {"sessions": {"example-id": {"latest_timestamp": 1719648000000, "output_file": "20260629_example.md", "source_mtime_ns": 123}}},
  "cursor": {"sessions": {"example-id": {"latest_timestamp": 1719648000000, "output_file": "20260629_example.md"}}}
}
```

Each adapter carries its own cursor semantics because the sources expose time differently:

- **Second Mind** has no per-conversation timestamp exposed reliably, so the cursor is a count of conversations already exported; on each run it exports only the conversations beyond that count.
- **OpenCode** uses `session.time_created` (ms epoch) and re-queries rows with `time_created > last_session_time`.
- **Claude Code** reduces each transcript to a single `latest_timestamp_ms` and compares it with a source-level cursor.
- **Antigravity** keeps independent per-session state inside each product surface. Unchanged mtime/size fingerprints skip reparsing, changed sessions rewrite their prior output file, failed sessions remain retryable, and identical session ids on different surfaces do not collide in state.
- **Codex** uses per-session state because active rollout files keep growing and archived sessions can move between directories. The adapter rewrites the same output file when a session changes and skips unchanged files by source mtime.
- **Cursor** uses per-session state because composer bubbles grow in place and the composer id namespace does not fully overlap the header table. The adapter rewrites the same output file when a session's max timestamp advances and skips unchanged composers whose output already exists.

The deprecated `antigravity.last_timestamp` is retained only as a one-time migration input for existing installations. Because the old adapter scanned only the IDE, that cursor is applied only to the `ide` surface. Migration completes only on an unfiltered run that scanned at least one IDE transcript without parse failures; 2.0 and CLI sessions are never suppressed by the legacy cursor.

Two correctness properties are enforced uniformly:

1. `--dry-run` runs the full scan and returns accurate counts but writes no files and does not persist state.
2. `--full` ignores the cursor and re-exports everything, while still advancing the cursor forward (it never moves the cursor backward).

`state.py` deep-copies `DEFAULT_STATE` on load and `setdefault`s per-source defaults on top of any persisted file, so a missing or partially-populated state file degrades gracefully to fresh cursors rather than crashing.

## CLI Design

`cli.py` exposes `run_export(source, *, full, dry_run, base_dir, state_file, source-specific paths, since_date)` as the programmatic entrypoint, and `main()` as the argparse entrypoint. The split makes the integration test trivial: it calls `run_export` directly with a temp `base_dir` and temp source paths, avoiding any filesystem assumptions.

Flags:

| Flag | Purpose |
|---|---|
| `--source {all,second-mind,opencode,claude-code,antigravity,codex,cursor}` | Select one source or all. |
| `--full` | Ignore cursors; export everything. |
| `--dry-run` | Scan and report without writing or persisting state. |
| `--since-date YYYY-MM-DD` | Drop sessions whose date is before the given day. |
| `--base-dir` | Override the export root (default: `~/.local/share/ai-session-export`). |
| `--state-file` | Override the state cursor file. |
| `--second-mind-json`, `--opencode-db`, `--antigravity-dir`, `--codex-dir`, `--codex-session-index`, `--cursor-db` | Override each source's input location. `--antigravity-dir` preserves the original single-root interface and treats that root as an IDE-compatible surface; omitting it scans all three defaults. |

After each adapter runs, `main()` prints one summary line per source (`exported=N scanned=N`, or `exported=N total=N` for Second Mind). Antigravity adds `failed=N` when any session could not be parsed, then exits non-zero after persisting successful work so cron can distinguish partial success from a clean run.

## Test Strategy

The suite is split into four tiers, ordered from fastest/most-isolated to slowest/most-coupled. All four tiers share synthetic fixture builders so no test ever touches real user data.

1. **Unit tests** — pure functions with no I/O (`sanitize_filename`, `should_skip_session`, `render_markdown`, `yaml_string`, `unique_output_path`, `load_state`/`save_state`).
2. **Source-adapter tests** — each adapter exercised against a synthetic fixture built in `tmp_path` (a hand-written JSONL/JSON file or a seeded SQLite database).
3. **Integration test** — a single `run_export("all")` call that wires all six adapters into temp paths and asserts state is persisted with refreshed cursors and Antigravity, Codex, and Cursor per-session status.
4. **Live end-to-end tests** — opt-in via `AI_SESSION_EXPORT_LIVE=1`, run against the real local data on the developer's machine with a 7-day `--since-date` window. Skipped automatically in CI.

See `docs/test.md` for the full per-test breakdown.
