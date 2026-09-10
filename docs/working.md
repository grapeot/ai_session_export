## Changelog

### 2026-09-10

- Fixed #7: Claude Code now tracks timestamps and output filenames per session, rewrites resumed sessions in place, and adopts existing archives by frontmatter identity when upgrading legacy state. Full exports preserve output identity and filters; dry-runs leave files and caller state untouched. Added synthetic regressions for growth, migration, prior duplicates, collisions, missing outputs, full exports, and dry-runs.

### 2026-09-08 (maintainer review round 2)

- Stale-archive retirement is now provable-rewind-only: Gemini retires when the JSON snapshot's messages were emptied or the JSONL replay ends in a rewind with no surviving messages; Grok retires when a `rewind_marker` trail leaves no surviving prompt runs. Noise-title rewrites, subagent kinds, and other parser rejections no longer delete archives, and `--since-date` scopes retirement by session start time.
- Gemini migration comparison now replays the JSONL (direct messages, same-id replacements, `$set.messages` checkpoints, `$rewindTo`) instead of counting raw lines, so interrupted migrations and duplicate-id replay both pick the more complete source.
- Per-file failure isolation widened to `except Exception` (aligning with DSH): malformed JSON floats (e.g. `1e309` timestamps) can raise `OverflowError`, which previously aborted the whole run before `save_state`.
- Grok hidden user echoes now advance the prompt-model state before being dropped, so a runtime wake on a new model attributes the following assistant turn correctly; `rewind_marker` resets both the stitching key and the model-attribution state.
- Added regression tests: noise-title rename survival, since-date retirement scoping, checkpoint/duplicate-id migration, hidden-prompt model attribution, rewind stitching boundary, `OverflowError` isolation.

### 2026-09-08 (maintainer review pass)

- Gemini adapter drops machine-injected user content (`<session_context>`, `<hook_context>`, slash/help commands), mirroring gemini-cli's `isIgnoredUserContent`; hook output and environment context no longer enter the archive as user turns.
- Gemini adapter falls back to raw `content` whenever `displayContent` yields empty text, matching the UI's `displayContentString || contentString` semantics.
- Gemini JSON→JSONL migration: a same-name JSONL now only supersedes the legacy JSON once its replayed message count catches up; mid-migration sessions export from the JSON instead of silently truncating.
- Grok adapter hides model-only user echoes by prompt-id prefix (`task-completed-`, `subagent-completed-`, `workflow-completed-`, `notifications-`, `goal-summary-`, `goal-classifier-nudge-`) and by legacy bare auto-wake text (`<system-reminder>`, `<monitor-event>`, monitor-drain heads), matching the upstream scrollback policy.
- Grok adapter treats any `session_kind` starting with `subagent` (including `subagent_resume`) as hidden, matching upstream `Summary::is_hidden()` prefix semantics.
- Grok adapter attributes per-turn models from chunk `_meta.modelId` (user turn + preceding unmatched turns), keeping `turn_models` populated across mid-session model switches; the summary's `current_model_id` is merged into `models_used` as session inventory only.
- Both adapters retire a previously exported session's archive file (and state entry) when a rewind empties the live conversation, so dead branches do not linger after the provider deleted them; dry-run reports the retirement without deleting.
- Both adapters report per-file failure diagnostics (`path` + exception type/message) as `warnings`, aligned with the DSH adapter's diagnosability; the CLI prints them to stderr.
- Registered both sources in the public `skill.md` data-location table.

### 2026-09-08

- Support for `gemini` source adapter (`--source gemini`, `--gemini-dir`) matching public structures in `google-gemini/gemini-cli` (reading JSON/JSONL, replaying checkpoints/rewinds, filtering subagents/tools/thoughts).
- Support for `grok` source adapter (`--source grok`, `--grok-sessions-dir`) matching public structures in `xai-org/grok-build` (parsing `updates.jsonl` and `summary.json`, stitching streaming text, obeying rewinds, omitting thoughts/hidden host prompts).
- Common stability features: stable output paths, dry-run safety, zero-byte writes for unchanged files, and counted/retryable malformed sessions.
- Improves Grok Build rewind fidelity and introduces isolated failure handling and retry after repair for malformed sessions.

### 2026-08-14

- Added the DeepSeek Harness source adapter (`src/ai_session_export/sources/dsh.py`), registered in `sources/__init__.py`, `cli.py`, and `state.py` (`DEFAULT_STATE`).
- DSH sessions live under `~/.dsh/sessions/<workspace-slug>/<session-id>/` as one append-only event log per session, stored as checksummed Zstandard frames (`session.jsonl.zstd`) or plain JSONL. Decompression shells out to the `zstd` binary, keeping the package stdlib-only.
- Discovery globs `*/*/session.jsonl*`: session directory ids are not a single namespace (top-level sessions use `session-<uuid>`, subagent children use bare uuids), so filtering on a name prefix silently loses sessions.
- The adapter keeps `user/message` and `assistant/message` events (final assembled turns; streaming `assistant/chunk` and packed chunk rows are never read), takes titles from the last `session/title` event (LLM-generated titles supersede the fallback), and attributes models per message from `assistant/message` `source` (`provider/model`), with `request/header` as fallback and back-fill for the user turns that triggered each response.
- Dropped subagent-child sessions (header `origin: "subagent"`) and stripped `<system-reminder>` instruction injections from user messages — DSH delivers workspace instructions as user-role messages, and reminder-only messages vanish entirely once stripped. DSH's rendered runtime-context snapshot (`Current runtime context.` prefix, from `renderContextSnapshot` in dsh-system-prompt) arrives as a plain user message with no reminder wrapper and is dropped by prefix match.
- Torn trailing records (interrupted durable batches) are discarded per DSH's own crash-recovery semantics; this includes a truncated final Zstandard frame, which still yields the complete earlier frames on stdout — the adapter treats a nonzero `zstd` exit with partial output as a torn tail, not a failure, and only a fully unusable artifact becomes an isolated retryable failure with a CLI warning.
- Reused the Codex per-session incremental pattern (`latest_timestamp` + `output_file` + `source_mtime_ns`) instead of a global cursor: DSH session files are mutable live logs, and per-session output identity rewrites one stable Markdown file as a session grows instead of minting `_2.md` duplicates. Sessions without any timestamp are skipped (they cannot be placed in the archive), and a session directory holding both physical encodings exports once.
- Added `--dsh-sessions-dir` CLI flag, thirteen synthetic tests (fixture export, growing-session rewrite, subagent skip, reminder drop, compressed path, corrupted-file isolation, bare-uuid discovery, torn compressed tail, source-only model attribution, since-date filter, missing header/header-only skip, both-encodings dedupe, zero-timestamp skip), all-source integration coverage, and a live e2e test. Live run against real data: 7 scanned, 5 exported (one empty session and one subagent child in a bare-uuid directory correctly skipped), full `turn_models` attribution, incremental re-run exported 0.

### 2026-08-12

- Added the Cursor source adapter (`src/ai_session_export/sources/cursor.py`), registered in `sources/__init__.py`, `cli.py`, and `state.py` (`DEFAULT_STATE`).
- Cursor reads the single `state.vscdb` SQLite database: it enumerates composers from `bubbleId:` key prefixes, pulls title/project directory from `composerHeaders`, orders messages by bubble `createdAt`, and keeps only user/assistant bubbles. `modelInfo.modelName` on user bubbles feeds `turn_models` and carries forward to assistant responses.
- Skipped sub-agent composers (`isSubagent = 1`) as agent-to-agent chatter, consistent with the existing noise-drop contract.
- Added `--cursor-db` CLI flag and per-session incremental state (`latest_timestamp` + `output_file`).
- Added synthetic adapter, sub-agent-skip, live, and all-source integration coverage.

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

- **Stable output identity needs a migration path.** Replacing a global cursor with an empty per-session map alone duplicates existing archives on upgrade. Adopt files by frontmatter session identity before allocating a filename; dates and sanitized titles are not ownership keys.

### Key Learnings: Stateful CLI Integration

1. **Replay-based Reconstruction**: Both Gemini CLI and Grok Build maintain history as sequential transition logs (events, replacements, and rewinds) rather than static snapshots. Reconstructing clean conversations requires sequentially replaying these mutations rather than simple log stitching.
2. **Omission of Auxiliary Tracks**: Users expect clean, readable Markdown. Internal cognitive tracks (thoughts, tools, hidden host system prompts, subagent sub-trees) must be systematically stripped during processing to preserve a pure User/Assistant dialogue.
3. **Idempotence and Stability**: By mapping variable states (such as rewinds or growing lists) to a stable output filepath, and verifying contents before writing, the exporter prevents redundant disk operations and file thrashing.

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

- **Cursor composer ids are not a single namespace.** The ids in `bubbleId:` keys and the ids in `composerHeaders` overlap but do not match exactly: some composers have bubbles but no header row, and some header rows have no bubbles. The bubbles are the authoritative record, so enumeration must start from the key prefixes and treat the header table as optional metadata.
- **DSH session directory ids are not a single namespace either.** Top-level sessions use `session-<uuid>` directories, but subagent children materialize under bare uuids. A discovery glob keyed on the `session-` prefix returned 6 of 7 real files with no warning — the same failure class as the Cursor bubble/header split, caught only by diffing directory listings against parsed headers.
- **DSH duplicates user speech through two channels.** `agent/inbox/spliced` mirrors user input around turn boundaries and `assistant/chunk`/packed chunk rows mirror the streaming transcript. Reading either duplicates the archive; only the canonical `user/message` and assembled `assistant/message` events carry the dialogue.
- **DSH injects workspace instructions as user messages.** The `<system-reminder>` wrapper arrives as a regular `user/message` event, so a naive export turns injected AGENTS.md content into phantom user turns. Stripping the wrapper before the empty-text check drops reminder-only messages entirely while preserving user text that merely sits beside a reminder. The runtime-context snapshot is a second injection class with no wrapper; its stable `Current runtime context.` prefix (generated by dsh-system-prompt) is the filter anchor.
- **Per-message model beats session-sparse request context.** Real DSH logs carry one `request/header` per multi-turn session but tag every `assistant/message` with `source.{provider, model}`. Attributing from the request header alone leaves later user turns `null` in `turn_models`; the per-message source back-fills every turn.
- **A torn Zstandard frame still streams its complete prefix.** `zstd -d -c` exits nonzero on a truncated final frame after emitting the earlier frames' bytes. `check=True` turned the realistic crash state into a permanent export failure; tolerating nonzero-with-output preserves the durable prefix, and only zero-output corruption stays a retryable failure.
