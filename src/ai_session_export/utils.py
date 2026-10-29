from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path


SUBAGENT_PATTERNS = (
    "@explore subagent",
    "@librarian subagent",
    "@oracle subagent",
    "@general subagent",
    "@hephaestus subagent",
    "@metis subagent",
    "@momus subagent",
)
SYSTEM_PREFIXES = ("find ", "search ", "explore ")


def sanitize_filename(title: str, max_length: int = 80) -> str:
    text = (title or "").strip()
    text = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        text = "untitled"
    return text[:max_length]


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def unique_output_path(output_dir: Path, date_ymd: str, title: str) -> Path:
    prefix = date_ymd.replace("-", "")
    stem = f"{prefix}_{sanitize_filename(title)}"
    candidate = output_dir / f"{stem}.md"
    counter = 2
    while candidate.exists():
        candidate = output_dir / f"{stem}_{counter}.md"
        counter += 1
    return candidate


def archived_session_id(path: Path) -> str | None:
    """Return the session_id recorded in a rendered archive file, if parseable.

    Archive files carry their session id in the YAML frontmatter. Unparsable or
    unrelated files return None so callers can treat them as foreign sessions
    and never overwrite them.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            head = handle.read(4096)
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    end = head.find("---", 3)
    if end == -1:
        return None
    match = re.search(r"^session_id:\s*(.+)$", head[:end], re.MULTILINE)
    if not match:
        return None
    try:
        return str(json.loads(match.group(1).strip()))
    except json.JSONDecodeError:
        return match.group(1).strip().strip('"')


def session_aware_output_path(
    output_dir: Path,
    session_id: str,
    date_ymd: str,
    title: str,
    max_candidates: int = 512,
) -> Path:
    """Resolve the archive path for a session that may already be exported.

    Sessions no longer follow create-once semantics: a long-lived session keeps
    growing after its first export. Re-emitting it must update the file it
    already owns instead of creating an ``_2`` duplicate. The same-session check
    is the frontmatter ``session_id``; a different session keeps its own file
    and a new session gets the next unique number (identical numbering to
    :func:`unique_output_path`).
    """
    prefix = date_ymd.replace("-", "")
    stem = f"{prefix}_{sanitize_filename(title)}"
    candidate = output_dir / f"{stem}.md"
    counter = 2
    while candidate.exists() and counter <= max_candidates:
        if archived_session_id(candidate) == session_id:
            return candidate
        candidate = output_dir / f"{stem}_{counter}.md"
        counter += 1
    return candidate


def should_skip_session(title: str | None) -> bool:
    text = (title or "").strip().lower()
    if not text:
        return False
    if any(pattern in text for pattern in SUBAGENT_PATTERNS):
        return True
    if text.startswith(SYSTEM_PREFIXES) and "subagent" in text:
        return True
    return False


def parse_second_mind_date(created_at: str) -> str:
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        dt = datetime.fromisoformat(created_at)
    return dt.date().isoformat()


def ms_to_date(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000).date().isoformat()


def ms_to_hhmm(epoch_ms: int) -> str:
    """Local-time HH:MM for a ms epoch (used in per-turn markdown headers)."""
    return datetime.fromtimestamp(epoch_ms / 1000).strftime("%H:%M")


def to_local_date(value: datetime) -> date:
    """Session date in local time, matching the local HH:MM turn headers.

    Agent transcripts usually store timestamps as UTC (``...Z``). Using the UTC
    date while rendering local turn times files evening sessions under the next
    day, so downstream window filters (e.g. eink_diary) replay yesterday's work.
    Naive values are treated as already-local wall-clock time.
    """
    if value.tzinfo is not None:
        return value.astimezone().date()
    return value.date()


def resolve_output_path(
    output_dir: Path, previous_output: str, date_ymd: str, title: str
) -> Path:
    """Keep an existing archive file, renaming it when its date prefix is stale."""
    if previous_output:
        previous = output_dir / Path(previous_output).name
        if not previous.is_file():
            return previous
        expected_prefix = date_ymd.replace("-", "") + "_"
        if previous.name.startswith(expected_prefix):
            return previous
        renamed = unique_output_path(output_dir, date_ymd, title)
        previous.replace(renamed)
        return renamed
    return unique_output_path(output_dir, date_ymd, title)


def parse_iso_timestamp(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def date_from_cli(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()
