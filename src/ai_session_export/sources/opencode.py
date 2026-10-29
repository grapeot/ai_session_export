from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import (
    archived_session_id,
    ms_to_date,
    resolve_output_path,
    session_aware_output_path,
    should_skip_session,
)


def _session_activity_max(conn: sqlite3.Connection, session_id: str) -> int:
    """Max timestamp across a session's message and part rows."""

    row = conn.execute(
        """
        SELECT MAX(t) FROM (
            SELECT COALESCE(time_created, 0) AS t FROM message WHERE session_id = ?
            UNION ALL
            SELECT COALESCE(time_created, 0) FROM part WHERE session_id = ?
        )
        """,
        (session_id, session_id),
    ).fetchone()
    return int(row[0] or 0)


def _resolve_archive_path(
    output_dir: Path,
    entries: dict[str, Any],
    session_id: str,
    record: SessionRecord,
    *,
    dry_run: bool,
) -> Path:
    """Pick the archive file to (re)write for a session.

    The per-session ``sessions[session_id].output_file`` mapping is the primary
    pointer; it is probed defensively so a replaced file falls through. In
    dry-run the mapping is used only when it already points at a live file —
    renames are skipped so nothing on disk changes.
    """
    entry = entries.get(session_id)
    previous = entry.get("output_file") if isinstance(entry, dict) else None
    if previous:
        mapped = output_dir / Path(previous).name
        if mapped.is_file() and archived_session_id(mapped) == session_id:
            if dry_run:
                return mapped
            return resolve_output_path(output_dir, previous, record.date, record.title)
    return session_aware_output_path(output_dir, session_id, record.date, record.title)


def load_opencode_session_messages(
    conn: sqlite3.Connection, session_id: str
) -> tuple[list[MessageTurn], list[str], int, int, bool]:
    """Load human-readable turns plus a transcript-growth cursor ceiling.

    Returns ``(turns, sorted_models, user_count, cursor_ceiling_ms, settled)``.
    ``settled=True`` means every row rendered cleanly (assistant/tool rows
    without text parts are a normal final state) and the caller may advance
    its retry cursor up to the session's message+part activity maximum.
    ``settled=False`` means a user-role message row has no text part yet, so
    text may still stream in at/after ``cursor_ceiling_ms`` and the cursor
    must stop below it.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id,
               COALESCE(json_extract(data, '$.role'), ''),
               COALESCE(json_extract(data, '$.model.modelID'), json_extract(data, '$.modelID'), ''),
               time_created
        FROM message
        WHERE session_id = ?
        ORDER BY time_created ASC
        """,
        (session_id,),
    )

    messages: list[MessageTurn] = []
    models: set[str] = set()
    user_count = 0
    cursor_ceiling = 0
    settled = True

    for message_id, role, model_id, time_created in cursor.fetchall():
        turn_time = int(time_created) if time_created is not None else 0
        cursor.execute(
            """
            SELECT COALESCE(json_extract(data, '$.text'), '')
            FROM part
            WHERE session_id = ?
              AND message_id = ?
              AND json_extract(data, '$.type') = 'text'
            ORDER BY time_created ASC
            """,
            (session_id, message_id),
        )
        content = "".join(row[0] for row in cursor.fetchall()).strip()
        if not content:
            if role == "user":
                # User text may still be streaming in at/after this timestamp
                settled = False
                if cursor_ceiling >= turn_time:
                    cursor_ceiling = turn_time - 1
            continue
        if settled:
            cursor_ceiling = max(cursor_ceiling, turn_time)
        normalized_model = str(model_id).strip() or None
        messages.append(MessageTurn(role=role, content=content, time_created=turn_time, model=normalized_model))
        if role == "user":
            user_count += 1
        if normalized_model:
            models.add(normalized_model)

    return messages, sorted(models), user_count, int(cursor_ceiling), bool(settled)


def export_opencode(
    output_dir: Path,
    state: dict[str, Any],
    *,
    db_path: Path,
    full: bool,
    dry_run: bool,
    since_date: date | None,
) -> dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(f"OpenCode database not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    opencode_state = state.setdefault("opencode", {})
    if "last_message_time" not in opencode_state:
        # One-time migration. Selection used to be keyed on session creation
        # time, which froze sessions that kept growing after their first
        # export. A message's own timestamp is always >= its session's
        # creation time, so reusing the old cutoff starts message-time
        # selection without rescanning the whole history.
        opencode_state["last_message_time"] = int(opencode_state.get("last_session_time", 0))
    cutoff = int(opencode_state.get("last_message_time", 0))
    session_entries: dict[str, Any] = opencode_state.setdefault("sessions", {})

    query = "SELECT id, title, directory, time_created FROM session"
    params: tuple[Any, ...] = ()
    if not full:
        # Select sessions with any new activity (message or part rows), so a
        # late-arriving part on an old message also re-enters selection.
        query += (
            " WHERE id IN ("
            "SELECT session_id FROM message WHERE COALESCE(time_created, 0) > ?"
            " UNION"
            " SELECT session_id FROM part WHERE COALESCE(time_created, 0) > ?)"
        )
        params = (cutoff, cutoff)
    query += " ORDER BY time_created ASC"

    cursor = conn.cursor()
    cursor.execute(query, params)
    rows = cursor.fetchall()

    exported = 0
    scanned = 0
    overwritten = 0
    skipped_shrunk = 0
    cursor_advance = 0
    output_dir.mkdir(parents=True, exist_ok=True)

    for row in rows:
        scanned += 1
        session_id = row["id"]
        title = (row["title"] or "").strip() or "Untitled"
        session_time = int(row["time_created"] or 0)

        if should_skip_session(title):
            continue
        if since_date and ms_to_date(session_time) < str(since_date):
            continue

        messages, models, user_count, ceiling, settled = load_opencode_session_messages(conn, session_id)
        if user_count == 0:
            # No human turn has text content yet; nothing to render. The
            # ceiling stays below this session's rows, so the run after the
            # text part lands re-renders it.
            continue

        record = SessionRecord(
            source="opencode",
            session_id=session_id,
            title=title,
            date=ms_to_date(session_time),
            messages=messages,
            project_directory=row["directory"] or "",
            models_used=models,
        )
        output_path = _resolve_archive_path(
            output_dir, session_entries, session_id, record, dry_run=dry_run
        )
        candidate_bytes = render_markdown(record).encode("utf-8")
        wrote = False
        if output_path.exists():
            existing_bytes = output_path.read_bytes()
            if len(candidate_bytes) < len(existing_bytes):
                # The on-disk source lost history relative to the archive
                # (truncated transcript). Keep the bigger archive file and
                # keep the session retryable; do not advance the cursor.
                skipped_shrunk += 1
                exported += 1
                continue
            if existing_bytes != candidate_bytes:
                overwritten += 1
                wrote = True
            # byte-identical re-emits write nothing (no mtime churn)
        else:
            wrote = True
        if wrote and not dry_run:
            output_path.write_bytes(candidate_bytes)
            session_entries[session_id] = {"output_file": output_path.name}
        exported += 1
        if settled:
            # A settled export may advance the cursor up to the session's
            # message+part activity maximum, so converged late-part sessions
            # stop being re-rendered. Settled means every row present was
            # rendered; unsettled sessions keep the pending ceiling only.
            if settled:
                cursor_advance = max(
                    cursor_advance,
                    _session_activity_max(conn, session_id),
                )
        else:
            cursor_advance = max(cursor_advance, ceiling)

    if not dry_run:
        opencode_state["last_message_time"] = max(cutoff, int(cursor_advance))
        state["opencode"] = opencode_state

    conn.close()
    return {
        "source": "opencode",
        "scanned": scanned,
        "exported": exported,
        "overwritten": overwritten,
        "skipped_shrunk": skipped_shrunk,
    }
