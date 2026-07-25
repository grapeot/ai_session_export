# Test Plan — AI Session Export

All tests live in `tests/test_export.py` and are run with `python -m pytest tests/ -v` from the project root. The suite is split into four tiers. Every fixture is synthetic — no test reads or writes real user data, and all paths, session ids, and titles in fixtures are fabricated (for example `antigravity-session-fixture`, `/home/example/projects/demo`).

## 1. Unit Tests

Pure-logic tests with no filesystem or database I/O beyond `tmp_path`. These run on every push and in CI.

| Test | What it covers |
|---|---|
| `test_sanitize_filename` | Non-alphanumeric runs collapse to `_`; CJK characters are preserved; consecutive separators merge; empty input becomes `untitled`; output is capped at 80 characters. |
| `test_should_skip_session` | Sub-agent and system-search noise titles (`@explore subagent`, `Search ... subagent`, `Find ... subagent`) are skipped; normal titles are kept. |
| `test_render_markdown_second_mind_shape` | Frontmatter ordering, JSON-quoted string values, `message_count`, CJK title in both frontmatter and `H1`, and the `## User` / `## Assistant` body for a session with no `project_directory`, `models_used`, or `turn_models`. |
| `test_render_markdown_opencode_shape` | `project_directory`, `models_used`, and aligned `turn_models` frontmatter lines are emitted when populated; body shape for a session that has all three. |
| `test_render_markdown_turn_models_preserves_null_alignment` | Mixed known/unknown turn attribution renders a position-preserving JSON array with `null`. |
| `test_render_markdown_with_timestamps` | The `[HH:MM]` suffix appears on `## User` / `## Assistant` headers when `time_created` is set, and is absent when it is not; the frontmatter `date` is independent of per-turn times. |
| `test_state_load_save_roundtrip` | A missing state file yields `DEFAULT_STATE`; a mutated state round-trips through `save_state` / `load_state`; per-source defaults for untouched sources are preserved on reload; extra top-level keys survive. |
| `test_state_defaults` | A fresh state (loaded from a non-existent path) has zero-valued cursors for every source. |
| `test_unique_output_path` | The first call yields `YYYYMMDD_title.md`; a second collision yields a `_2` suffix; a third yields `_3`. |
| `test_yaml_string` | Plain strings, embedded double quotes, CJK, and YAML-significant characters (`a: b`) are all JSON-quoted so they stay valid inside the frontmatter block. |

## 2. Source Adapter Tests

Each adapter is exercised against a synthetic fixture built inside `tmp_path`. These tests verify the parsing contract of one source in isolation.

| Test | Fixture | What it covers |
|---|---|---|
| `test_second_mind_export_with_fixture` | A one-conversation JSON array with a `system`-role message mixed in. | Full export writes one file; `total` and `exported` are correct; `system`-role messages are dropped; user/assistant text appears in the output. |
| `test_opencode_export_with_fixture` | A seeded SQLite database with one session, one user turn and one assistant turn, and native model ids on both messages. | `exported == 1`; per-turn `[HH:MM]` headers are correct; `project_directory`, `models_used`, and `turn_models` appear in frontmatter; user and assistant text survive. |
| `test_claude_code_export_with_fixture` | A synthetic `projects/` tree with one `.jsonl` session file plus a `history.jsonl` providing a human title. The session includes two model phases, mixed assistant content, and a `tool_result` user message. | The history title is chosen over the raw first-user-text fallback; the `tool_result` user message is dropped; assistant text survives; each user turn receives its following assistant model without attribution bleeding across the model transition. |
| `test_claude_missing_assistant_model_does_not_leak_later_model` | Two Claude turns where the first assistant omits model metadata and the second supplies it. | The first user/assistant pair remains `null`; the later model is attributed only to its own pair. |
| `test_antigravity_export_with_fixture` | A synthetic brain directory with one session whose `transcript_full.jsonl` contains `USER_INPUT`, `CONVERSATION_HISTORY`, two `PLANNER_RESPONSE`, and a `CODE_ACTION` step. | Frontmatter `source`/`session_id`/`date`/`message_count` are correct; title is derived from the inner `<USER_REQUEST>` text; XML wrappers (`<USER_REQUEST>`, `<ADDITIONAL_METADATA>`) are stripped; only one `## User` and two `## Assistant` sections appear; tool-call names (`view_file`) are absent. |
| `test_codex_export_with_fixture_and_incremental_update` | A synthetic rollout plus `session_index.jsonl`, including user/agent messages, reasoning, tool output, system metadata, and a model switch before a follow-up turn. | Only user/agent narrative survives; title/cwd/per-turn model metadata are preserved across the switch; unchanged reruns export zero files; appending turns updates the original Markdown file instead of creating a suffix duplicate. |
| `test_codex_model_context_after_user_backfills_turn` | A synthetic rollout where `turn_context` follows the user event. | Delayed context backfills the pending user turn and applies to its assistant response. |

## 3. Integration Test

| Test | What it covers |
|---|---|
| `test_cli_run_export_all_sources` (marked `integration`) | Calls `run_export("all")` with all five synthetic fixtures wired into `tmp_path`, then asserts every source appears, every source writes Markdown, and all persisted cursors including Codex per-session state are refreshed. |

This is the only test that exercises `cli.py`'s dispatch and state-persistence logic end to end; it is the regression guard for the "adding a new source" checklist in `AGENTS.md`.

## 4. Live End-to-End Tests

These tests run against the developer's real local data and are **opt-in**. They are skipped unless `AI_SESSION_EXPORT_LIVE=1` is set, and they are skipped automatically in CI (no env var present). Each uses a 7-day `--since-date` window to keep runtime bounded and to avoid re-exporting the entire archive.

| Test | What it covers |
|---|---|
| `test_live_antigravity_export` | Skips if the default brain directory does not exist. Runs a `--dry-run` scan first (asserts no files are written), then a real export over the last 7 days. Asserts the number of written files equals `result["exported"]` and that a sample file carries `source: antigravity`. Skips gracefully if there are no recent sessions. |
| `test_live_opencode_export` | Skips if the default OpenCode database does not exist. Same dry-run-then-real pattern over the last 7 days. Asserts file count matches `exported` and that a sample carries `source: opencode`. |
| `test_live_codex_export` | Skips if no default Codex session directory exists. Exports only the last 7 days to `tmp_path`, checks file counts, and verifies `source: codex` without printing transcript content. |

To run the full suite including live tests on a populated machine:

```bash
AI_SESSION_EXPORT_LIVE=1 python -m pytest tests/ -v -m live_e2e
```
