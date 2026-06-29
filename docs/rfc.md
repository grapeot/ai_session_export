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

class SessionRecord(NamedTuple):
    source: str          # e.g. "antigravity"
    session_id: str
    title: str
    date: str            # "YYYY-MM-DD"
    messages: list[MessageTurn]
    project_directory: str = ""
    models_used: list[str] = []
```

`NamedTuple` is chosen over `dataclass` for its immutability and trivial serialisability — a parsed session is a value object that should never be mutated between parsing and rendering. Adapters are free to carry private intermediate types (for example `ParsedAntigravitySession` and `ParsedClaudeSession`) that bundle a `SessionRecord` together with a source-specific cursor field, but those intermediate types never cross into the renderer.

## Shared Renderer

`markdown.py::render_markdown(session: SessionRecord) -> str` is the single function that turns a `SessionRecord` into the final file bytes. Every adapter calls it; none writes Markdown directly. This guarantees the Markdown Output Contract (see `prd.md`) is enforced in exactly one place.

The renderer is a straight-line builder: it assembles the frontmatter lines (conditionally adding `project_directory` and `models_used`), then iterates `messages` emitting `## User` / `## Assistant` headers with an optional `[HH:MM]` suffix derived from `time_created` via `utils.ms_to_hhmm`. All string frontmatter values pass through `utils.yaml_string`, which JSON-quotes them so YAML-special characters cannot break the block.

## Source Adapters

Each adapter follows the same contract: a function `export_<name>(output_dir, state, *, source-specific-kwargs...) -> dict` that returns at least `{"source": ..., "scanned": N, "exported": N}` (Second Mind returns `total` instead of `scanned`). Within that contract, each adapter is free to implement its own parsing.

| Adapter | Data Source | Parsing Approach |
|---|---|---|
| `second_mind.py` | A single JSON array export at `second_mind_export.json`. | `json.loads` the whole file; each element is one conversation with `title`, `created_at`, and a `messages` list of `{role, content}`. Drops any non-`user`/`assistant` role. Incremental cursor is the count of conversations already seen (`last_export_count`). |
| `opencode.py` | SQLite database at `~/.local/share/opencode/opencode.db`, opened read-only via the `file:...?mode=ro` URI. | Joins `session` -> `message` -> `part`. Text parts (`json_extract(data, '$.type') == 'text'`) are concatenated per message; model ids are collected from assistant messages. Sessions with zero user turns are skipped. Noise titles (sub-agent chatter) are filtered via `utils.should_skip_session`. Cursor is `session.time_created` (`last_session_time`). |
| `claude_code.py` | JSONL session files under `~/.claude/projects/**/*.jsonl`, plus `history.jsonl` for human-readable titles. | Iterates session files (excluding anything under a `subagents/` path). Each line is one event; `user` and `assistant` events produce turns, `isSidechain` events are skipped. Assistant content is an array that may mix `text` and `tool_use` items — only `text` items survive. `tool_result` user messages produce empty text and are dropped. Titles are chosen from the history file when a meaningful `display` exists, otherwise from the first user message. Cursor is the max event timestamp (`last_timestamp`). |
| `antigravity.py` | JSONL transcripts at `~/.gemini/antigravity-ide/brain/*/.system_generated/logs/transcript_full.jsonl`. | One JSON object per line per "step". See the dedicated section below. Cursor is the max step timestamp (`last_timestamp`). |

## Antigravity Adapter Design

The Antigravity IDE stores its brain state under `~/.gemini/antigravity-ide/brain/`. Each subdirectory is one session, named by a UUID, and (in current versions) contains a `.system_generated/logs/` directory whose `transcript_full.jsonl` is the only file with human-readable text. Other artefacts in a session directory (`.pb` protobuf files, snapshots, etc.) are binary and have no published schema; the adapter ignores them.

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

## Incremental Export

A single JSON state file (default `.export_state.json` next to the export root) tracks how far each source has been consumed. Its shape is:

```json
{
  "second_mind": {"last_export_count": 12},
  "opencode": {"last_session_time": 1719648000000},
  "claude_code": {"last_timestamp": 1719648000000},
  "antigravity": {"last_timestamp": 1719648000000}
}
```

Each adapter carries its own cursor semantics because the sources expose time differently:

- **Second Mind** has no per-conversation timestamp exposed reliably, so the cursor is a count of conversations already exported; on each run it exports only the conversations beyond that count.
- **OpenCode** uses `session.time_created` (ms epoch) and re-queries rows with `time_created > last_session_time`.
- **Claude Code** and **Antigravity** both reduce the transcript to a single `latest_timestamp_ms` (max over all kept events/steps) and skip sessions whose latest timestamp is at or before the cursor.

Two correctness properties are enforced uniformly:

1. `--dry-run` runs the full scan and returns accurate counts but writes no files and does not persist state.
2. `--full` ignores the cursor and re-exports everything, while still advancing the cursor forward (it never moves the cursor backward).

`state.py` deep-copies `DEFAULT_STATE` on load and `setdefault`s per-source defaults on top of any persisted file, so a missing or partially-populated state file degrades gracefully to fresh cursors rather than crashing.

## CLI Design

`cli.py` exposes `run_export(source, *, full, dry_run, base_dir, state_file, source-specific paths, since_date)` as the programmatic entrypoint, and `main()` as the argparse entrypoint. The split makes the integration test trivial: it calls `run_export` directly with a temp `base_dir` and temp source paths, avoiding any filesystem assumptions.

Flags:

| Flag | Purpose |
|---|---|
| `--source {all,second-mind,opencode,claude-code,antigravity}` | Select one source or all. |
| `--full` | Ignore cursors; export everything. |
| `--dry-run` | Scan and report without writing or persisting state. |
| `--since-date YYYY-MM-DD` | Drop sessions whose date is before the given day. |
| `--base-dir` | Override the export root (default: project root). |
| `--state-file` | Override the state cursor file. |
| `--second-mind-json`, `--opencode-db`, `--antigravity-dir` | Override each source's input location. |

After each adapter runs, `main()` prints one summary line per source (`exported=N scanned=N`, or `exported=N total=N` for Second Mind).

## Test Strategy

The suite is split into four tiers, ordered from fastest/most-isolated to slowest/most-coupled. All four tiers share synthetic fixture builders so no test ever touches real user data.

1. **Unit tests** — pure functions with no I/O (`sanitize_filename`, `should_skip_session`, `render_markdown`, `yaml_string`, `unique_output_path`, `load_state`/`save_state`).
2. **Source-adapter tests** — each adapter exercised against a synthetic fixture built in `tmp_path` (a hand-written JSONL/JSON file or a seeded SQLite database).
3. **Integration test** — a single `run_export("all")` call that wires all four adapters into temp paths and asserts state is persisted with refreshed cursors.
4. **Live end-to-end tests** — opt-in via `AI_SESSION_EXPORT_LIVE=1`, run against the real local data on the developer's machine with a 7-day `--since-date` window. Skipped automatically in CI.

See `docs/test.md` for the full per-test breakdown.
