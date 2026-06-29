# AI Session Export

Export AI coding session transcripts from multiple tools into a unified Markdown archive for browsing, search, and downstream workflows.

## Supported Sources

| Source | Data Location |
|---|---|
| OpenCode | `~/.local/share/opencode/opencode.db` |
| Claude Code | `~/.claude/projects/**/*.jsonl` |
| Google Antigravity | `~/.gemini/antigravity-ide/brain/*/.system_generated/logs/transcript_full.jsonl` |
| Second Mind | `second_mind_export.json` |

## Quick Start

```bash
# Install
uv pip install -e '.[dev]'

# Export all sources (incremental)
python export_sessions.py

# Export specific source
python export_sessions.py --source antigravity

# Full re-export (ignore incremental state)
python export_sessions.py --full

# Export only recent sessions
python export_sessions.py --since-date 2026-06-01

# Dry run
python export_sessions.py --dry-run
```

## Output Format

Each session is exported as a Markdown file with YAML frontmatter:

```markdown
---
source: antigravity
session_id: "8a425409-..."
title: "Fix the bug in auth.py"
date: "2026-06-29"
message_count: 3
---
# Fix the bug in auth.py

## User [16:38]

Fix the bug in auth.py

## Assistant [16:38]

I'll look at the auth.py file first.

## Assistant [16:39]

The bug is on line 42.
```

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
