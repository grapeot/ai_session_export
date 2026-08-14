from __future__ import annotations

import json
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import ms_to_date, should_skip_session, unique_output_path


DEFAULT_DSH_SESSIONS_DIR = Path.home() / ".dsh" / "sessions"
SYSTEM_REMINDER_PATTERN = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


class ParsedDshSession(NamedTuple):
    record: SessionRecord
    latest_timestamp_ms: int


def _iter_session_files(sessions_dir: Path) -> list[Path]:
    """DSH materializes one directory per session under per-workspace roots.

    Directory ids are not a single namespace: top-level sessions use
    `session-<uuid>` while subagent children may use a bare uuid, so discovery
    must not filter on a name prefix. Both physical encodings are accepted:
    `session.jsonl.zstd` (default checksummed Zstandard frames) and plain
    `session.jsonl`.
    """
    if not sessions_dir.is_dir():
        return []
    return sorted(sessions_dir.glob("*/*/session.jsonl*"))


def _read_session_text(file_path: Path) -> str:
    if file_path.suffix == ".zstd":
        completed = subprocess.run(
            ["zstd", "-d", "-c", "--", str(file_path)],
            capture_output=True,
            check=False,
        )
        # A torn final frame (crash or mid-append) still emits the complete
        # earlier frames on stdout with a nonzero exit; only a fully unusable
        # artifact produces no output and becomes an isolated failure.
        if completed.returncode != 0 and not completed.stdout:
            raise RuntimeError(f"zstd exited {completed.returncode} with no decompressed output")
        return completed.stdout.decode("utf-8", errors="replace")
    return file_path.read_text(encoding="utf-8")


def _extract_text_content(content: Any) -> str:
    """Keep `type: "text"` items only; reasoning blocks stay out of the archive."""
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


def _strip_system_reminders(text: str) -> str:
    """Drop machine-injected workspace instructions; they are not user speech."""
    return SYSTEM_REMINDER_PATTERN.sub("", text).strip()


def _format_model(provider: str, model: str) -> str:
    if provider and model:
        return f"{provider}/{model}"
    return model


def _model_from_request_header(header: Any) -> str:
    """Model attribution from the request-context event; sparse in long sessions."""
    config = (header or {}).get("config") if isinstance(header, dict) else None
    config = config if isinstance(config, dict) else {}
    return _format_model(
        str(config.get("provider") or "").strip(),
        str(config.get("model") or "").strip(),
    )


def _model_from_message_source(source: Any) -> str:
    """Per-message attribution; present on every real `assistant/message`."""
    if not isinstance(source, dict):
        return ""
    return _format_model(
        str(source.get("provider") or "").strip(),
        str(source.get("model") or "").strip(),
    )


def parse_dsh_session_file(file_path: Path) -> ParsedDshSession | None:
    session_id = ""
    cwd = ""
    origin = ""
    created_at_ms = 0
    title = ""
    first_user_text: str | None = None
    models: set[str] = set()
    current_model: str | None = None
    messages: list[MessageTurn] = []
    pending_user_turns: list[int] = []
    latest_timestamp_ms = 0

    for raw_line in _read_session_text(file_path).splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            # Live sessions are appended in durable batches; a torn final
            # record is discarded rather than failing the whole session.
            continue

        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        raw_time = event.get("time")
        event_time_ms = raw_time if isinstance(raw_time, int) and not isinstance(raw_time, bool) else 0
        if event_time_ms:
            latest_timestamp_ms = max(latest_timestamp_ms, event_time_ms)
        data = event.get("data")
        data = data if isinstance(data, dict) else {}

        if event_type == "session":
            session_id = str(event.get("id") or session_id)
            cwd = str(event.get("cwd") or cwd)
            origin = str(event.get("origin") or "")
            created_at_ms = int(event.get("createdAt") or 0)
            continue

        if event_type == "session/title":
            candidate = str(data.get("title") or "").strip()
            if candidate:
                # Later titles win: LLM-generated titles follow the fallback.
                title = candidate
            continue

        if event_type == "request/header":
            model = _model_from_request_header(data.get("header"))
            if model:
                current_model = model
                models.add(model)
                for index in pending_user_turns:
                    messages[index] = messages[index]._replace(model=model)
                pending_user_turns.clear()
            continue

        if event_type == "user/message":
            text = _strip_system_reminders(_extract_text_content(data.get("content")))
            if not text:
                continue
            if first_user_text is None:
                first_user_text = text
            messages.append(
                MessageTurn(role="user", content=text, time_created=event_time_ms or None, model=current_model)
            )
            pending_user_turns.append(len(messages) - 1)
            continue

        if event_type == "assistant/message":
            message = data.get("message")
            message = message if isinstance(message, dict) else {}
            model = _model_from_message_source(message.get("source"))
            if model:
                current_model = model
                models.add(model)
                for index in pending_user_turns:
                    messages[index] = messages[index]._replace(model=model)
            pending_user_turns.clear()
            text = _extract_text_content(message.get("content"))
            if not text:
                continue
            messages.append(
                MessageTurn(
                    role="assistant",
                    content=text,
                    time_created=event_time_ms or None,
                    model=model or current_model,
                )
            )

    if not session_id or origin == "subagent" or not messages:
        return None
    if not created_at_ms and not latest_timestamp_ms:
        # Without any timestamp the session cannot be placed in the archive.
        return None

    if not title and first_user_text:
        title = first_user_text.splitlines()[0][:120]
    title = title or session_id
    if should_skip_session(title):
        return None

    return ParsedDshSession(
        record=SessionRecord(
            source="dsh",
            session_id=session_id,
            title=title,
            date=ms_to_date(created_at_ms or latest_timestamp_ms),
            messages=messages,
            project_directory=cwd,
            models_used=sorted(models),
        ),
        latest_timestamp_ms=latest_timestamp_ms,
    )


def export_dsh(
    output_dir: Path,
    state: dict[str, Any],
    *,
    full: bool,
    dry_run: bool,
    since_date: date | None,
    sessions_dir: Path = DEFAULT_DSH_SESSIONS_DIR,
) -> dict[str, Any]:
    session_files = _iter_session_files(sessions_dir)
    source_state = state.setdefault("dsh", {})
    session_state: dict[str, dict[str, Any]] = source_state.setdefault("sessions", {})

    exported = 0
    scanned = 0
    failed = 0
    warnings: list[dict[str, Any]] = []
    seen_session_ids: set[str] = set()
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for file_path in session_files:
        scanned += 1
        try:
            file_mtime_ns = file_path.stat().st_mtime_ns
        except OSError:
            # The file vanished between discovery and stat.
            continue
        path_session_id = file_path.parent.name
        path_state = session_state.get(path_session_id, {})
        path_output = str(path_state.get("output_file") or "")
        path_output_exists = bool(path_output) and (output_dir / path_output).is_file()
        if (
            not full
            and path_session_id
            and int(path_state.get("source_mtime_ns", 0)) == file_mtime_ns
            and path_output_exists
        ):
            continue

        try:
            parsed = parse_dsh_session_file(file_path)
        except Exception as error:  # noqa: BLE001 - isolate one unreadable session
            failed += 1
            warnings.append(
                {"surface": "parse", "line": 0, "error": f"{type(error).__name__}: {error}"}
            )
            continue
        if parsed is None:
            continue
        if parsed.record.session_id in seen_session_ids:
            # The same session directory may hold both encodings during a
            # configuration change; export each session once.
            continue
        seen_session_ids.add(parsed.record.session_id)
        if since_date and date.fromisoformat(parsed.record.date) < since_date:
            continue

        record = parsed.record
        previous = session_state.get(record.session_id, {})
        previous_timestamp = int(previous.get("latest_timestamp", 0))
        previous_output = str(previous.get("output_file") or "")
        output_path = output_dir / previous_output if previous_output else None
        output_exists = output_path is not None and output_path.is_file()
        if not full and parsed.latest_timestamp_ms <= previous_timestamp and output_exists:
            if not dry_run:
                previous["source_mtime_ns"] = file_mtime_ns
                session_state[record.session_id] = previous
            continue

        if output_path is None:
            output_path = unique_output_path(output_dir, record.date, record.title)
        if not dry_run:
            output_path.write_text(render_markdown(record), encoding="utf-8")
            session_state[record.session_id] = {
                "latest_timestamp": parsed.latest_timestamp_ms,
                "output_file": output_path.name,
                "source_mtime_ns": file_mtime_ns,
            }
        exported += 1

    result: dict[str, Any] = {"source": "dsh", "scanned": scanned, "exported": exported}
    if failed:
        result["failed"] = failed
        result["warnings"] = warnings
    return result
