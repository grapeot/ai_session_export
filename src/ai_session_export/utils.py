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
