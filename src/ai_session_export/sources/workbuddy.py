"""WorkBuddy (Tencent's desktop agent) adapter.

Storage (observed on WorkBuddy 5.9.4, macOS):

- Transcripts: ``~/.workbuddy-ai/projects/<cwd with "/" replaced by "-">/<session_id>.jsonl``; releases before
  5.9 used ``~/.workbuddy/projects``. One JSON object per line, OpenAI Responses-style items:
  ``session-meta``, ``message`` (``role`` user with ``input_text`` parts / assistant with ``output_text``
  parts), ``reasoning``, ``function_call``, ``function_call_result``, ``ai-title``, ``file-history-snapshot``.
  Timestamps are epoch milliseconds.
- The user's words are wrapped: every user message starts with a large ``<system-reminder>`` block of
  injected environment and identity context, followed by ``<user_query>...</user_query>``. Only the query is
  exported.
- Metadata: ``~/.workbuddy-ai/workbuddy.db`` (SQLite), table ``sessions``: ``title``, ``custom_title``
  (set when the user renames a task), ``status`` (``archived`` when archived; the transcript stays on disk).

Like the Codex adapter, only user and assistant text is exported; reasoning and tool calls stay private.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, NamedTuple

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import resolve_output_path, should_skip_session, unique_output_path


DEFAULT_WORKBUDDY_PROJECT_DIRS = (
    Path.home() / ".workbuddy-ai" / "projects",
    Path.home() / ".workbuddy" / "projects",
)
DEFAULT_WORKBUDDY_DB = Path.home() / ".workbuddy-ai" / "workbuddy.db"
SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
USER_QUERY_PATTERN = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)
SYSTEM_REMINDER_PATTERN = re.compile(r"<system-reminder\b[^>]*>.*?</system-reminder>", re.DOTALL)


class WorkBuddyMeta(NamedTuple):
    title: str
    status: str


class ParsedWorkBuddySession(NamedTuple):
    record: SessionRecord
    latest_timestamp_ms: int


def _iter_session_files(project_dirs: tuple[Path, ...]) -> list[Path]:
    files: set[Path] = set()
    for root in project_dirs:
        if not root.is_dir():
            continue
        # Transcripts sit directly under each project directory; per-session subdirectories hold
        # tool-result spill files, not transcripts.
        for path in root.glob("*/*.jsonl"):
            if SESSION_ID_PATTERN.match(path.stem):
                files.add(path)
    return sorted(files)


def load_workbuddy_meta(db_path: Path) -> dict[str, WorkBuddyMeta]:
    """Session titles and status from workbuddy.db, opened read-only. Missing or locked DB → empty."""
    meta: dict[str, WorkBuddyMeta] = {}
    if not db_path.is_file():
        return meta
    try:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return meta
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "id" not in columns:
            return meta
        wanted = [c for c in ("title", "custom_title", "status") if c in columns]
        select = ", ".join(["id", *wanted])
        for row in conn.execute(f"SELECT {select} FROM sessions"):
            values = dict(zip(["id", *wanted], row))
            title = str(values.get("custom_title") or "").strip() or str(values.get("title") or "").strip()
            meta[str(values["id"])] = WorkBuddyMeta(title=title, status=str(values.get("status") or ""))
    except sqlite3.Error:
        return meta
    finally:
        conn.close()
    return meta


def _user_text(content: Any) -> str:
    parts: list[str] = []
    for entry in content or []:
        if not isinstance(entry, dict):
            continue
        raw = str(entry.get("text") or "")
        queries = [q.strip() for q in USER_QUERY_PATTERN.findall(raw) if q.strip()]
        if queries:
            parts.extend(queries)
            continue
        stripped = SYSTEM_REMINDER_PATTERN.sub("", raw).strip()
        if stripped:
            parts.append(stripped)
    return "\n".join(parts).strip()


def _assistant_text(content: Any) -> str:
    parts = []
    for entry in content or []:
        if isinstance(entry, dict) and entry.get("type") in ("output_text", "text"):
            text = str(entry.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _model_of(item: dict) -> str | None:
    provider = item.get("providerData")
    if not isinstance(provider, dict):
        return None
    model = str(provider.get("requestModelName") or provider.get("model") or "").strip()
    return model or None


def parse_workbuddy_session_file(
    file_path: Path, meta: dict[str, WorkBuddyMeta] | None = None
) -> ParsedWorkBuddySession | None:
    session_id = file_path.stem
    cwd = ""
    ai_title = ""
    models: set[str] = set()
    messages: list[MessageTurn] = []
    first_user_text = ""
    first_ts: int | None = None
    latest_timestamp_ms = 0
    pending_user_turns: list[int] = []

    with file_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            try:
                item = json.loads(raw_line)
            except json.JSONDecodeError:
                # A task that is still running may end with a partial line.
                continue
            if not isinstance(item, dict):
                continue
            ts = item.get("timestamp")
            ts_ms = int(ts) if isinstance(ts, (int, float)) else None
            if ts_ms is not None:
                latest_timestamp_ms = max(latest_timestamp_ms, ts_ms)
            cwd = str(item.get("cwd") or cwd)
            session_id = str(item.get("sessionId") or session_id)
            item_type = item.get("type")

            if item_type == "ai-title":
                ai_title = str(item.get("aiTitle") or "").strip() or ai_title
                continue
            if item_type != "message":
                continue

            role = item.get("role")
            if role == "user":
                text = _user_text(item.get("content"))
                if not text:
                    continue
                if not first_user_text:
                    first_user_text = text
                    first_ts = ts_ms
                messages.append(MessageTurn(role="user", content=text, time_created=ts_ms))
                pending_user_turns.append(len(messages) - 1)
            elif role == "assistant":
                model = _model_of(item)
                if model:
                    models.add(model)
                    for index in pending_user_turns:
                        messages[index] = messages[index]._replace(model=model)
                pending_user_turns.clear()
                text = _assistant_text(item.get("content"))
                if text:
                    messages.append(MessageTurn(role="assistant", content=text, time_created=ts_ms, model=model))

    if not first_user_text or first_ts is None:
        return None

    info = (meta or {}).get(session_id)
    title = (info.title if info else "") or ai_title or first_user_text.splitlines()[0][:120]
    if should_skip_session(title):
        return None
    started = datetime.fromtimestamp(first_ts / 1000).date()

    return ParsedWorkBuddySession(
        record=SessionRecord(
            source="workbuddy",
            session_id=session_id,
            title=title,
            date=started.isoformat(),
            messages=messages,
            project_directory=cwd,
            models_used=sorted(models),
            surface="archived" if info and info.status == "archived" else "",
        ),
        latest_timestamp_ms=latest_timestamp_ms,
    )


def export_workbuddy(
    output_dir: Path,
    state: dict[str, Any],
    *,
    full: bool,
    dry_run: bool,
    since_date: date | None,
    project_dirs: tuple[Path, ...] = DEFAULT_WORKBUDDY_PROJECT_DIRS,
    db_path: Path = DEFAULT_WORKBUDDY_DB,
) -> dict[str, Any]:
    session_files = _iter_session_files(project_dirs)
    meta = load_workbuddy_meta(db_path)
    source_state = state.setdefault("workbuddy", {})
    session_state: dict[str, dict[str, Any]] = source_state.setdefault("sessions", {})

    exported = 0
    scanned = 0
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for file_path in session_files:
        scanned += 1
        path_state = session_state.get(file_path.stem, {})
        path_output = str(path_state.get("output_file") or "")
        path_output_exists = bool(path_output) and (output_dir / path_output).is_file()
        file_mtime_ns = file_path.stat().st_mtime_ns
        title_now = meta[file_path.stem].title if file_path.stem in meta else ""
        if (
            not full
            and int(path_state.get("source_mtime_ns", 0)) == file_mtime_ns
            and str(path_state.get("title", "")) == title_now
            and path_output_exists
        ):
            continue

        parsed = parse_workbuddy_session_file(file_path, meta)
        if parsed is None:
            continue
        if since_date and date.fromisoformat(parsed.record.date) < since_date:
            continue

        previous = session_state.get(parsed.record.session_id, {})
        previous_output = str(previous.get("output_file") or "")
        if previous_output and not dry_run:
            output_path = resolve_output_path(output_dir, previous_output, parsed.record.date, parsed.record.title)
        elif previous_output:
            output_path = output_dir / previous_output
        else:
            output_path = unique_output_path(output_dir, parsed.record.date, parsed.record.title)
        if not dry_run:
            output_path.write_text(render_markdown(parsed.record), encoding="utf-8")
            session_state[parsed.record.session_id] = {
                "latest_timestamp": parsed.latest_timestamp_ms,
                "output_file": output_path.name,
                "source_mtime_ns": file_mtime_ns,
                "title": title_now,
            }
        exported += 1

    return {"source": "workbuddy", "scanned": scanned, "exported": exported}
