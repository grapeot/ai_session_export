# AI Session Export — Agent Guide

## What This Project Is

A public Python tool that exports AI coding session transcripts from multiple sources (OpenCode, Claude Code, Codex, Google Antigravity, Second Mind) into a unified Markdown archive format.

## Working Environment

- Python 3.11+
- Use `uv pip install` for dependencies
- Run tests from project root: `python -m pytest tests/ -v`
- Activate the workspace `.venv` if running within the workspace

## Project Structure

```
src/ai_session_export/     # reusable Python package
  sources/                 # one adapter per source
    second_mind.py
    opencode.py
    claude_code.py
    codex.py
    antigravity.py
scripts/                   # shell entrypoints
tests/                     # unit + integration + live e2e
docs/                      # PRD, RFC, working, test docs
export_sessions.py         # CLI entrypoint (thin wrapper)
skill.md                   # public skill file
```

## Rules

1. Update `docs/working.md` whenever you make meaningful changes.
2. Keep `docs/working.md` split into `## Changelog` and `## Lessons Learned`.
3. Prefer small, reviewable commits.
4. Preserve the exported Markdown contract (YAML frontmatter + alternating sections) unless docs explicitly approve a change.
5. Keep source-specific parsing inside adapters under `src/ai_session_export/sources/`.
6. This is a **public repo**. Never commit real emails, real file paths, API keys, or personal data. Use fake fixtures in tests.

## Adding a New Source

1. Create `src/ai_session_export/sources/<name>.py` with an `export_<name>()` function.
2. The function must return `{"source": "<name>", "scanned": N, "exported": N}` or similar.
3. Register it in `src/ai_session_export/sources/__init__.py` and `cli.py`.
4. Add its default state to `state.py` `DEFAULT_STATE`.
5. Write adapter unit tests with synthetic fixtures.
6. Update this README and `skill.md`.

## CI

GitHub Actions runs `python -m pytest tests/ -v` on every push and PR. Live tests are automatically skipped in CI (no `AI_SESSION_EXPORT_LIVE` env var set).
