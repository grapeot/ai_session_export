from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import parse_iso_timestamp, sanitize_filename, should_skip_session, unique_output_path


DEFAULT_CLAUDE_PROJECT_DIRS = (
    Path.home() / ".claude" / "projects",
    Path.home() / ".config" / "claude" / "projects",
    Path.home() / ".config" / "claude-code" / "projects",
)
DEFAULT_CLAUDE_HISTORY_FILES = (
    Path.home() / ".claude" / "history.jsonl",
    Path.home() / ".config" / "claude" / "history.jsonl",
    Path.home() / ".config" / "claude-code" / "history.jsonl",
)

BORING_TITLE_PREFIXES = (
    "/resume",
    "/ide",
    "/model",
    "/mcp",
    "/login",
    "read the full task from",
)


CONTINUATION_SUMMARY_PREFIX = "This session is being continued from a previous conversation"
RECOVERY_DOC_HINT = 'see skill.md "Recovering a truncated Claude Code Remote session"'

SIGNAL_PARENT_UUID_MISSING = "parent_uuid_missing"
SIGNAL_CONTINUATION_SUMMARY = "starts_with_continuation_summary"
SIGNAL_SUBAGENT_EARLIER = "subagent_activity_before_transcript"


class ParsedClaudeSession(NamedTuple):
    record: SessionRecord
    latest_timestamp_ms: int
    first_timestamp_ms: int = 0  # earliest timestamp in the local transcript file
    backfill_mtime_ns: int | None = None  # mtime of the matching backfill file, if any
    warnings: list[dict[str, Any]] = []


def _ms_to_iso_utc(epoch_ms: int) -> str:
    text = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    return text.replace("+00:00", "Z")


def _ms_to_utc_date(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).date().isoformat()


def _earliest_subagent_timestamp_ms(session_file: Path) -> int | None:
    """Earliest timestamp across the session's sibling ``subagents/*.jsonl`` files.

    Subagent transcripts are append-only, so the first timestamped record of each
    file is its earliest; only that prefix of each file is read.
    """
    subagent_dir = session_file.parent / session_file.stem / "subagents"
    if not subagent_dir.is_dir():
        return None
    earliest: int | None = None
    for subagent_file in sorted(subagent_dir.glob("*.jsonl")):
        try:
            with subagent_file.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    value = event.get("timestamp")
                    timestamp = parse_iso_timestamp(value) if isinstance(value, str) else None
                    if timestamp is None:
                        continue
                    ts_ms = int(timestamp.timestamp() * 1000)
                    earliest = ts_ms if earliest is None else min(earliest, ts_ms)
                    break
        except (OSError, UnicodeError):
            continue
    return earliest


def _load_backfill(backfill_file: Path) -> tuple[list[MessageTurn], int]:
    """Read recovered cloud messages. Returns (turns, invalid_line_count)."""
    turns: list[MessageTurn] = []
    invalid = 0
    with backfill_file.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                continue
            if not isinstance(entry, dict):
                invalid += 1
                continue
            created_at = entry.get("created_at")
            text = entry.get("text")
            role = entry.get("role") or "user"
            timestamp = parse_iso_timestamp(created_at) if isinstance(created_at, str) else None
            if timestamp is None or not isinstance(text, str) or role not in {"user", "assistant"}:
                invalid += 1
                continue
            text = text.strip()
            if not text:
                continue
            turns.append(MessageTurn(role=role, content=text, time_created=int(timestamp.timestamp() * 1000)))
    return turns, invalid


def _merge_backfill(backfill: list[MessageTurn], boundary_ms: int) -> list[MessageTurn]:
    """Backfill turns strictly earlier than ``boundary_ms``, sorted and de-duplicated.

    Turns at or after the first local message overlap the local range and are
    dropped: the local transcript wins.
    """
    earlier = sorted(
        (turn for turn in backfill if turn.time_created is not None and turn.time_created < boundary_ms),
        key=lambda turn: turn.time_created or 0,
    )
    seen: set[tuple[int | None, str, str]] = set()
    merged: list[MessageTurn] = []
    for turn in earlier:
        key = (turn.time_created, turn.role, turn.content)
        if key in seen:
            continue
        seen.add(key)
        merged.append(turn)
    return merged


def _iter_session_files(project_dirs: tuple[Path, ...]) -> list[Path]:
    files: list[Path] = []
    for root in project_dirs:
        if not root.exists():
            continue
        for file_path in root.rglob("*.jsonl"):
            if "subagents" in file_path.parts:
                continue
            files.append(file_path)
    return sorted(files)


def _load_history_titles(history_files: tuple[Path, ...]) -> dict[str, list[tuple[int, str]]]:
    titles: dict[str, list[tuple[int, str]]] = {}
    for history_file in history_files:
        if not history_file.exists():
            continue
        with history_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                session_id = entry.get("sessionId")
                display = (entry.get("display") or "").strip()
                timestamp = int(entry.get("timestamp") or 0)
                if not session_id or not display:
                    continue
                titles.setdefault(session_id, []).append((timestamp, display))
    for values in titles.values():
        values.sort(key=lambda item: item[0])
    return titles


def _extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
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


def _is_meaningful_title(display: str) -> bool:
    text = display.strip().lower()
    if not text:
        return False
    return not any(text.startswith(prefix) for prefix in BORING_TITLE_PREFIXES)


def _choose_title(session_id: str, history_titles: dict[str, list[tuple[int, str]]], first_user_text: str | None) -> str:
    for _, display in history_titles.get(session_id, []):
        if _is_meaningful_title(display):
            return display.strip()
    if first_user_text:
        return first_user_text.strip().splitlines()[0][:120]
    return session_id


def parse_claude_session_file(
    file_path: Path,
    history_titles: dict[str, list[tuple[int, str]]],
    *,
    backfill_dir: Path | None = None,
) -> ParsedClaudeSession | None:
    session_id = ""
    cwd = ""
    models: set[str] = set()
    messages: list[MessageTurn] = []
    pending_user_turns: list[int] = []
    first_user_text: str | None = None
    started_at: date | None = None
    latest_timestamp_ms = 0
    first_timestamp_ms = 0
    uuids: set[str] = set()
    first_parent_uuid: str | None = None
    seen_uuid_record = False

    with file_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            event = json.loads(line)
            record_uuid = event.get("uuid")
            if isinstance(record_uuid, str) and record_uuid:
                uuids.add(record_uuid)
                if not seen_uuid_record:
                    seen_uuid_record = True
                    parent = event.get("parentUuid")
                    first_parent_uuid = parent if isinstance(parent, str) and parent else None
            raw_timestamp = event.get("timestamp")
            if isinstance(raw_timestamp, str):
                any_timestamp = parse_iso_timestamp(raw_timestamp)
                if any_timestamp is not None:
                    any_ts_ms = int(any_timestamp.timestamp() * 1000)
                    if not first_timestamp_ms or any_ts_ms < first_timestamp_ms:
                        first_timestamp_ms = any_ts_ms
            if event.get("isSidechain") is True:
                continue
            timestamp = parse_iso_timestamp(event.get("timestamp", ""))
            event_ts_ms = None
            if timestamp is not None:
                event_ts_ms = int(timestamp.timestamp() * 1000)
                latest_timestamp_ms = max(latest_timestamp_ms, event_ts_ms)
                if started_at is None:
                    started_at = timestamp.date()
            session_id = event.get("sessionId") or session_id
            cwd = event.get("cwd") or cwd

            if event.get("type") == "user":
                text = _extract_text_content((event.get("message") or {}).get("content"))
                if not text:
                    continue
                if first_user_text is None:
                    first_user_text = text
                messages.append(MessageTurn(role="user", content=text, time_created=event_ts_ms))
                pending_user_turns.append(len(messages) - 1)
                continue

            if event.get("type") == "assistant":
                message = event.get("message") or {}
                model = (message.get("model") or "").strip()
                if model:
                    models.add(model)
                    for index in pending_user_turns:
                        messages[index] = messages[index]._replace(model=model)
                pending_user_turns.clear()
                text = _extract_text_content(message.get("content"))
                if text:
                    messages.append(
                        MessageTurn(role="assistant", content=text, time_created=event_ts_ms, model=model or None)
                    )

    if not session_id or not messages or latest_timestamp_ms == 0 or started_at is None:
        return None

    signals: list[str] = []
    if first_parent_uuid is not None and first_parent_uuid not in uuids:
        signals.append(SIGNAL_PARENT_UUID_MISSING)
    if first_user_text is not None and first_user_text.startswith(CONTINUATION_SUMMARY_PREFIX):
        signals.append(SIGNAL_CONTINUATION_SUMMARY)
    earliest_known_activity = ""
    earliest_subagent_ms = _earliest_subagent_timestamp_ms(file_path)
    if earliest_subagent_ms is not None and first_timestamp_ms and earliest_subagent_ms < first_timestamp_ms:
        signals.append(SIGNAL_SUBAGENT_EARLIER)
        earliest_known_activity = _ms_to_iso_utc(earliest_subagent_ms)

    warnings: list[dict[str, Any]] = []
    session_date = started_at.isoformat()
    backfilled: list[MessageTurn] = []
    backfill_mtime_ns: int | None = None
    if backfill_dir is not None:
        backfill_file = backfill_dir / f"{session_id}.jsonl"
        if backfill_file.is_file():
            backfill_mtime_ns = backfill_file.stat().st_mtime_ns
            recovered, invalid = _load_backfill(backfill_file)
            if invalid:
                warnings.append(
                    {"session_id": session_id, "error": f"skipped {invalid} invalid backfill line(s)"}
                )
            boundary_ms = messages[0].time_created or first_timestamp_ms
            backfilled = _merge_backfill(recovered, boundary_ms)
            if backfilled:
                messages = backfilled + messages
                session_date = min(session_date, _ms_to_utc_date(backfilled[0].time_created or boundary_ms))
                first_backfilled_user = next((turn.content for turn in backfilled if turn.role == "user"), None)
                if first_backfilled_user is not None:
                    first_user_text = first_backfilled_user

    title = _choose_title(session_id, history_titles, first_user_text)
    if should_skip_session(title):
        return None

    if signals:
        warnings.append(
            {
                "session_id": session_id,
                "error": (
                    f"transcript truncated (local transcript starts at {_ms_to_iso_utc(first_timestamp_ms)}; "
                    f"signals: {', '.join(signals)}"
                    + (f"; backfilled_messages: {len(backfilled)}" if backfilled else "")
                    + f"); {RECOVERY_DOC_HINT}"
                ),
            }
        )

    return ParsedClaudeSession(
        record=SessionRecord(
            source="claude_code",
            session_id=session_id,
            title=title,
            date=session_date,
            messages=messages,
            project_directory=cwd,
            models_used=sorted(models),
            truncation_signals=signals,
            earliest_known_activity=earliest_known_activity,
            backfilled_messages=len(backfilled),
        ),
        latest_timestamp_ms=latest_timestamp_ms,
        first_timestamp_ms=first_timestamp_ms,
        backfill_mtime_ns=backfill_mtime_ns,
        warnings=warnings,
    )


def export_claude_code(
    output_dir: Path,
    state: dict[str, Any],
    *,
    full: bool,
    dry_run: bool,
    since_date: date | None,
    project_dirs: tuple[Path, ...] = DEFAULT_CLAUDE_PROJECT_DIRS,
    history_files: tuple[Path, ...] = DEFAULT_CLAUDE_HISTORY_FILES,
    backfill_dir: Path | None = None,
) -> dict[str, Any]:
    history_titles = _load_history_titles(history_files)
    session_files = _iter_session_files(project_dirs)
    source_state = state.get("claude_code", {}) if dry_run else state.setdefault("claude_code", {})
    sessions = source_state.get("sessions", {}) if dry_run else source_state.setdefault("sessions", {})
    identity_index: dict[str, list[str]] | None = None

    exported = 0
    scanned = 0
    latest_seen = 0
    warnings: list[dict[str, Any]] = []
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for file_path in session_files:
        parsed = parse_claude_session_file(file_path, history_titles, backfill_dir=backfill_dir)
        if parsed is None:
            continue
        scanned += 1
        latest_seen = max(latest_seen, parsed.latest_timestamp_ms)
        if since_date and date.fromisoformat(parsed.record.date) < since_date:
            continue

        session_id = parsed.record.session_id
        previous = sessions.get(session_id, {})
        previous_timestamp = int(previous.get("latest_timestamp", 0))
        previous_output = str(previous.get("output_file") or "")
        output_path = output_dir / previous_output if previous_output else None
        output_exists = output_path is not None and output_path.is_file()
        previous_backfill = previous.get("backfill_mtime_ns")
        backfill_unchanged = parsed.backfill_mtime_ns == (int(previous_backfill) if previous_backfill else None)
        if not full and parsed.latest_timestamp_ms <= previous_timestamp and output_exists and backfill_unchanged:
            continue

        if not previous_output:
            if identity_index is None:
                identity_index = {}
                for archive in output_dir.glob("*.md"):
                    try:
                        with archive.open("r", encoding="utf-8") as handle:
                            if handle.readline().strip() != "---":
                                continue
                            metadata = {}
                            for line in handle:
                                if line.strip() == "---":
                                    break
                                key, _, value = line.partition(":")
                                if key in {"source", "session_id"}:
                                    value = value.strip()
                                    metadata[key] = json.loads(value) if value.startswith('"') else value
                            else:
                                continue
                        archive_id = metadata.get("session_id")
                        if metadata.get("source") == "claude_code" and isinstance(archive_id, str) and archive_id:
                            identity_index.setdefault(archive_id, []).append(archive.name)
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        continue
            candidates = identity_index.get(session_id, [])
            if candidates:
                canonical = f"{parsed.record.date.replace('-', '')}_{sanitize_filename(parsed.record.title)}.md"
                output_path = output_dir / min(
                    candidates,
                    key=lambda name: (
                        name != canonical,
                        int(Path(name).stem.rsplit("_", 1)[-1])
                        if Path(name).stem.rsplit("_", 1)[-1].isdigit() else 1,
                        name,
                    ),
                )
            else:
                output_path = unique_output_path(output_dir, parsed.record.date, parsed.record.title)
        if not dry_run:
            file_mtime_ns = file_path.stat().st_mtime_ns
            output_path.write_text(render_markdown(parsed.record), encoding="utf-8")
            session_state = {
                "latest_timestamp": parsed.latest_timestamp_ms,
                "output_file": output_path.name,
                "source_mtime_ns": file_mtime_ns,
            }
            if parsed.backfill_mtime_ns is not None:
                session_state["backfill_mtime_ns"] = parsed.backfill_mtime_ns
            sessions[session_id] = session_state
        exported += 1
        warnings.extend(parsed.warnings)

    result: dict[str, Any] = {"source": "claude_code", "scanned": scanned, "exported": exported, "latest_seen": latest_seen}
    if warnings:
        result["warnings"] = warnings
    return result
