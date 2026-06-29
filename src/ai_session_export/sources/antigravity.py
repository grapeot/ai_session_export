from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import parse_iso_timestamp, unique_output_path


DEFAULT_ANTIGRAVITY_BRAIN_DIR = Path.home() / ".gemini" / "antigravity-ide" / "brain"
TRANSCRIPT_RELATIVE_PATH = Path(".system_generated") / "logs" / "transcript_full.jsonl"

USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)


class ParsedAntigravitySession(NamedTuple):
    record: SessionRecord
    latest_timestamp_ms: int


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


def _parse_transcript(file_path: Path, session_id: str) -> ParsedAntigravitySession | None:
    messages: list[MessageTurn] = []
    first_user_text: str | None = None
    started_at: date | None = None
    latest_timestamp_ms = 0

    with file_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            step = json.loads(line)

            timestamp = parse_iso_timestamp(step.get("created_at") or "")
            step_ts_ms = None
            if timestamp is not None:
                step_ts_ms = int(timestamp.timestamp() * 1000)
                latest_timestamp_ms = max(latest_timestamp_ms, step_ts_ms)
                if started_at is None:
                    started_at = timestamp.date()

            step_type = step.get("type")
            source = step.get("source")

            if step_type == "USER_INPUT" and source == "USER_EXPLICIT":
                text = _extract_user_text(step.get("content") or "")
                if not text:
                    continue
                if first_user_text is None:
                    first_user_text = text
                messages.append(MessageTurn(role="user", content=text, time_created=step_ts_ms))
                continue

            if step_type == "PLANNER_RESPONSE" and source == "MODEL":
                text = (step.get("content") or "").strip()
                if not text:
                    continue
                messages.append(MessageTurn(role="assistant", content=text, time_created=step_ts_ms))

    if not messages or latest_timestamp_ms == 0 or started_at is None:
        return None

    title = (first_user_text or session_id).strip().splitlines()[0][:120] or session_id

    return ParsedAntigravitySession(
        record=SessionRecord(
            source="antigravity",
            session_id=session_id,
            title=title,
            date=started_at.isoformat(),
            messages=messages,
            models_used=[],
        ),
        latest_timestamp_ms=latest_timestamp_ms,
    )


def export_antigravity(
    output_dir: Path,
    state: dict[str, Any],
    *,
    brain_dir: Path = DEFAULT_ANTIGRAVITY_BRAIN_DIR,
    full: bool,
    dry_run: bool,
    since_date: date | None,
) -> dict[str, Any]:
    """Export Antigravity IDE transcripts under ``brain_dir`` to markdown files in ``output_dir``.

    Each subdirectory of ``brain_dir`` is treated as one session (its UUID name is the
    session id) and parsed from its ``transcript_full.jsonl`` log. Only explicit user
    input and model planner responses are kept; tool calls, thinking, and other
    ephemeral steps are dropped.
    """
    last_timestamp = int(state.get("antigravity", {}).get("last_timestamp", 0))

    exported = 0
    scanned = 0
    latest_seen = last_timestamp
    output_dir.mkdir(parents=True, exist_ok=True)

    for session_id, transcript in _iter_transcript_files(brain_dir):
        parsed = _parse_transcript(transcript, session_id)
        if parsed is None:
            continue
        scanned += 1
        latest_seen = max(latest_seen, parsed.latest_timestamp_ms)
        if not full and parsed.latest_timestamp_ms <= last_timestamp:
            continue
        if since_date and date.fromisoformat(parsed.record.date) < since_date:
            continue
        output_path = unique_output_path(output_dir, parsed.record.date, parsed.record.title)
        if not dry_run:
            output_path.write_text(render_markdown(parsed.record), encoding="utf-8")
        exported += 1

    if not dry_run:
        state.setdefault("antigravity", {})["last_timestamp"] = latest_seen

    return {"source": "antigravity", "scanned": scanned, "exported": exported, "latest_seen": latest_seen}
