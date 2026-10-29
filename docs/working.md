## Changelog

### 2026-10-29

- Fixed the OpenCode adapter freezing continued sessions. Incremental selection keyed on session creation time ("created since the last run"), so a session that kept growing after its one-and-only export never re-entered selection: the archive permanently lagged the transcript while looking complete downstream.
- The cursor is now a per-session watermark (`opencode.sessions[session_id] = {"watermark": ms, "output_file": name}`). A run renders sessions whose latest message/part activity time exceeds the session's watermark, so late-arriving parts on old messages also re-enter selection, and no session's state can be pushed past another session's unexported rows.
- The watermark advances only from content actually rendered this round: settled exports may advance up to their message+part activity maximum (converged late-part sessions stop re-rendering), while skipped-title, user-turn-less, `--since-date`-excluded and shrink-guard-skipped sessions stay retryable with their watermark untouched. An unsettled export (a user row without text, or a message row with no part rows) keeps the watermark below the pending timestamp so the run after the text commits re-renders it; assistant/tool rows carrying non-text parts are a normal final state.
- Legacy states are migrated once: the interim global message-time cursor and the older creation-time shape are each adopted as every existing entry's watermark, then the legacy key is removed (`last_message_time` popped; `DEFAULT_STATE` intentionally keeps only the legacy creation-time key so legacy-vs-migrated states stay distinguishable).
- Re-emitting an already-archived session updates the file it already owns instead of creating an `_2` duplicate: `session_aware_output_path()` + `archived_session_id()` in `utils.py` match candidates by frontmatter `session_id` (head slice treated as still-frontmatter when the close marker lies outside it, because per-turn lists grow the frontmatter); `claim_archive_file()` re-adopts a renamed session's existing file (no rename, no second copy) — same-day files first, full directory only when the numbering chain is exhausted. Byte-identical re-renders skip the write entirely.
- The CLI summary line reports `overwritten=` and `skipped_shrunk=` for the OpenCode source; dry-run writes nothing (no markdown, no mapping adoption, no renames).
- Tests: continued-session overwrite, per-session watermark incremental (including interim-state migration), same-title collision separation and stable re-emission numbering, shrink guard kept-and-retryable, frontmatter parsing past a fixed window, foreign-file termination, dry-run writing nothing, `--since-date`/partial-message/part-growth/streaming-assistant cursor cases (each with a cross-session burn variant where applicable), entry-backed title adoption, and a CLI summary round. Full suite: 191 passed, 6 skipped (live).

### 2026-10-28

- Added native Antigravity metadata parsing, session inspection (`--antigravity-list`, `--antigravity-parse`), and archive/unarchive lifecycle commands (`--antigravity-archive`, `--antigravity-unarchive`) in `src/ai_session_export/sources/antigravity.py` and `cli.py`.
- When `conversation_summaries.db` or `annotations/*.pbtxt` exists in the surface root (`brain/..`), `export_antigravity` now enriches exported sessions with their native Antigravity title and `project_directory` (falling back to the first user message line when absent).
- Implemented stdlib-only wire-format protobuf and `.pbtxt` helpers for `CascadeTrajectorySummary` and `ConversationAnnotations`, updating and verifying `annotations/<session_id>.pbtxt`, `conversation_summaries.db` (`conversations_fts` header row `chunk_index = -1` and `conversation_summaries.raw_summary`), and `agyhub_summaries_proto.pb` on archive/unarchive.
- Added synthetic unit and CLI tests in `tests/test_antigravity_lifecycle.py` and updated `skill.md`, `README.md`, `docs/rfc.md`, and `docs/test.md`.

### 2026-10-19

- Fixed session dates being filed one day ahead when a UTC timestamp falls after the local evening boundary. Per-turn headers render local `HH:MM`, but Antigravity, Claude Code, Codex, Gemini, and Grok derived the frontmatter `date` from the timestamp's own timezone (usually UTC). An Antigravity turn at `2026-10-19T00:14:11Z` is `2026-10-18 17:14 PDT`; the archive said `2026-10-19`, so eink_diary pulled yesterday's work into today's 16:00–18:00 window. Date derivation now uses local time across all adapters: aware timestamps go through `to_local_date()`, while OpenCode, Cursor, Cursor CLI, and DSH already used local `ms_to_date()`.
- Added `resolve_output_path()` and wired it into Antigravity, Claude Code, Codex, Gemini, and Grok, so a re-export renames an existing archive whose filename date prefix no longer matches the corrected frontmatter date.
- Historical archives do not self-heal: incremental state skips unchanged transcripts. After deploying this fix, run each affected source once with `--full` (or the whole archive once with `--full`). Added the same caveat to `README.md` and `skill.md`.
- Added regressions for local-date conversion, stale filename rename, and an evening-UTC Antigravity transcript under `TZ=America/Los_Angeles`. The test suite now pins `TZ=UTC` via `tests/conftest.py` so it is host-independent.

### 2026-10-17

- Added the Cursor CLI (`cursor agent`) source adapter (`src/ai_session_export/sources/cursor_cli.py`), registered in `sources/__init__.py`, `cli.py` (`--source cursor-cli`, `--cursor-cli-dir`, included in `all`), and `state.py` (`DEFAULT_STATE["cursor_cli"]`). It is a separate source from the Cursor IDE adapter: the storage formats share nothing and the session id namespaces are disjoint. Frontmatter, result dict, state key, and output directory use `cursor_cli`, matching the repo's underscore convention for everything except CLI choices. Added `cursor_cli/` to `.gitignore`.
- Sessions live in `~/.cursor/chats/<md5(cwd)>/<session-uuid>/` as a content-addressed `store.db` (`meta` and `blobs` tables) plus an optional `meta.json`. The adapter reads `meta['0']` (hex-encoded JSON), resolves `latestRootBlobId`, and walks the root protobuf with a small stdlib wire-format decoder that collects only the field-1 message refs. Turns come from those refs in order, keeping repeats. Human turns keep only `<user_query>` text (plus the `<timestamp>` as the turn time) and assistant turns keep only `text` parts.
- Compaction stitching: when the latest root contains a summary blob, the adapter collects every conversation root (first ref is a `system` blob), keeps the maximal ones, orders their chains by smallest rowid, and concatenates them while dropping summary blobs and refs shared with earlier snapshots. If the latest root's chain does not sort last, it exports the latest root alone and reports a warning without counting a failure.
- Per-session incremental state records `latest_timestamp`, `output_file`, and `root_blob_id`. The root id is content-addressed, so it changes exactly when the conversation changes, and it covers sessions whose missing `meta.json` leaves no live-updated timestamp. Per-session failures are isolated, reported as `{"session_id", "error"}` warnings, and retried on the next run.
- Added `tests/test_cursor_cli.py` with synthetic stores built in `tmp_path` (hand-built root protobufs, fake ids, paths, and model slugs). It covers the protobuf walker, timestamp parsing, root order versus scrambled rowids, orphans, repeated refs, compaction stitching and its fallback, rewinds, injected-content drops, missing `meta.json`, subagent and schema-version skips, empty sessions, corrupt-root isolation and repair, incremental and dry-run behavior, `since_date`, both open paths, model attribution, CLI wiring, and a live e2e test. Extended the all-source integration test with a Cursor CLI fixture. A content-free dry run against a real local store completed with no failures and no warnings, and every compacted session stitched (some regaining human turns that the latest root alone would lose).

- Claude Code truncation detection. Sessions driven through Claude Code Remote / Remote Control can have their local transcript rewritten to start at a later compaction boundary; the exporter used to archive the remaining half silently. The adapter now checks three local signals: `parent_uuid_missing`, `starts_with_continuation_summary`, and `subagent_activity_before_transcript` (sibling `subagents/*.jsonl` files start earlier, read prefix-only). Affected archives gain optional `transcript_truncated`, `truncation_signals`, and `earliest_known_activity` frontmatter keys, and the CLI prints one stderr warning per affected session pointing to the recovery section in `skill.md`. Output for sessions without signals is byte-identical.
- Cloud backfill merge. `--claude-backfill-dir` (default `<base-dir>/claude_code_backfill/`) holds `<local_session_id>.jsonl` files of messages an agent recovered from the cloud transcript. Messages earlier than the first local message are sorted, de-duplicated, and prepended; overlapping ones are ignored. `date` moves to the earliest message and `backfilled_messages: N` is added. Per-session state records `backfill_mtime_ns`, so a changed backfill file re-exports the session even when the local transcript is unchanged.
- Documented the agent-side recovery procedure (Claude Code Remote MCP `get_session` / `list_sessions` / `list_events` paging) and the `--since-date` + `--full` overwrite caveat in `skill.md` and `README.md`. Added synthetic tests in `tests/test_claude_truncation.py`. A dry run against a real local archive flagged well under 1% of sessions, so the signals are not noisy.

### 2026-09-27

- DSH discovery now accepts immutable generations `session.vN.jsonl` and `session.vN.jsonl.zstd` in addition to legacy `session.jsonl` / `session.jsonl.zstd`. Each session directory exports only its highest generation, so a migrated predecessor cannot win the session-id dedupe and hide later turns. Two encodings of that same version resolve by latest mtime, then filename. Non-canonical names (`.v0`, leading zeros, backups, `session.lock`) are ignored. The V4 dialogue fields the parser already reads (`user/message` text, assembled `assistant/message` text, `source.provider` / `source.model`) are unchanged, so no parser edit was required.
- V4 `user/message` events are not all human input. When `data.source` is a dict, the parser keeps only `source.kind == "user"`. `agent-instructions`, `runtime-context`, `user-approval`, and `model-selection` are dropped even when their text is nonempty and does not match the older reminder or runtime-context prefix filters. Events that omit `source` stay accepted, so legacy V0 user messages are not dropped. Synthetic coverage checks mixed kinds, human title fallback, `turn_models` alignment, and parse count.

### 2026-09-26

- Second Mind is now opt-in: the default `all` run no longer touches `second_mind_export.json`, so a missing export file no longer aborts every other source with `FileNotFoundError`. `--source second-mind` remains available and still fails fast when its JSON is missing. The "all" integration test now seeds the Second Mind fixture and asserts it stays out; a new integration test locks in the explicit opt-in path.

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

- **A local transcript is not necessarily the whole session.** Claude Code Remote can rewrite the local jsonl to start at a compaction boundary, so a clean parse can still be half a conversation. The file itself carries the evidence (a dangling first `parentUuid`, a leading continuation summary), and sibling subagent transcripts keep timestamps from the lost range. An exporter should surface integrity signals rather than trust the file shape. Recovery needs the cloud transcript, so it belongs to an agent with network tools, and the exporter only merges the result from a local file.
- **`--full` is only as good as the current source.** A full re-export with `--since-date` overwrites archives from whatever is on disk now. If the source lost history, the archive copy that still had it is overwritten too. Merged backfill and integrity markers make that loss visible and recoverable.

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
- **V4 `user/message` is not human speech.** The same event type also carries `agent-instructions`, `runtime-context`, `user-approval`, and `model-selection`. Text anchors miss injections that are not wrapped in `<system-reminder>` and do not start with `Current runtime context.` When `data.source` is a dict, only `kind == "user"` is dialogue. Missing `source` must stay accepted: legacy V0 events have no source field, and treating absence as a rejection drops real user turns.
- **Per-message model beats session-sparse request context.** Real DSH logs carry one `request/header` per multi-turn session but tag every `assistant/message` with `source.{provider, model}`. Attributing from the request header alone leaves later user turns `null` in `turn_models`; the per-message source back-fills every turn.
- **A torn Zstandard frame still streams its complete prefix.** `zstd -d -c` exits nonzero on a truncated final frame after emitting the earlier frames' bytes. `check=True` turned the realistic crash state into a permanent export failure; tolerating nonzero-with-output preserves the durable prefix, and only zero-output corruption stays a retryable failure.
- **In a content-addressed store, `rowid` is not time.** Cursor CLI blobs use `id == sha256(data)` and replace-on-write semantics, so `rowid` reflects the last write, not creation. Identical content collapses to one row, and a whole-store rewrite reshuffles rowids. Ordering by `rowid` put replies before their questions in real sessions and resurrected rewound branches. The ordered field-1 ref list of the latest root snapshot is the only authoritative turn order.
- **Read-only SQLite and WAL do not mix by default.** Every Cursor CLI store is in WAL mode. Inside a sandbox, plain `mode=ro` failed on closed stores because a read-only connection cannot create `-shm`. Outside the sandbox it opened them but created `-wal`/`-shm` sidecar files in the source directory. `immutable=1` reads a closed store without touching it, but it ignores the WAL, so it misses the tables of a live store whose pages sit only in `-wal`. The rule is: `-wal` present means `mode=ro`, otherwise `mode=ro&immutable=1`.
- **The latest snapshot can be incomplete.** After a context compaction, the latest Cursor CLI root restarts at `[system, <user_info>, summary, retained tail]`, and the original request survives only in older root snapshots. Reading only the latest root silently drops human turns. Stitching the maximal conversation roots recovers them, and a strict ordering check with a latest-root fallback keeps a wrong stitch from becoming a silent reorder.
- **A suffix glob exports the stale generation.** `session.v4.jsonl.zstd` does not match `session.jsonl*`. Widening discovery is not enough: lexicographic order reads `session.jsonl` before `session.v4.jsonl`, and the session-id dedupe then drops the current generation. Select the highest version per directory. Use mtime only to split two encodings of that same version.
- **Antigravity archiving is a multi-store metadata flag, not a file move.** Unlike Codex (which moves rollouts into `archived_sessions/`), Antigravity keeps `brain/<session_id>/.system_generated/logs/transcript_full.jsonl` in place and records `archived` in `annotations/<session_id>.pbtxt`, `conversation_summaries.db` (`conversations_fts` where `chunk_index = -1` and `conversation_summaries.raw_summary` field 15), and `agyhub_summaries_proto.pb`. Content chunks (`chunk_index >= 0`) in `conversations_fts` always keep `archived = 0`, so querying or updating `conversations_fts` must filter on `chunk_index = -1`.
