from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import ms_to_date, parse_iso_timestamp, unique_output_path


DEFAULT_CURSOR_DB = (
    Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
)

BUBBLE_KEY_PREFIX = "bubbleId:"
# A bubble key is `bubbleId:<composerId>:<bubbleId>` where both ids are 36-char
# UUIDs. SQLite `substr` is 1-indexed, so the composer id starts at
# len("bubbleId:") + 1 and spans 36 characters.
COMPOSER_ID_OFFSET = len(BUBBLE_KEY_PREFIX) + 1
COMPOSER_ID_LENGTH = 36
# The character after `:` (0x3A) is `;` (0x3B), so a half-open range
# [prefix, "bubbleId;") captures every bubble key via the key index.
BUBBLE_RANGE_END = "bubbleId;"


class CursorComposerMeta(NamedTuple):
    title: str
    project_directory: str
    created_ms: int
    last_updated_ms: int
    is_subagent: bool


def _parse_iso_to_ms(value: str) -> int | None:
    timestamp = parse_iso_timestamp(value)
    return int(timestamp.timestamp() * 1000) if timestamp is not None else None


def _lexical_to_text(node: Any) -> str:
    """Extract plain text from a Lexical editor JSON tree."""
    if isinstance(node, dict):
        node_type = node.get("type")
        if node_type == "text":
            return str(node.get("text") or "")
        if node_type == "linebreak":
            return "\n"
        children = node.get("children")
        if isinstance(children, list):
            return "".join(_lexical_to_text(child) for child in children)
        return ""
    if isinstance(node, list):
        return "".join(_lexical_to_text(child) for child in node)
    return ""


def _bubble_text(bubble: dict[str, Any]) -> str:
    text = str(bubble.get("text") or "").strip()
    if text:
        return text
    rich_text = bubble.get("richText")
    if isinstance(rich_text, str) and rich_text.strip():
        try:
            return _lexical_to_text(json.loads(rich_text)).strip()
        except json.JSONDecodeError:
            return ""
    return ""


def _parse_header_value(composer_id: str, value: str) -> CursorComposerMeta:
    try:
        meta = json.loads(value or "{}")
    except json.JSONDecodeError:
        meta = {}

    if not isinstance(meta, dict):
        meta = {}

    workspace = meta.get("workspaceIdentifier") or {}
    if isinstance(workspace, dict):
        uri = workspace.get("uri") or {}
        project_directory = str((uri or {}).get("fsPath") or "") if isinstance(uri, dict) else ""
    else:
        project_directory = ""

    title = str(meta.get("name") or "").strip()
    return CursorComposerMeta(
        title=title,
        project_directory=project_directory,
        created_ms=meta.get("createdAt") or 0,
        last_updated_ms=meta.get("lastUpdatedAt") or 0,
        is_subagent=bool(meta.get("isSubagent", False)),
    )


def parse_cursor_composer(
    composer_id: str,
    bubbles: list[dict[str, Any]],
    header: CursorComposerMeta | None,
) -> SessionRecord | None:
    messages: list[MessageTurn] = []
    models: set[str] = set()
    user_count = 0
    first_user_text = ""
    current_model: str | None = None

    for bubble in bubbles:
        bubble_type = bubble.get("type")
        if bubble_type == 1:
            role = "user"
        elif bubble_type == 2:
            role = "assistant"
        else:
            continue

        text = _bubble_text(bubble)
        if not text:
            continue

        time_created = _parse_iso_to_ms(str(bubble.get("createdAt") or ""))

        # Cursor records the responding model on the user bubble (the model
        # selected to answer that turn), and leaves assistant bubbles largely
        # unannotated. Mirror Claude Code's attribution: capture the model from
        # a user turn and apply it to that turn and its assistant responses.
        model_info = bubble.get("modelInfo") or {}
        if isinstance(model_info, dict):
            model_name = str(model_info.get("modelName") or "").strip()
            if model_name:
                current_model = model_name
                models.add(model_name)

        messages.append(MessageTurn(role=role, content=text, time_created=time_created, model=current_model))
        if role == "user":
            user_count += 1
            if not first_user_text:
                first_user_text = text

    if user_count == 0 or not messages:
        return None

    title = (header.title if header and header.title else "") or first_user_text.splitlines()[0][:120]
    if not title:
        title = composer_id

    first_time = next((m.time_created for m in messages if m.time_created), None)
    if header and header.created_ms:
        session_date = ms_to_date(int(header.created_ms))
    elif first_time:
        session_date = ms_to_date(first_time)
    else:
        session_date = date.today().isoformat()

    return SessionRecord(
        source="cursor",
        session_id=composer_id,
        title=title,
        date=session_date,
        messages=messages,
        project_directory=(header.project_directory if header else ""),
        models_used=sorted(models),
    )


def _load_composer_meta(conn: sqlite3.Connection) -> dict[str, CursorComposerMeta]:
    meta_by_id: dict[str, CursorComposerMeta] = {}
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT composerId, value, isSubagent FROM composerHeaders")
        for composer_id, value, is_subagent in cursor.fetchall():
            meta = _parse_header_value(composer_id, value or "")
            meta_by_id[composer_id] = meta._replace(is_subagent=bool(is_subagent))
    except sqlite3.OperationalError:
        pass
    return meta_by_id


def export_cursor(
    output_dir: Path,
    state: dict[str, Any],
    *,
    db_path: Path = DEFAULT_CURSOR_DB,
    full: bool,
    dry_run: bool,
    since_date: date | None,
) -> dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(f"Cursor database not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    source_state = state.setdefault("cursor", {})
    session_state: dict[str, dict[str, Any]] = source_state.setdefault("sessions", {})

    meta_by_id = _load_composer_meta(conn)

    # Enumerate composers from bubble keys, with their max bubble timestamp as an
    # ISO string. This is authoritative: composerHeaders may be missing for old
    # or deleted composers, and may list composers that no longer have bubbles.
    cursor = conn.cursor()
    cursor.execute(
        "SELECT substr(key, ?, ?) AS composer_id, max(json_extract(value, '$.createdAt')) "
        "FROM cursorDiskKV WHERE key >= ? AND key < ? GROUP BY composer_id",
        (COMPOSER_ID_OFFSET, COMPOSER_ID_LENGTH, BUBBLE_KEY_PREFIX, BUBBLE_RANGE_END),
    )
    composers: dict[str, str] = {row[0]: row[1] or "" for row in cursor.fetchall()}

    exported = 0
    scanned = 0
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for composer_id, max_created_at in composers.items():
        scanned += 1
        header = meta_by_id.get(composer_id)

        # Sub-agent composers are agent-to-agent chatter, not user dialogue.
        if header and header.is_subagent:
            continue

        header_updated_ms = int(header.last_updated_ms) if header else 0
        bubble_max_ms = _parse_iso_to_ms(max_created_at) or 0
        latest_timestamp_ms = max(header_updated_ms, bubble_max_ms)

        previous = session_state.get(composer_id, {})
        previous_timestamp = int(previous.get("latest_timestamp", 0))
        previous_output = str(previous.get("output_file") or "")
        output_path = output_dir / previous_output if previous_output else None
        output_exists = output_path is not None and output_path.is_file()
        if not full and latest_timestamp_ms <= previous_timestamp and output_exists:
            continue

        composer_prefix = f"{BUBBLE_KEY_PREFIX}{composer_id}:"
        composer_end = f"{BUBBLE_KEY_PREFIX}{composer_id};"
        cursor.execute(
            "SELECT value FROM cursorDiskKV WHERE key >= ? AND key < ? "
            "ORDER BY json_extract(value, '$.createdAt')",
            (composer_prefix, composer_end),
        )
        bubbles: list[dict[str, Any]] = []
        for (raw_value,) in cursor.fetchall():
            if raw_value is None:
                continue
            try:
                parsed = json.loads(raw_value)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                bubbles.append(parsed)

        record = parse_cursor_composer(composer_id, bubbles, header)
        if record is None:
            continue
        if since_date and date.fromisoformat(record.date) < since_date:
            continue

        if output_path is None:
            output_path = unique_output_path(output_dir, record.date, record.title)
        if not dry_run:
            output_path.write_text(render_markdown(record), encoding="utf-8")
            session_state[composer_id] = {
                "latest_timestamp": latest_timestamp_ms,
                "output_file": output_path.name,
            }
        exported += 1

    conn.close()
    return {"source": "cursor", "scanned": scanned, "exported": exported}
