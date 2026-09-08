from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import parse_iso_timestamp, should_skip_session, unique_output_path


DEFAULT_GEMINI_DIR = Path.home() / ".gemini" / "tmp"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = content if isinstance(content, list) else [content]
    return "".join(
        part if isinstance(part, str) else part.get("text", "")
        for part in parts
        if isinstance(part, str)
        or (isinstance(part, dict) and isinstance(part.get("text"), str) and not part.get("thought"))
    )


def _load_conversation(file_path: Path) -> dict[str, Any]:
    if file_path.suffix == ".json":
        record = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError("invalid conversation object")
        return record

    metadata: dict[str, Any] = {}
    messages: dict[str, dict[str, Any]] = {}
    with file_path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                # The provider also skips torn or malformed append records.
                continue
            if not isinstance(item, dict):
                continue
            if "$rewindTo" in item:
                ids = list(messages)
                target = item["$rewindTo"]
                for message_id in ids[ids.index(target) if target in ids else 0:]:
                    del messages[message_id]
            elif isinstance(item.get("id"), str) and "type" in item:
                messages[item["id"]] = item
            else:
                update = item.get("$set", item)
                if not isinstance(update, dict):
                    continue
                if isinstance(update.get("messages"), list):
                    if "$set" in item:
                        messages.clear()
                    for message in update["messages"]:
                        if isinstance(message, dict) and isinstance(message.get("id"), str):
                            messages[message["id"]] = message
                metadata.update(update)
    return {**metadata, "messages": list(messages.values())}


def parse_gemini_session_file(file_path: Path) -> SessionRecord | None:
    data = _load_conversation(file_path)
    if data.get("kind") == "subagent":
        return None
    session_id = data.get("sessionId")
    started = parse_iso_timestamp(str(data.get("startTime") or ""))
    if not isinstance(session_id, str) or not session_id or started is None:
        return None
    messages: list[MessageTurn] = []
    pending_users: list[int] = []
    models: set[str] = set()
    for item in data.get("messages", []):
        if not isinstance(item, dict) or item.get("type") not in {"user", "gemini"}:
            continue
        model = item.get("model") if item["type"] == "gemini" else None
        model = model if isinstance(model, str) and model else None
        if model:
            models.add(model)
        # Display content is the provider's user-facing text, before context expansion.
        content = _text(item.get("displayContent") if item.get("displayContent") is not None else item.get("content")).strip()
        if item["type"] == "gemini":
            for index in pending_users:
                messages[index] = messages[index]._replace(model=model)
            pending_users.clear()
        if not content:
            continue
        timestamp = parse_iso_timestamp(str(item.get("timestamp") or ""))
        messages.append(MessageTurn(
            role="user" if item["type"] == "user" else "assistant",
            content=content,
            time_created=int(timestamp.timestamp() * 1000) if timestamp else None,
            model=model,
        ))
        if item["type"] == "user":
            pending_users.append(len(messages) - 1)
    first_user = next((message.content for message in messages if message.role == "user"), "")
    if not first_user:
        return None
    title = str(data.get("summary") or first_user.splitlines()[0][:120])
    if should_skip_session(title):
        return None
    # projectHash is not a path, and added directories do not identify the original cwd.
    return SessionRecord("gemini", session_id, title, started.date().isoformat(), messages,
                         models_used=sorted(models))


def export_gemini(
    output_dir: Path, state: dict[str, Any], *, full: bool, dry_run: bool,
    since_date: date | None, gemini_dir: Path = DEFAULT_GEMINI_DIR,
) -> dict[str, Any]:
    files = sorted(gemini_dir.glob("*/chats/*.json*")) if gemini_dir.is_dir() else []
    # A migration leaves a legacy JSON alongside its JSONL successor.
    files = [path for path in files if path.suffix in {".json", ".jsonl"}
             and not (path.suffix == ".json" and path.with_suffix(".jsonl").is_file())]
    sessions = state.get("gemini", {}).get("sessions", {})
    exported = failed = 0
    seen: set[str] = set()
    for path in files:
        try:
            record = parse_gemini_session_file(path)
            if record is None or record.session_id in seen:
                continue
            seen.add(record.session_id)
            if since_date and date.fromisoformat(record.date) < since_date:
                continue
            previous = sessions.get(record.session_id, {})
            output = output_dir / previous["output_file"] if previous else unique_output_path(output_dir, record.date, record.title)
            rendered = render_markdown(record)
            if not full and output.is_file() and output.read_text(encoding="utf-8") == rendered:
                continue
            if not dry_run:
                output_dir.mkdir(parents=True, exist_ok=True)
                output.write_text(rendered, encoding="utf-8")
                state.setdefault("gemini", {}).setdefault("sessions", {})[record.session_id] = {"output_file": output.name}
            exported += 1
        except (OSError, ValueError, TypeError):
            failed += 1
    result = {"source": "gemini", "scanned": len(files), "exported": exported}
    if failed:
        result["failed"] = failed
    return result
