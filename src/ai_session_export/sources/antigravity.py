from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import parse_iso_timestamp, unique_output_path


DEFAULT_ANTIGRAVITY_BRAIN_DIRS = {
    "2": Path.home() / ".gemini" / "antigravity" / "brain",
    "ide": Path.home() / ".gemini" / "antigravity-ide" / "brain",
    "cli": Path.home() / ".gemini" / "antigravity-cli" / "brain",
}
# Kept as the IDE root for callers that used the original single-surface API.
DEFAULT_ANTIGRAVITY_BRAIN_DIR = DEFAULT_ANTIGRAVITY_BRAIN_DIRS["ide"]
TRANSCRIPT_RELATIVE_PATH = Path(".system_generated") / "logs" / "transcript_full.jsonl"

USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)


class ParsedAntigravitySession(NamedTuple):
    record: SessionRecord
    latest_timestamp_ms: int


class MalformedTranscriptError(ValueError):
    def __init__(self, line_number: int, message: str) -> None:
        super().__init__(message)
        self.line_number = line_number


def _string_field(step: dict[str, Any], field: str, line_number: int) -> str:
    value = step.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MalformedTranscriptError(line_number, f"Field '{field}' must be a string")
    return value


def _extract_user_text(content: str) -> str:
    """Strip Antigravity XML wrappers, keeping only the inner text of <USER_REQUEST> blocks."""
    matches = USER_REQUEST_RE.findall(content or "")
    if matches:
        return "\n\n".join(match.strip() for match in matches).strip()
    return (content or "").strip()


def _iter_transcript_files(brain_dir: Path) -> list[tuple[str, Path]]:
    """Return (session_id, transcript_path) pairs for each session directory that has a transcript."""
    sessions: list[tuple[str, Path]] = []
    if not brain_dir.is_dir():
        return sessions
    for entry in sorted(brain_dir.iterdir()):
        if not entry.is_dir():
            continue
        transcript = entry / TRANSCRIPT_RELATIVE_PATH
        if transcript.exists():
            sessions.append((entry.name, transcript))
    return sessions


def _parse_transcript(file_path: Path, session_id: str, surface: str) -> ParsedAntigravitySession | None:
    messages: list[MessageTurn] = []
    first_user_text: str | None = None
    started_at: date | None = None
    latest_timestamp_ms = 0

    with file_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                step = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MalformedTranscriptError(line_number, exc.msg) from exc
            if not isinstance(step, dict):
                raise MalformedTranscriptError(line_number, "Expected a JSON object")

            timestamp = parse_iso_timestamp(_string_field(step, "created_at", line_number))
            step_ts_ms = None
            if timestamp is not None:
                step_ts_ms = int(timestamp.timestamp() * 1000)
                latest_timestamp_ms = max(latest_timestamp_ms, step_ts_ms)
                if started_at is None:
                    started_at = timestamp.date()

            step_type = _string_field(step, "type", line_number)
            source = _string_field(step, "source", line_number)

            if step_type == "USER_INPUT" and source == "USER_EXPLICIT":
                content = _string_field(step, "content", line_number)
                text = _extract_user_text(content)
                if not text:
                    continue
                if first_user_text is None:
                    first_user_text = text
                messages.append(MessageTurn(role="user", content=text, time_created=step_ts_ms))
                continue

            if step_type == "PLANNER_RESPONSE" and source == "MODEL":
                content = _string_field(step, "content", line_number)
                text = content.strip()
                if not text:
                    continue
                messages.append(MessageTurn(role="assistant", content=text, time_created=step_ts_ms))

    if not messages or latest_timestamp_ms == 0 or started_at is None:
        return None

    title = (first_user_text or session_id).strip().splitlines()[0][:120] or session_id

    return ParsedAntigravitySession(
        record=SessionRecord(
            source="antigravity",
            surface=surface,
            session_id=session_id,
            title=title,
            date=started_at.isoformat(),
            messages=messages,
            models_used=[],
        ),
        latest_timestamp_ms=latest_timestamp_ms,
    )


def _is_unchanged_session(
    session_state: dict[str, Any], output_dir: Path, source_mtime_ns: int, source_size: int
) -> bool:
    if int(session_state.get("source_mtime_ns", 0)) != source_mtime_ns:
        return False
    if int(session_state.get("source_size", -1)) != source_size:
        return False
    if session_state.get("status") in {"ignored", "legacy_imported"}:
        return True
    output_file = str(session_state.get("output_file") or "")
    return session_state.get("status") == "complete" and bool(output_file) and (output_dir / output_file).is_file()


def export_antigravity(
    output_dir: Path,
    state: dict[str, Any],
    *,
    brain_dir: Path | None = None,
    brain_dirs: Mapping[str, Path] | None = None,
    full: bool,
    dry_run: bool,
    since_date: date | None,
) -> dict[str, Any]:
    """Export Antigravity 2.0, IDE, and CLI transcripts to one Markdown directory.

    ``brain_dir`` retains the original API and treats the supplied root as an IDE
    fixture or override. New callers can pass ``brain_dirs`` keyed by surface.
    """
    if brain_dir is not None and brain_dirs is not None:
        raise ValueError("brain_dir and brain_dirs are mutually exclusive")

    roots: Mapping[str, Path]
    if brain_dirs is not None:
        roots = brain_dirs
    elif brain_dir is not None:
        roots = {"ide": brain_dir}
    else:
        roots = DEFAULT_ANTIGRAVITY_BRAIN_DIRS

    source_state = state.get("antigravity", {}) if dry_run else state.setdefault("antigravity", {})
    legacy_timestamp = int(source_state.get("last_timestamp", 0))
    use_legacy_cursor = legacy_timestamp > 0 and not bool(source_state.get("legacy_cursor_migrated", False))
    surface_states: dict[str, dict[str, Any]] = (
        source_state.get("surfaces", {}) if dry_run else source_state.setdefault("surfaces", {})
    )

    exported = 0
    scanned = 0
    failed = 0
    warnings: list[dict[str, Any]] = []
    surface_results: dict[str, dict[str, int]] = {}
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for surface, root in roots.items():
        counts = {"scanned": 0, "exported": 0, "failed": 0}
        surface_results[surface] = counts
        surface_state = surface_states.get(surface, {}) if dry_run else surface_states.setdefault(surface, {})
        sessions: dict[str, dict[str, Any]] = (
            surface_state.get("sessions", {}) if dry_run else surface_state.setdefault("sessions", {})
        )

        for session_id, transcript in _iter_transcript_files(root):
            scanned += 1
            counts["scanned"] += 1
            stat = transcript.stat()
            previous = sessions.get(session_id, {})
            if not full and _is_unchanged_session(previous, output_dir, stat.st_mtime_ns, stat.st_size):
                continue

            try:
                parsed = _parse_transcript(transcript, session_id, surface)
            except MalformedTranscriptError as exc:
                failed += 1
                counts["failed"] += 1
                warnings.append(
                    {
                        "surface": surface,
                        "line": exc.line_number,
                        "error": str(exc),
                    }
                )
                if not dry_run:
                    sessions[session_id] = {
                        **previous,
                        "status": "failed",
                        "source_mtime_ns": stat.st_mtime_ns,
                        "source_size": stat.st_size,
                        "error_line": exc.line_number,
                    }
                continue

            if parsed is None:
                if not dry_run:
                    sessions[session_id] = {
                        "status": "ignored",
                        "source_mtime_ns": stat.st_mtime_ns,
                        "source_size": stat.st_size,
                    }
                continue
            if since_date and date.fromisoformat(parsed.record.date) < since_date:
                continue

            # The legacy cursor only ever represented the IDE root. Import it
            # without suppressing previously unseen 2.0 or CLI sessions.
            if (
                not full
                and use_legacy_cursor
                and surface == "ide"
                and (not previous or previous.get("status") == "failed")
                and parsed.latest_timestamp_ms <= legacy_timestamp
            ):
                if not dry_run:
                    sessions[session_id] = {
                        "status": "legacy_imported",
                        "latest_timestamp": parsed.latest_timestamp_ms,
                        "source_mtime_ns": stat.st_mtime_ns,
                        "source_size": stat.st_size,
                    }
                continue

            previous_output = str(previous.get("output_file") or "")
            output_path = output_dir / previous_output if previous_output else None
            if output_path is None:
                output_path = unique_output_path(output_dir, parsed.record.date, parsed.record.title)
            if not dry_run:
                output_path.write_text(render_markdown(parsed.record), encoding="utf-8")
                sessions[session_id] = {
                    "status": "complete",
                    "latest_timestamp": parsed.latest_timestamp_ms,
                    "output_file": output_path.name,
                    "source_mtime_ns": stat.st_mtime_ns,
                    "source_size": stat.st_size,
                }
            exported += 1
            counts["exported"] += 1

        if (
            not dry_run
            and surface == "ide"
            and use_legacy_cursor
            and since_date is None
            and counts["scanned"] > 0
            and counts["failed"] == 0
        ):
            source_state["legacy_cursor_migrated"] = True

    return {
        "source": "antigravity",
        "scanned": scanned,
        "exported": exported,
        "failed": failed,
        "surfaces": surface_results,
        "warnings": warnings,
    }
