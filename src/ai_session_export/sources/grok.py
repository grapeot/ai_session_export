from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import parse_iso_timestamp, should_skip_session, unique_output_path


DEFAULT_GROK_SESSIONS_DIR = Path.home() / ".grok" / "sessions"


def _object(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"invalid {field} object")
    return value


def _params(event: dict[str, Any]) -> dict[str, Any]:
    params = event.get("params", event)
    return params if isinstance(params, dict) else {}


def _timestamp(event: dict[str, Any]) -> int | None:
    meta = _object(_params(event).get("_meta"), "notification metadata")
    timestamp = meta.get("agentTimestampMs")
    if isinstance(timestamp, (int, float)):
        return int(timestamp)
    seconds = event.get("timestamp")
    return int(seconds * 1000) if isinstance(seconds, (int, float)) else None


def _surviving_updates(file_path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    prompt_starts: list[int] = []
    in_user = False
    previous_index = None
    seen_marker = False
    with file_path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                in_user = False
                continue
            if not isinstance(event, dict):
                in_user = False
                continue
            update = _params(event).get("update", {})
            if not isinstance(update, dict):
                in_user = False
                continue
            tag = update.get("sessionUpdate")
            if event.get("method") == "_x.ai/session/update" and tag == "rewind_marker":
                target = update.get("target_prompt_index")
                if isinstance(target, int) and 0 <= target < len(prompt_starts):
                    events = events[:prompt_starts[target]]
                    prompt_starts = prompt_starts[:target]
                in_user = False
                continue
            meta = _object(update.get("_meta"), "chunk metadata")
            index = meta.get("promptIndex")
            index = index if type(index) is int and index >= 0 else None
            is_user = (event.get("method", "session/update") == "session/update"
                       and tag == "user_message_chunk" and not meta.get("hostTurn"))
            if is_user:
                if index is not None:
                    seen_marker = True
                # Provider replay counts unmarked runs only before the first marker;
                # later unmarked chunks can be context within an existing prompt.
                new_run = not in_user or (seen_marker and index != previous_index)
                if new_run and (not seen_marker or index is not None):
                    prompt_starts.append(len(events))
            in_user = is_user
            previous_index = index if is_user else None
            events.append(event)
    return events


def parse_grok_session_file(file_path: Path, *, skipped: dict[str, int] | None = None) -> SessionRecord | None:
    def skip(reason: str) -> None:
        if skipped is not None:
            skipped[reason] = skipped.get(reason, 0) + 1

    summary_path = file_path.with_name("summary.json")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise ValueError("invalid summary object")
    if summary.get("session_kind") in {"subagent", "subagent_fork"}:
        skip("subagent")
        return None
    info = _object(summary.get("info"), "session info")
    session_id = info.get("id")
    events = _surviving_updates(file_path)
    started = parse_iso_timestamp(str(summary.get("created_at") or ""))
    if started is None:
        first_timestamp = next((timestamp for event in events if (timestamp := _timestamp(event))), None)
        if first_timestamp is not None:
            started = datetime.fromtimestamp(first_timestamp / 1000, timezone.utc)
    if not isinstance(session_id, str) or not session_id:
        skip("missing_session_id")
        return None
    if started is None:
        skip("missing_created_at")
        return None
    messages: list[MessageTurn] = []
    previous_key = None
    for event in events:
        params = _params(event)
        if params.get("sessionId", session_id) != session_id:
            continue
        update = params.get("update") or {}
        tag = update.get("sessionUpdate")
        if event.get("method", "session/update") != "session/update":
            previous_key = None
            continue
        if tag not in {"user_message_chunk", "agent_message_chunk"}:
            if tag != "agent_thought_chunk":
                previous_key = None
            continue
        meta = _object(update.get("_meta"), "chunk metadata")
        content = _object(update.get("content"), "chunk content")
        content_meta = _object(content.get("_meta"), "content metadata")
        if meta.get("hostTurn") or meta.get("hideFromScrollback") or content_meta.get("bash_command"):
            previous_key = None
            continue
        if content.get("type") != "text" or not isinstance(content.get("text"), str):
            continue
        text = content_meta.get("displayText", content["text"])
        if not isinstance(text, str) or not text:
            continue
        role = "user" if tag == "user_message_chunk" else "assistant"
        notification_meta = _object(params.get("_meta"), "notification metadata")
        timestamp = _timestamp(event)
        key = (role, meta.get("promptIndex"), notification_meta.get("promptId"))
        if key == previous_key and messages:
            messages[-1] = messages[-1]._replace(content=messages[-1].content + text)
        else:
            messages.append(MessageTurn(role, text, timestamp))
        previous_key = key
    messages = [message._replace(content=message.content.strip()) for message in messages if message.content.strip()]
    first_user = next((message.content for message in messages if message.role == "user"), "")
    if not first_user:
        skip("no_user_text")
        return None
    title = str(summary.get("generated_title") or summary.get("session_summary") or first_user.splitlines()[0][:120])
    if should_skip_session(title):
        skip("noise_title")
        return None
    # The summary's current model cannot establish the model of earlier turns.
    model = summary.get("current_model_id")
    return SessionRecord("grok", session_id, title, started.date().isoformat(), messages,
                         project_directory=str(info.get("cwd") or ""),
                         models_used=[model] if isinstance(model, str) and model else [])


def export_grok(
    output_dir: Path, state: dict[str, Any], *, full: bool, dry_run: bool,
    since_date: date | None, sessions_dir: Path = DEFAULT_GROK_SESSIONS_DIR,
) -> dict[str, Any]:
    files = sorted(sessions_dir.glob("*/*/updates.jsonl")) if sessions_dir.is_dir() else []
    sessions = state.get("grok", {}).get("sessions", {})
    exported = failed = 0
    skipped: dict[str, int] = {}
    for path in files:
        try:
            record = parse_grok_session_file(path, skipped=skipped)
            if record is None or (since_date and date.fromisoformat(record.date) < since_date):
                continue
            previous = sessions.get(record.session_id, {})
            output = output_dir / previous["output_file"] if previous else unique_output_path(output_dir, record.date, record.title)
            rendered = render_markdown(record)
            if not full and output.is_file() and output.read_text(encoding="utf-8") == rendered:
                continue
            if not dry_run:
                output_dir.mkdir(parents=True, exist_ok=True)
                output.write_text(rendered, encoding="utf-8")
                state.setdefault("grok", {}).setdefault("sessions", {})[record.session_id] = {"output_file": output.name}
            exported += 1
        except (OSError, ValueError, TypeError):
            failed += 1
    result = {"source": "grok", "scanned": len(files), "exported": exported}
    if failed:
        result["failed"] = failed
    if skipped:
        result["skipped"] = skipped
    return result
