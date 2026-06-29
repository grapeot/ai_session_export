## Changelog

### 2026-06-29

- Project scaffolded from the earlier `contexts/ai_sessions` prototype and promoted into a standalone, installable package under `adhoc_jobs/ai_session_export/`.
- Added the Google Antigravity source adapter (`src/ai_session_export/sources/antigravity.py`), registered in `sources/__init__.py`, `cli.py`, and `state.py` (`DEFAULT_STATE`).
- Antigravity adapter parses `transcript_full.jsonl`, strips the `<USER_REQUEST>` XML wrapper, keeps only `USER_INPUT`/`USER_EXPLICIT` and `PLANNER_RESPONSE`/`MODEL` steps, and drops `CONVERSATION_HISTORY`, `CODE_ACTION`, tool calls, and thinking.
- Added `--antigravity-dir` CLI flag and the `antigravity.last_timestamp` incremental cursor.
- Wrote four source-adapter tests plus a shared fixture builder for the Antigravity transcript shape; full non-live suite is 14 tests passing.
- Added `docs/` with `prd.md`, `rfc.md`, `test.md`, and this file.

## Lessons Learned

- **Only `transcript_full.jsonl` is readable.** Antigravity session directories contain several artefacts, including `.pb` files that are binary protobuf with no published schema. Reverse-engineering them is not worth it: the JSONL transcript under `.system_generated/logs/` already contains the full readable dialogue, so it is the only file the adapter needs to touch.
- **The `.system_generated/logs/` directory is created by a recent Antigravity upgrade.** Older sessions on disk were captured before that directory existed, so they have no `transcript_full.jsonl` and are silently skipped by `_iter_transcript_files`. When a user reports "my old Antigravity sessions are missing," the cause is the absence of this directory, not a parsing bug.
- **User intent is wrapped in XML, not bare text.** The `content` of a `USER_INPUT` step is a concatenation of `<USER_REQUEST>...</USER_REQUEST>` and `<ADDITIONAL_METADATA>...</ADDITIONAL_METADATA>` blocks. Exporting the raw content would leak IDE state (active document paths, cursor position, etc.) into the archive, so the adapter must extract only the inner `USER_REQUEST` text.
- **Planner responses are narrative, not tool calls.** A single model turn may carry both a `PLANNER_RESPONSE` step (narrated text, worth keeping) and a `CODE_ACTION` step (the applied edit, not worth keeping) at adjacent `step_index` values. Treating them as separate step types — rather than collapsing them — keeps the archive readable.
- **Second Mind cannot be cursor'd by timestamp.** Its export JSON does not expose a reliable per-conversation timestamp, so the incremental cursor is a plain count of conversations already seen. This is fragile if the export file is regenerated in a different order; `--full` is the escape hatch.
