## Changelog

### 2026-07-25

- Extended the backward-compatible Markdown frontmatter contract with optional `turn_models`, aligned one-to-one with rendered dialogue sections and using `null` for unknown attribution.
- Preserved native OpenCode turn models, assigned Claude user turns from their next assistant response, and tracked Codex turn-context models including delayed context events. Antigravity and Second Mind remain unknown when their exports do not expose model identity.
- Added synthetic renderer and adapter coverage without introducing real transcript data into the public repository.

### 2026-07-15

- Added Codex rollout export from active and archived JSONL sessions, using `session_index.jsonl` for titles and the existing unified Markdown contract.
- Codex keeps only explicit user and agent narrative events; developer instructions, reasoning, tool traffic, token accounting, and world-state records are excluded.
- Added per-session incremental state so active rollouts update one stable Markdown file. Source mtimes avoid reparsing unchanged historical rollouts.
- State writes now use an atomic same-directory replacement, preventing a large Codex state map from being truncated if a process stops mid-write.
- Moved the default output root outside the public repository to `~/.local/share/ai-session-export/` and added gitignore defenses for every generated source directory and state file.
- Added synthetic parser, filtering, incremental-update, and all-source integration coverage.

### 2026-06-29

- Project scaffolded from the earlier `contexts/ai_sessions` prototype and promoted into a standalone, installable package under `adhoc_jobs/ai_session_export/`.
- Added the Google Antigravity source adapter (`src/ai_session_export/sources/antigravity.py`), registered in `sources/__init__.py`, `cli.py`, and `state.py` (`DEFAULT_STATE`).
- Antigravity adapter parses `transcript_full.jsonl`, strips the `<USER_REQUEST>` XML wrapper, keeps only `USER_INPUT`/`USER_EXPLICIT` and `PLANNER_RESPONSE`/`MODEL` steps, and drops `CONVERSATION_HISTORY`, `CODE_ACTION`, tool calls, and thinking.
- Added `--antigravity-dir` CLI flag and the `antigravity.last_timestamp` incremental cursor.
- Wrote four source-adapter tests plus a shared fixture builder for the Antigravity transcript shape; full non-live suite is 14 tests passing.
- Added `docs/` with `prd.md`, `rfc.md`, `test.md`, and this file.

## Lessons Learned

- **Session-level model inventories cannot recover turn attribution.** Downstream analytics need an index-aligned `turn_models` contract; `models_used` remains descriptive metadata only.
- **Codex records the same conversation through multiple event channels.** `response_item` mirrors narrative and tool traffic, while `event_msg` provides clean `user_message` and `agent_message` events. Reading both duplicates the transcript; the adapter treats `event_msg` as canonical.
- **Codex rollouts are mutable session files.** A global timestamp cursor creates duplicate `_2.md` files when an active session grows. Per-session output identity is required for incremental correctness.

- **Only `transcript_full.jsonl` is readable.** Antigravity session directories contain several artefacts, including `.pb` files that are binary protobuf with no published schema. Reverse-engineering them is not worth it: the JSONL transcript under `.system_generated/logs/` already contains the full readable dialogue, so it is the only file the adapter needs to touch.
- **The `.system_generated/logs/` directory is created by a recent Antigravity upgrade.** Older sessions on disk were captured before that directory existed, so they have no `transcript_full.jsonl` and are silently skipped by `_iter_transcript_files`. When a user reports "my old Antigravity sessions are missing," the cause is the absence of this directory, not a parsing bug.
- **User intent is wrapped in XML, not bare text.** The `content` of a `USER_INPUT` step is a concatenation of `<USER_REQUEST>...</USER_REQUEST>` and `<ADDITIONAL_METADATA>...</ADDITIONAL_METADATA>` blocks. Exporting the raw content would leak IDE state (active document paths, cursor position, etc.) into the archive, so the adapter must extract only the inner `USER_REQUEST` text.
- **Planner responses are narrative, not tool calls.** A single model turn may carry both a `PLANNER_RESPONSE` step (narrated text, worth keeping) and a `CODE_ACTION` step (the applied edit, not worth keeping) at adjacent `step_index` values. Treating them as separate step types — rather than collapsing them — keeps the archive readable.
- **Second Mind cannot be cursor'd by timestamp.** Its export JSON does not expose a reliable per-conversation timestamp, so the incremental cursor is a plain count of conversations already seen. This is fragile if the export file is regenerated in a different order; `--full` is the escape hatch.
