"""WorkBuddy (Tencent's desktop agent) adapter.

Storage (observed on WorkBuddy 5.9.4, macOS):

- Transcripts: ``~/.workbuddy-ai/projects/<cwd with "/" replaced by "-">/<session_id>.jsonl``. The app bundle
  also lists ``~/.workbuddy/projects`` among its project roots (assumed to be the pre-5.9 location; not
  verified on a real install), so both are scanned and a session present in both is read once, from the newer
  file. One JSON object per line, OpenAI Responses-style items: ``session-meta``, ``message`` (``role`` user
  with ``input_text`` parts / assistant with ``output_text`` parts), ``reasoning``, ``function_call``,
  ``function_call_result``, ``ai-title``, ``file-history-snapshot``. Timestamps are epoch milliseconds.
  Per-session subdirectories hold tool-result spill files and are not transcripts.
- The user's words are wrapped: every user message starts with injected ``<system-reminder>`` blocks
  (environment and identity context), followed by ``<user_query>...</user_query>``. Only the query is exported.
- Metadata: ``~/.workbuddy-ai/workbuddy.db`` (SQLite), table ``sessions``: ``title``, ``custom_title`` (set when
  the user renames a task), ``status`` (``archived`` when archived; the transcript stays on disk) and
  ``deleted_at``. Tasks the user deleted in the app are not exported.

Like the Codex adapter, only user and assistant text is exported; reasoning and tool calls stay private.
Archived tasks are exported like any other task.
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
    deleted: bool


class ParsedWorkBuddySession(NamedTuple):
    record: SessionRecord
    latest_timestamp_ms: int


def _iter_session_files(project_dirs: tuple[Path, ...]) -> list[Path]:
    """One transcript per session id; when a session exists under several roots, keep the newest file."""
    best: dict[str, tuple[int, Path]] = {}
    for root in project_dirs:
        if not root.is_dir():
            continue
        for path in root.glob("*/*.jsonl"):
            if not SESSION_ID_PATTERN.match(path.stem):
                continue
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            if path.stem not in best or mtime > best[path.stem][0]:
                best[path.stem] = (mtime, path)
    return sorted(path for _, path in best.values())


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri()
    wal = db_path.with_name(db_path.name + "-wal")
    # Without a WAL file, immutable=1 lets us read even from a read-only directory.
    suffix = "?mode=ro" if wal.exists() else "?mode=ro&immutable=1"
    return sqlite3.connect(uri + suffix, uri=True, timeout=2)


def load_workbuddy_meta(db_path: Path) -> dict[str, WorkBuddyMeta] | None:
    """Titles and deletion flags from workbuddy.db, read-only. None when the DB cannot be read."""
    if not db_path.is_file():
        return None
    try:
        conn = _connect_readonly(db_path)
    except (sqlite3.Error, ValueError, OSError):
        return None
    meta: dict[str, WorkBuddyMeta] = {}
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "id" not in columns:
            return None
        wanted = [c for c in ("title", "custom_title", "deleted_at") if c in columns]
        select = ", ".join(["id", *wanted])
        for row in conn.execute(f"SELECT {select} FROM sessions"):
            values = dict(zip(["id", *wanted], row))
            title = str(values.get("custom_title") or "").strip() or str(values.get("title") or "").strip()
            meta[str(values["id"])] = WorkBuddyMeta(title=title, deleted=bool(values.get("deleted_at")))
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return meta


def _user_text(content: Any) -> str:
    parts: list[str] = []
    for entry in content or []:
        if not isinstance(entry, dict):
            continue
        # Strip the injected context first, so a <user_query> quoted inside it is never picked up.
        raw = SYSTEM_REMINDER_PATTERN.sub("", str(entry.get("text") or ""))
        queries = [q.strip() for q in USER_QUERY_PATTERN.findall(raw) if q.strip()]
        if queries:
            parts.extend(queries)
            continue
        stripped = raw.strip()
        # An unclosed reminder or any leftover injected wrapper: drop the part rather than risk leaking it.
        if not stripped or "<system-reminder" in stripped or stripped.startswith("<"):
            continue
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
    # `model` is the provider's model id; `requestModelName` is a UI label (it can carry a thinking-level suffix).
    model = str(provider.get("model") or provider.get("requestModelName") or "").strip()
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

    # errors="replace": a task still being written can end mid UTF-8 sequence; that partial line then fails
    # json.loads and is skipped like any other partial line.
    with file_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            try:
                item = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            ts = item.get("timestamp")
            ts_ms = int(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None
            if ts_ms is not None:
                latest_timestamp_ms = max(latest_timestamp_ms, ts_ms)
                if first_ts is None:
                    first_ts = ts_ms
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
    if info and info.deleted:
        return None
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
        ),
        latest_timestamp_ms=latest_timestamp_ms,
    )


def _fingerprint(info: WorkBuddyMeta | None) -> list:
    return [info.title, info.deleted] if info else ["", False]


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
    source_state = state.setdefault("workbuddy", {})
    session_state: dict[str, dict[str, Any]] = source_state.setdefault("sessions", {})
    meta = load_workbuddy_meta(db_path)
    db_available = meta is not None
    if not db_available:
        # Keep the titles we already know instead of falling back to ai-title until the DB is readable again.
        meta = {
            sid: WorkBuddyMeta(title=str(entry.get("db_fingerprint", ["", False])[0]), deleted=False)
            for sid, entry in session_state.items()
        }

    exported = 0
    scanned = 0
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for file_path in session_files:
        scanned += 1
        sid = file_path.stem
        try:
            file_mtime_ns = file_path.stat().st_mtime_ns
        except OSError:
            continue  # removed between the scan and now
        path_state = session_state.get(sid, {})
        path_output = str(path_state.get("output_file") or "")
        path_output_exists = bool(path_output) and (output_dir / path_output).is_file()
        fingerprint = _fingerprint(meta.get(sid)) if db_available else path_state.get("db_fingerprint")
        if (
            not full
            and int(path_state.get("source_mtime_ns", 0)) == file_mtime_ns
            and path_state.get("db_fingerprint") == fingerprint
            and path_output_exists
        ):
            continue

        try:
            parsed = parse_workbuddy_session_file(file_path, meta)
        except OSError:
            continue
        if parsed is None:
            continue
        if since_date and date.fromisoformat(parsed.record.date) < since_date:
            continue

        previous_output = str(path_state.get("output_file") or "")
        if previous_output and not dry_run:
            output_path = resolve_output_path(output_dir, previous_output, parsed.record.date, parsed.record.title)
        elif previous_output:
            output_path = output_dir / previous_output
        else:
            output_path = unique_output_path(output_dir, parsed.record.date, parsed.record.title)
        if not dry_run:
            output_path.write_text(render_markdown(parsed.record), encoding="utf-8")
            session_state[sid] = {
                "latest_timestamp": parsed.latest_timestamp_ms,
                "output_file": output_path.name,
                "source_mtime_ns": file_mtime_ns,
                "db_fingerprint": fingerprint if fingerprint is not None else _fingerprint(meta.get(sid)),
            }
        exported += 1

    return {"source": "workbuddy", "scanned": scanned, "exported": exported}
