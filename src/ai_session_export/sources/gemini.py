from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import parse_iso_timestamp, should_skip_session, unique_output_path


DEFAULT_GEMINI_DIR = Path.home() / ".gemini" / "tmp"

IGNORED_USER_PREFIXES = ("/", "?", "<session_context>", "<hook_context>")


def _ignored_user_content(text: str) -> bool:
    # Mirrors gemini-cli isIgnoredUserContent: slash/help commands and
    # machine-injected context blocks are CLI machinery, not user conversation.
    trimmed = text.strip()
    return not trimmed or trimmed.startswith(IGNORED_USER_PREFIXES)


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
    return _replay_jsonl(file_path)


def _replay_jsonl(file_path: Path) -> dict[str, Any]:
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


def _display_text(item: dict[str, Any]) -> str:
    # The UI falls back to the raw content whenever the display text is empty.
    display = _text(item.get("displayContent")).strip() if item.get("displayContent") is not None else ""
    return display or _text(item.get("content")).strip()


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
        content = _display_text(item)
        if item["type"] == "gemini":
            for index in pending_users:
                messages[index] = messages[index]._replace(model=model)
            pending_users.clear()
        if item["type"] == "user" and _ignored_user_content(content):
            continue
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


def _replayed_message_count(file_path: Path) -> int | None:
    """Count messages the loader would keep after replaying replacements/checkpoints/rewinds."""
    try:
        if file_path.suffix == ".json":
            record = json.loads(file_path.read_text(encoding="utf-8"))
            messages = record.get("messages") if isinstance(record, dict) else None
            return len(messages) if isinstance(messages, list) else None
        return len(_replay_jsonl(file_path).get("messages", []))
    except (OSError, ValueError, TypeError):
        return None


def export_gemini(
    output_dir: Path, state: dict[str, Any], *, full: bool, dry_run: bool,
    since_date: date | None, gemini_dir: Path = DEFAULT_GEMINI_DIR,
) -> dict[str, Any]:
    candidates = sorted(gemini_dir.glob("*/chats/*.json*")) if gemini_dir.is_dir() else []
    candidates = [path for path in candidates if path.suffix in {".json", ".jsonl"}]
    files: list[Path] = []
    migration_fallbacks = 0
    pending_json: dict[str, Path] = {}
    for path in candidates:
        if path.suffix == ".json":
            pending_json[path.with_suffix("").name] = path
            continue
        # A migration leaves a legacy JSON alongside its JSONL successor, and the
        # JSONL is appended incrementally (metadata then messages). Prefer the
        # JSONL only when its replayed message set has caught up with the JSON's;
        # while mid-migration the JSON still holds the most complete conversation.
        jsonl_count = _replayed_message_count(path)
        json_path = path.with_suffix(".json")
        json_count = _replayed_message_count(json_path) if json_path.is_file() else None
        if json_count is None or (jsonl_count is not None and jsonl_count >= json_count):
            files.append(path)
            pending_json.pop(path.with_suffix("").name, None)
        else:
            files.append(json_path)
            pending_json.pop(json_path.with_suffix("").name, None)
            migration_fallbacks += 1
    files.extend(pending_json.values())
    sessions = state.get("gemini", {}).get("sessions", {})
    exported = failed = 0
    warnings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in files:
        try:
            record = parse_gemini_session_file(path)
            if record is None:
                stale = _retire_if_rewound_empty(path, output_dir, sessions,
                                                 since_date=since_date, dry_run=dry_run)
                if stale:
                    warnings.append(stale)
                continue
            if record.session_id in seen:
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
        except Exception as error:  # noqa: BLE001 - isolate one unreadable session
            failed += 1
            warnings.append({"path": str(path), "error": f"{type(error).__name__}: {error}"})
    result = {"source": "gemini", "scanned": len(files), "exported": exported}
    if failed:
        result["failed"] = failed
    if migration_fallbacks:
        result["migration_fallbacks"] = migration_fallbacks
    if warnings:
        result["warnings"] = warnings
    return result


def _json_rewound_to_nothing(record: dict[str, Any]) -> bool:
    """True when the JSON snapshot's messages were all removed (rewind to before the first prompt)."""
    messages = record.get("messages")
    return isinstance(messages, list) and len(messages) == 0


def _retire_if_rewound_empty(
    source_path: Path, output_dir: Path, sessions: dict[str, Any],
    *, since_date: date | None, dry_run: bool,
) -> dict[str, Any] | None:
    """Retire a stale archive only when the source proves a rewind emptied the conversation.

    A parser-None can mean many things (noise title, subagent kind, missing id); only a
    raw rewind marker that wiped every message is provider-side deletion of a session
    this exporter previously archived. Archives outside `since_date` are never touched.
    """
    if source_path.suffix == ".json":
        try:
            raw = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        rewound_empty = isinstance(raw, dict) and _json_rewound_to_nothing(raw)
        session_id = raw.get("sessionId") if isinstance(raw, dict) else None
    else:
        rewound_empty = _jsonl_has_trailing_rewind(source_path)
        session_id = _read_session_id(source_path)
    if not rewound_empty:
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    path_state = sessions.get(session_id)
    output_file = path_state.get("output_file") if isinstance(path_state, dict) else None
    if not isinstance(output_file, str) or not output_file:
        return None
    if since_date:
        # Honor the date scope: the archive was exported under a prior date; if the
        # session's own start predates since_date, its archive is out of scope.
        started = parse_iso_timestamp(_read_start_time(source_path))
        if started is None or started.date() < since_date:
            return None
    if dry_run:
        return {"path": str(source_path), "session_id": session_id,
                "error": "stale archive after rewind cleared the session (dry-run, not removed)"}
    output = output_dir / output_file
    if output.is_file():
        output.unlink()
    sessions.pop(session_id, None)
    return {"path": str(source_path), "session_id": session_id,
            "error": "retired stale archive after rewind cleared the session"}


def _jsonl_has_trailing_rewind(file_path: Path) -> bool:
    """True when the JSONL log's surviving state is a rewind that wiped every message."""
    try:
        replayed = _replay_jsonl(file_path)
    except (OSError, ValueError, TypeError):
        return False
    messages = replayed.get("messages")
    return isinstance(messages, list) and len(messages) == 0 and _jsonl_seen_rewind(file_path)


def _jsonl_seen_rewind(file_path: Path) -> bool:
    try:
        with file_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and "$rewindTo" in item:
                    return True
    except OSError:
        return False
    return False


def _read_session_id(file_path: Path) -> str | None:
    """Read the raw sessionId without full conversation replay."""
    if file_path.suffix == ".json":
        try:
            record = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return record.get("sessionId") if isinstance(record, dict) and isinstance(record.get("sessionId"), str) else None
    try:
        with file_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    session_id = item.get("sessionId")
                    if isinstance(session_id, str) and session_id:
                        return session_id
                    update = item.get("$set")
                    if isinstance(update, dict) and isinstance(update.get("sessionId"), str):
                        return update["sessionId"]
    except OSError:
        return None
    return None


def _read_start_time(file_path: Path) -> str | None:
    """Read the raw startTime without full conversation replay."""
    if file_path.suffix == ".json":
        try:
            record = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return record.get("startTime") if isinstance(record, dict) else None
    try:
        with file_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("startTime") is not None:
                    return str(item["startTime"])
    except OSError:
        return None
    return None
