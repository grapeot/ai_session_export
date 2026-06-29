from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import parse_second_mind_date, unique_output_path


def _conversation_to_record(conversation: dict[str, Any]) -> SessionRecord:
    title = (conversation.get("title") or "Untitled").strip() or "Untitled"
    messages = [
        MessageTurn(role=role, content=(message.get("content") or "").rstrip())
        for message in conversation.get("messages") or []
        if (role := (message.get("role") or "").strip().lower()) in {"user", "assistant"}
        and (message.get("content") or "").strip()
    ]
    return SessionRecord(
        source="second_mind",
        session_id=conversation.get("conversation_id", ""),
        title=title,
        date=parse_second_mind_date(conversation.get("created_at", "1970-01-01 00:00:00.000000")),
        messages=messages,
    )


def export_second_mind(
    output_dir: Path,
    state: dict[str, Any],
    *,
    source_json: Path,
    full: bool,
    dry_run: bool,
    since_date: date | None,
) -> dict[str, Any]:
    if not source_json.exists():
        raise FileNotFoundError(f"Second Mind export file not found: {source_json}")

    conversations = json.loads(source_json.read_text(encoding="utf-8"))
    total = len(conversations)
    previous_count = int(state.get("second_mind", {}).get("last_export_count", 0))
    to_export = conversations if full else conversations[: max(total - previous_count, 0)]

    exported = 0
    output_dir.mkdir(parents=True, exist_ok=True)
    for conversation in reversed(to_export):
        record = _conversation_to_record(conversation)
        if since_date and date.fromisoformat(record.date) < since_date:
            continue
        output_path = unique_output_path(output_dir, record.date, record.title)
        if not dry_run:
            output_path.write_text(render_markdown(record), encoding="utf-8")
        exported += 1

    if not dry_run:
        state.setdefault("second_mind", {})["last_export_count"] = total

    return {"source": "second_mind", "total": total, "exported": exported}
