from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import ms_to_date, should_skip_session, unique_output_path


def load_opencode_session_messages(conn: sqlite3.Connection, session_id: str) -> tuple[list[MessageTurn], list[str], int]:
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

    for message_id, role, model_id, time_created in cursor.fetchall():
        if role not in {"user", "assistant"}:
            continue
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
            continue
        turn_time = int(time_created) if time_created is not None else None
        normalized_model = str(model_id).strip() or None
        messages.append(MessageTurn(role=role, content=content, time_created=turn_time, model=normalized_model))
        if role == "user":
            user_count += 1
        if normalized_model:
            models.add(normalized_model)

    return messages, sorted(models), user_count


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
    last_session_time = int(state.get("opencode", {}).get("last_session_time", 0))

    query = "SELECT id, title, directory, time_created FROM session"
    params: tuple[Any, ...] = ()
    if not full:
        query += " WHERE time_created > ?"
        params = (last_session_time,)
    query += " ORDER BY time_created ASC"

    cursor = conn.cursor()
    cursor.execute(query, params)
    rows = cursor.fetchall()

    exported = 0
    scanned = 0
    latest_seen = last_session_time
    output_dir.mkdir(parents=True, exist_ok=True)

    for row in rows:
        scanned += 1
        session_id = row["id"]
        title = (row["title"] or "").strip() or "Untitled"
        session_time = int(row["time_created"] or 0)
        latest_seen = max(latest_seen, session_time)

        if should_skip_session(title):
            continue

        messages, models, user_count = load_opencode_session_messages(conn, session_id)
        if user_count == 0:
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
        if since_date and date.fromisoformat(record.date) < since_date:
            continue
        output_path = unique_output_path(output_dir, record.date, record.title)
        if not dry_run:
            output_path.write_text(render_markdown(record), encoding="utf-8")
        exported += 1

    conn.close()
    if not dry_run:
        state.setdefault("opencode", {})["last_session_time"] = latest_seen

    return {"source": "opencode", "scanned": scanned, "exported": exported, "latest_seen": latest_seen}
