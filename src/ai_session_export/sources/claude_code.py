from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import parse_iso_timestamp, should_skip_session, unique_output_path


DEFAULT_CLAUDE_PROJECT_DIRS = (
    Path.home() / ".claude" / "projects",
    Path.home() / ".config" / "claude" / "projects",
    Path.home() / ".config" / "claude-code" / "projects",
)
DEFAULT_CLAUDE_HISTORY_FILES = (
    Path.home() / ".claude" / "history.jsonl",
    Path.home() / ".config" / "claude" / "history.jsonl",
    Path.home() / ".config" / "claude-code" / "history.jsonl",
)

BORING_TITLE_PREFIXES = (
    "/resume",
    "/ide",
    "/model",
    "/mcp",
    "/login",
    "read the full task from",
)


class ParsedClaudeSession(NamedTuple):
    record: SessionRecord
    latest_timestamp_ms: int


def _iter_session_files(project_dirs: tuple[Path, ...]) -> list[Path]:
    files: list[Path] = []
    for root in project_dirs:
        if not root.exists():
            continue
        for file_path in root.rglob("*.jsonl"):
            if "subagents" in file_path.parts:
                continue
            files.append(file_path)
    return sorted(files)


def _load_history_titles(history_files: tuple[Path, ...]) -> dict[str, list[tuple[int, str]]]:
    titles: dict[str, list[tuple[int, str]]] = {}
    for history_file in history_files:
        if not history_file.exists():
            continue
        with history_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                session_id = entry.get("sessionId")
                display = (entry.get("display") or "").strip()
                timestamp = int(entry.get("timestamp") or 0)
                if not session_id or not display:
                    continue
                titles.setdefault(session_id, []).append((timestamp, display))
    for values in titles.values():
        values.sort(key=lambda item: item[0])
    return titles


def _extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = (item.get("text") or "").strip()
            if text:
                texts.append(text)
    return "\n\n".join(texts).strip()


def _is_meaningful_title(display: str) -> bool:
    text = display.strip().lower()
    if not text:
        return False
    return not any(text.startswith(prefix) for prefix in BORING_TITLE_PREFIXES)


def _choose_title(session_id: str, history_titles: dict[str, list[tuple[int, str]]], first_user_text: str | None) -> str:
    for _, display in history_titles.get(session_id, []):
        if _is_meaningful_title(display):
            return display.strip()
    if first_user_text:
        return first_user_text.strip().splitlines()[0][:120]
    return session_id


def parse_claude_session_file(file_path: Path, history_titles: dict[str, list[tuple[int, str]]]) -> ParsedClaudeSession | None:
    session_id = ""
    cwd = ""
    models: set[str] = set()
    messages: list[MessageTurn] = []
    pending_user_turns: list[int] = []
    first_user_text: str | None = None
    started_at: date | None = None
    latest_timestamp_ms = 0

    with file_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("isSidechain") is True:
                continue
            timestamp = parse_iso_timestamp(event.get("timestamp", ""))
            event_ts_ms = None
            if timestamp is not None:
                event_ts_ms = int(timestamp.timestamp() * 1000)
                latest_timestamp_ms = max(latest_timestamp_ms, event_ts_ms)
                if started_at is None:
                    started_at = timestamp.date()
            session_id = event.get("sessionId") or session_id
            cwd = event.get("cwd") or cwd

            if event.get("type") == "user":
                text = _extract_text_content((event.get("message") or {}).get("content"))
                if not text:
                    continue
                if first_user_text is None:
                    first_user_text = text
                messages.append(MessageTurn(role="user", content=text, time_created=event_ts_ms))
                pending_user_turns.append(len(messages) - 1)
                continue

            if event.get("type") == "assistant":
                message = event.get("message") or {}
                model = (message.get("model") or "").strip()
                if model:
                    models.add(model)
                    for index in pending_user_turns:
                        messages[index] = messages[index]._replace(model=model)
                pending_user_turns.clear()
                text = _extract_text_content(message.get("content"))
                if text:
                    messages.append(
                        MessageTurn(role="assistant", content=text, time_created=event_ts_ms, model=model or None)
                    )

    if not session_id or not messages or latest_timestamp_ms == 0 or started_at is None:
        return None

    title = _choose_title(session_id, history_titles, first_user_text)
    if should_skip_session(title):
        return None

    return ParsedClaudeSession(
        record=SessionRecord(
            source="claude_code",
            session_id=session_id,
            title=title,
            date=started_at.isoformat(),
            messages=messages,
            project_directory=cwd,
            models_used=sorted(models),
        ),
        latest_timestamp_ms=latest_timestamp_ms,
    )


def export_claude_code(
    output_dir: Path,
    state: dict[str, Any],
    *,
    full: bool,
    dry_run: bool,
    since_date: date | None,
    project_dirs: tuple[Path, ...] = DEFAULT_CLAUDE_PROJECT_DIRS,
    history_files: tuple[Path, ...] = DEFAULT_CLAUDE_HISTORY_FILES,
) -> dict[str, Any]:
    history_titles = _load_history_titles(history_files)
    session_files = _iter_session_files(project_dirs)
    last_timestamp = int(state.get("claude_code", {}).get("last_timestamp", 0))

    exported = 0
    scanned = 0
    latest_seen = last_timestamp
    output_dir.mkdir(parents=True, exist_ok=True)

    for file_path in session_files:
        parsed = parse_claude_session_file(file_path, history_titles)
        if parsed is None:
            continue
        scanned += 1
        latest_seen = max(latest_seen, parsed.latest_timestamp_ms)
        if not full and parsed.latest_timestamp_ms <= last_timestamp:
            continue
        if since_date and date.fromisoformat(parsed.record.date) < since_date:
            continue
        output_path = unique_output_path(output_dir, parsed.record.date, parsed.record.title)
        if not dry_run:
            output_path.write_text(render_markdown(parsed.record), encoding="utf-8")
        exported += 1

    if not dry_run:
        state.setdefault("claude_code", {})["last_timestamp"] = latest_seen

    return {"source": "claude_code", "scanned": scanned, "exported": exported, "latest_seen": latest_seen}
