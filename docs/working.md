## Changelog

### 2026-07-31

- Expanded the single Antigravity adapter to scan Antigravity 2.0, Antigravity IDE, and Antigravity CLI while preserving the stable `source: antigravity` contract and adding optional `surface` provenance.
- Replaced the Antigravity global timestamp cursor with per-surface, per-session source fingerprints, stable output filenames, and parse status. The legacy cursor migrates only the IDE records it historically covered.
- Isolated malformed JSON and wrong-shape JSON to the affected session, retained retryable failure state, surfaced partial failures to the CLI, and kept successful sessions exportable in the same run.
- Treated invalid JSON field types as retryable session failures and kept CLI diagnostics free of session identifiers and local transcript paths.
- Added synthetic coverage for all three surfaces, identical cross-surface session ids, incremental rewrites, malformed-session repair, legacy state migration, mutable-default isolation, and dry-run state immutability.

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

- Project scaffolded from an earlier prototype and promoted into a standalone, installable package.
- Added the Google Antigravity source adapter (`src/ai_session_export/sources/antigravity.py`), registered in `sources/__init__.py`, `cli.py`, and `state.py` (`DEFAULT_STATE`).
- Antigravity adapter parses `transcript_full.jsonl`, strips the `<USER_REQUEST>` XML wrapper, keeps only `USER_INPUT`/`USER_EXPLICIT` and `PLANNER_RESPONSE`/`MODEL` steps, and drops `CONVERSATION_HISTORY`, `CODE_ACTION`, tool calls, and thinking.
- Added `--antigravity-dir` CLI flag and the `antigravity.last_timestamp` incremental cursor.
- Wrote four source-adapter tests plus a shared fixture builder for the Antigravity transcript shape; full non-live suite is 14 tests passing.
- Added `docs/` with `prd.md`, `rfc.md`, `test.md`, and this file.

## Lessons Learned

- **A product family is not one incremental domain.** Antigravity 2.0, IDE, and CLI use related transcript formats but write independently. A shared maximum timestamp can suppress unseen sessions from another surface; state must be scoped by surface and session.
- **A parse failure is state, not just an exception.** Continuing past one bad transcript is necessary, but marking a partial session complete would make the data loss permanent. Failed fingerprints stay retryable and make cron report partial success explicitly.
- **Legacy cursors encode historical scope.** The old Antigravity cursor represented only the IDE root, so applying it to newly discovered 2.0 or CLI roots would silently discard their history.

- **Session-level model inventories cannot recover turn attribution.** Downstream analytics need an index-aligned `turn_models` contract; `models_used` remains descriptive metadata only.
- **Codex records the same conversation through multiple event channels.** `response_item` mirrors narrative and tool traffic, while `event_msg` provides clean `user_message` and `agent_message` events. Reading both duplicates the transcript; the adapter treats `event_msg` as canonical.
- **Codex rollouts are mutable session files.** A global timestamp cursor creates duplicate `_2.md` files when an active session grows. Per-session output identity is required for incremental correctness.

- **Only `transcript_full.jsonl` is readable.** Antigravity session directories contain several artefacts, including `.pb` files that are binary protobuf with no published schema. Reverse-engineering them is not worth it: the JSONL transcript under `.system_generated/logs/` already contains the full readable dialogue, so it is the only file the adapter needs to touch.
- **The `.system_generated/logs/` directory is created by a recent Antigravity upgrade.** Older sessions on disk were captured before that directory existed, so they have no `transcript_full.jsonl` and are silently skipped by `_iter_transcript_files`. When a user reports "my old Antigravity sessions are missing," the cause is the absence of this directory, not a parsing bug.
- **User intent is wrapped in XML, not bare text.** The `content` of a `USER_INPUT` step is a concatenation of `<USER_REQUEST>...</USER_REQUEST>` and `<ADDITIONAL_METADATA>...</ADDITIONAL_METADATA>` blocks. Exporting the raw content would leak IDE state (active document paths, cursor position, etc.) into the archive, so the adapter must extract only the inner `USER_REQUEST` text.
- **Planner responses are narrative, not tool calls.** A single model turn may carry both a `PLANNER_RESPONSE` step (narrated text, worth keeping) and a `CODE_ACTION` step (the applied edit, not worth keeping) at adjacent `step_index` values. Treating them as separate step types — rather than collapsing them — keeps the archive readable.
- **Second Mind cannot be cursor'd by timestamp.** Its export JSON does not expose a reliable per-conversation timestamp, so the incremental cursor is a plain count of conversations already seen. This is fragile if the export file is regenerated in a different order; `--full` is the escape hatch.
