from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import unquote, urlparse

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import parse_iso_timestamp, resolve_output_path, to_local_date, unique_output_path


DEFAULT_ANTIGRAVITY_BRAIN_DIRS = {
    "2": Path.home() / ".gemini" / "antigravity" / "brain",
    "ide": Path.home() / ".gemini" / "antigravity-ide" / "brain",
    "cli": Path.home() / ".gemini" / "antigravity-cli" / "brain",
}
# Kept as the IDE root for callers that used the original single-surface API.
DEFAULT_ANTIGRAVITY_BRAIN_DIR = DEFAULT_ANTIGRAVITY_BRAIN_DIRS["ide"]
TRANSCRIPT_RELATIVE_PATH = Path(".system_generated") / "logs" / "transcript_full.jsonl"
COMPACT_TRANSCRIPT_RELATIVE_PATH = Path(".system_generated") / "logs" / "transcript.jsonl"

USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)
PBTXT_QUOTED_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')
PBTXT_TITLE_RE = re.compile(r'\btitle:\s*"((?:\\.|[^"\\])*)"')
PBTXT_ARCHIVED_RE = re.compile(r"\barchived:\s*(true|false)\b")
PBTXT_ARCHIVAL_TS_RE = re.compile(
    r"\barchival_status_timestamp:\s*\{\s*seconds:\s*(\d+)(?:\s+nanos:\s*(\d+))?\s*\}"
)


class ParsedAntigravitySession(NamedTuple):
    record: SessionRecord
    latest_timestamp_ms: int


class AntigravitySessionMetadata(NamedTuple):
    session_id: str
    surface: str
    title: str = ""
    project_directory: str = ""
    archived: bool = False
    archival_status_timestamp_s: int | None = None
    step_count: int = 0
    last_modified_time: str = ""
    parent_conversation_id: str = ""
    has_transcript: bool = False
    transcript_path: Path | None = None


class MalformedTranscriptError(ValueError):
    def __init__(self, line_number: int, message: str) -> None:
        super().__init__(message)
        self.line_number = line_number


def _resolve_roots(
    brain_dir: Path | None = None,
    brain_dirs: Mapping[str, Path] | None = None,
) -> Mapping[str, Path]:
    if brain_dir is not None and brain_dirs is not None:
        raise ValueError("brain_dir and brain_dirs are mutually exclusive")
    if brain_dirs is not None:
        return brain_dirs
    if brain_dir is not None:
        return {"ide": brain_dir}
    return DEFAULT_ANTIGRAVITY_BRAIN_DIRS


def _uri_to_path(uri_or_path: str) -> str:
    raw = (uri_or_path or "").strip()
    if not raw:
        return ""
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        return unquote(parsed.path or "")
    return raw


def _parse_workspace_uris(raw_uris: str) -> str:
    text = (raw_uris or "").strip()
    if not text:
        return ""
    if text.startswith("["):
        try:
            items = json.loads(text)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, str) and item.strip():
                        resolved = _uri_to_path(item)
                        if resolved:
                            return resolved
        except json.JSONDecodeError:
            pass
    return _uri_to_path(text)


# --- Minimal Protobuf Wire Format Helpers (stdlib only) ---


def _decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("Truncated protobuf varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift >= 64:
            raise ValueError("Protobuf varint too long")


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("Negative varint not supported")
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _iter_proto_fields(buf: bytes) -> list[tuple[int, int, bytes, bytes]]:
    """Return list of (field_number, wire_type, raw_tag_and_payload, value_bytes_or_varint_bytes)."""
    fields: list[tuple[int, int, bytes, bytes]] = []
    pos = 0
    n = len(buf)
    while pos < n:
        start = pos
        tag, pos = _decode_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num == 0:
            raise ValueError("Invalid protobuf field number 0")
        if wire_type == 0:
            val_start = pos
            _, pos = _decode_varint(buf, pos)
            fields.append((field_num, wire_type, buf[start:pos], buf[val_start:pos]))
        elif wire_type == 1:
            if pos + 8 > n:
                raise ValueError("Truncated 64-bit protobuf field")
            val = buf[pos : pos + 8]
            pos += 8
            fields.append((field_num, wire_type, buf[start:pos], val))
        elif wire_type == 2:
            length, pos = _decode_varint(buf, pos)
            if pos + length > n:
                raise ValueError("Truncated length-delimited protobuf field")
            val = buf[pos : pos + length]
            pos += length
            fields.append((field_num, wire_type, buf[start:pos], val))
        elif wire_type == 5:
            if pos + 4 > n:
                raise ValueError("Truncated 32-bit protobuf field")
            val = buf[pos : pos + 4]
            pos += 4
            fields.append((field_num, wire_type, buf[start:pos], val))
        else:
            raise ValueError(f"Unsupported protobuf wire type: {wire_type}")
    return fields


def _encode_length_delimited(field_num: int, payload: bytes) -> bytes:
    tag = (field_num << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(payload)) + payload


def _encode_varint_field(field_num: int, value: int) -> bytes:
    tag = (field_num << 3) | 0
    return _encode_varint(tag) + _encode_varint(value)


def _encode_timestamp_message(seconds: int, nanos: int = 0) -> bytes:
    parts = [_encode_varint_field(1, seconds)]
    if nanos > 0:
        parts.append(_encode_varint_field(2, nanos))
    return b"".join(parts)


def _parse_timestamp_message(payload: bytes) -> int | None:
    try:
        for field_num, wire_type, _, val in _iter_proto_fields(payload):
            if field_num == 1 and wire_type == 0:
                sec, _ = _decode_varint(val, 0)
                return sec
    except ValueError:
        return None
    return None


def _parse_annotations_proto(payload: bytes) -> dict[str, Any]:
    """Parse .exa.jetski_cortex_pb.ConversationAnnotations binary payload."""
    info: dict[str, Any] = {}
    try:
        for field_num, wire_type, _, val in _iter_proto_fields(payload):
            if field_num == 1 and wire_type == 2:
                title = val.decode("utf-8", errors="replace").strip()
                if title:
                    info["title"] = title
            elif field_num == 4 and wire_type == 0:
                archived_val, _ = _decode_varint(val, 0)
                info["archived"] = bool(archived_val)
            elif field_num == 5 and wire_type == 2:
                ts = _parse_timestamp_message(val)
                if ts is not None:
                    info["archival_status_timestamp_s"] = ts
    except ValueError:
        pass
    return info


def _parse_raw_summary_proto(raw_summary: bytes | None) -> dict[str, Any]:
    """Parse .exa.jetski_cortex_pb.CascadeTrajectorySummary binary payload."""
    if not raw_summary:
        return {}
    info: dict[str, Any] = {}
    try:
        for field_num, wire_type, _, val in _iter_proto_fields(raw_summary):
            if field_num == 1 and wire_type == 2:
                summary = val.decode("utf-8", errors="replace").strip()
                if summary:
                    info["summary"] = summary
            elif field_num == 2 and wire_type == 0:
                step_count, _ = _decode_varint(val, 0)
                info["step_count"] = step_count
            elif field_num == 9 and wire_type == 2 and "project_directory" not in info:
                for wf_num, wf_wt, _, wf_val in _iter_proto_fields(val):
                    if wf_num == 1 and wf_wt == 2:
                        uri = wf_val.decode("utf-8", errors="replace").strip()
                        resolved = _uri_to_path(uri)
                        if resolved:
                            info["project_directory"] = resolved
                            break
            elif field_num == 15 and wire_type == 2:
                ann = _parse_annotations_proto(val)
                info["annotations"] = ann
    except ValueError:
        pass
    return info


def _update_annotations_proto_archived(
    existing_payload: bytes, archived: bool, timestamp_s: int, timestamp_ns: int = 0
) -> bytes:
    kept: list[bytes] = []
    for field_num, _, raw_chunk, _ in _iter_proto_fields(existing_payload):
        if field_num in {4, 5}:
            continue
        kept.append(raw_chunk)
    kept.append(_encode_varint_field(4, 1 if archived else 0))
    kept.append(_encode_length_delimited(5, _encode_timestamp_message(timestamp_s, timestamp_ns)))
    return b"".join(kept)


def _update_raw_summary_archived(
    raw_summary: bytes, archived: bool, timestamp_s: int, timestamp_ns: int = 0
) -> bytes:
    fields = _iter_proto_fields(raw_summary)
    out: list[bytes] = []
    updated_annotations = False
    for field_num, wire_type, raw_chunk, val in fields:
        if field_num == 15 and wire_type == 2:
            new_ann = _update_annotations_proto_archived(val, archived, timestamp_s, timestamp_ns)
            out.append(_encode_length_delimited(15, new_ann))
            updated_annotations = True
        else:
            out.append(raw_chunk)
    if not updated_annotations:
        new_ann = _update_annotations_proto_archived(b"", archived, timestamp_s, timestamp_ns)
        out.append(_encode_length_delimited(15, new_ann))
    return b"".join(out)


def _update_agyhub_proto_archived(
    proto_bytes: bytes, session_id: str, archived: bool, timestamp_s: int, timestamp_ns: int = 0
) -> tuple[bytes, bool]:
    """Update a session's CascadeTrajectorySummary entry inside agyhub_summaries_proto.pb."""
    fields = _iter_proto_fields(proto_bytes)
    out: list[bytes] = []
    matched = False
    target_id_bytes = session_id.encode("utf-8")
    for field_num, wire_type, raw_chunk, val in fields:
        if field_num == 1 and wire_type == 2:
            entry_fields = _iter_proto_fields(val)
            entry_id = None
            for ef_num, ef_wt, _, ef_val in entry_fields:
                if ef_num == 1 and ef_wt == 2:
                    entry_id = ef_val
                    break
            if entry_id == target_id_bytes:
                new_entry_parts: list[bytes] = []
                for ef_num, ef_wt, ef_raw, ef_val in entry_fields:
                    if ef_num == 2 and ef_wt == 2:
                        updated_summary = _update_raw_summary_archived(
                            ef_val, archived, timestamp_s, timestamp_ns
                        )
                        new_entry_parts.append(_encode_length_delimited(2, updated_summary))
                        matched = True
                    else:
                        new_entry_parts.append(ef_raw)
                out.append(_encode_length_delimited(1, b"".join(new_entry_parts)))
            else:
                out.append(raw_chunk)
        else:
            out.append(raw_chunk)
    return b"".join(out), matched


def _read_agyhub_session_archived(proto_bytes: bytes, session_id: str) -> bool | None:
    """Return the archived flag for session_id inside agyhub_summaries_proto.pb, or None if absent."""
    target_id_bytes = session_id.encode("utf-8")
    for field_num, wire_type, _, val in _iter_proto_fields(proto_bytes):
        if field_num == 1 and wire_type == 2:
            entry_fields = _iter_proto_fields(val)
            entry_id = None
            summary_bytes = None
            for ef_num, ef_wt, _, ef_val in entry_fields:
                if ef_num == 1 and ef_wt == 2:
                    entry_id = ef_val
                elif ef_num == 2 and ef_wt == 2:
                    summary_bytes = ef_val
            if entry_id == target_id_bytes and summary_bytes is not None:
                parsed = _parse_raw_summary_proto(summary_bytes)
                ann = parsed.get("annotations", {})
                if "archived" in ann:
                    return bool(ann["archived"])
                return False
    return None


# --- Text-Format Protobuf (annotations/<session_id>.pbtxt) Helpers ---


def _unescape_pbtxt_string(raw: str) -> str:
    try:
        return bytes(raw, "utf-8").decode("unicode_escape").encode("latin1").decode("utf-8")
    except Exception:
        return raw


def _mask_quoted_strings(text: str) -> tuple[str, list[str]]:
    """Replace double-quoted literals with placeholders of identical structure so regexes only hit top-level fields."""
    literals: list[str] = []

    def _replacer(match: re.Match[str]) -> str:
        idx = len(literals)
        literals.append(match.group(0))
        return f'"__PBTXT_STR_{idx}__"'

    masked = PBTXT_QUOTED_STRING_RE.sub(_replacer, text)
    return masked, literals


def _unmask_quoted_strings(masked: str, literals: list[str]) -> str:
    out = masked
    for idx, literal in enumerate(literals):
        out = out.replace(f'"__PBTXT_STR_{idx}__"', literal)
    return out


def _parse_pbtxt_annotation(text: str) -> dict[str, Any]:
    info: dict[str, Any] = {}
    title_match = PBTXT_TITLE_RE.search(text)
    if title_match:
        title = _unescape_pbtxt_string(title_match.group(1)).strip()
        if title:
            info["title"] = title
    masked, _ = _mask_quoted_strings(text)
    arch_match = PBTXT_ARCHIVED_RE.search(masked)
    if arch_match:
        info["archived"] = arch_match.group(1) == "true"
    ts_match = PBTXT_ARCHIVAL_TS_RE.search(masked)
    if ts_match:
        info["archival_status_timestamp_s"] = int(ts_match.group(1))
    return info


def _update_pbtxt_archived(
    existing_text: str, archived: bool, timestamp_s: int, timestamp_ns: int = 0
) -> str:
    arch_str = f"archived:{'true' if archived else 'false'}"
    ts_str = f"archival_status_timestamp:{{seconds:{timestamp_s} nanos:{timestamp_ns}}}"
    masked, literals = _mask_quoted_strings(existing_text.strip())

    if PBTXT_ARCHIVED_RE.search(masked):
        masked = PBTXT_ARCHIVED_RE.sub(arch_str, masked, count=1)
    else:
        masked = f"{arch_str} {masked}".strip() if masked else arch_str

    if PBTXT_ARCHIVAL_TS_RE.search(masked):
        masked = PBTXT_ARCHIVAL_TS_RE.sub(ts_str, masked, count=1)
    else:
        masked = re.sub(
            r"(\barchived:\s*(?:true|false)\b)",
            rf"\1 {ts_str}",
            masked,
            count=1,
        )
    return _unmask_quoted_strings(masked.strip(), literals) + "\n"


# --- Surface Metadata Loader ---


def _open_ro_sqlite(db_path: Path) -> sqlite3.Connection:
    wal_path = db_path.with_name(db_path.name + "-wal")
    query = "mode=ro" if wal_path.exists() else "mode=ro&immutable=1"
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?{query}", uri=True)


def _load_surface_metadata(brain_dir: Path, surface: str) -> dict[str, AntigravitySessionMetadata]:
    surface_root = brain_dir.parent
    db_path = surface_root / "conversation_summaries.db"
    ann_dir = surface_root / "annotations"

    raw_rows: dict[str, dict[str, Any]] = {}
    fts_rows: dict[str, dict[str, Any]] = {}
    ann_rows: dict[str, dict[str, Any]] = {}

    if db_path.is_file():
        try:
            with closing(_open_ro_sqlite(db_path)) as conn:
                cur = conn.cursor()
                try:
                    cur.execute(
                        """
                        SELECT conversation_id, title, step_count, last_modified_time,
                               workspace_uris, parent_conversation_id, raw_summary
                        FROM conversation_summaries
                        """
                    )
                    for cid, title, step_count, last_mod, ws_uris, parent_id, raw_sum in cur.fetchall():
                        if not cid:
                            continue
                        proto_info = _parse_raw_summary_proto(raw_sum)
                        raw_rows[str(cid)] = {
                            "title": (title or "").strip(),
                            "step_count": int(step_count or 0),
                            "last_modified_time": str(last_mod or ""),
                            "workspace": _parse_workspace_uris(str(ws_uris or "")),
                            "parent_conversation_id": str(parent_id or ""),
                            "proto": proto_info,
                        }
                except sqlite3.OperationalError:
                    pass

                try:
                    cur.execute(
                        """
                        SELECT conversation_id, title, workspace, last_modified_time,
                               step_count, archived
                        FROM conversations_fts
                        WHERE chunk_index = -1
                        """
                    )
                    for cid, title, workspace, last_mod, step_count, archived in cur.fetchall():
                        if not cid:
                            continue
                        fts_rows[str(cid)] = {
                            "title": (title or "").strip(),
                            "workspace": _uri_to_path(str(workspace or "")),
                            "last_modified_time": str(last_mod or ""),
                            "step_count": int(step_count or 0),
                            "archived": bool(int(archived or 0)),
                        }
                except sqlite3.OperationalError:
                    pass
        except sqlite3.Error:
            pass

    if ann_dir.is_dir():
        for pbtxt_file in sorted(ann_dir.glob("*.pbtxt")):
            cid = pbtxt_file.stem
            try:
                ann_rows[cid] = _parse_pbtxt_annotation(
                    pbtxt_file.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                continue

    transcript_map: dict[str, Path] = dict(_iter_transcript_files(brain_dir))

    all_ids = sorted(set(raw_rows) | set(fts_rows) | set(ann_rows) | set(transcript_map))
    result: dict[str, AntigravitySessionMetadata] = {}

    for cid in all_ids:
        r_info = raw_rows.get(cid, {})
        proto_info = r_info.get("proto", {})
        proto_ann = proto_info.get("annotations", {})
        f_info = fts_rows.get(cid, {})
        a_info = ann_rows.get(cid, {})

        title = (
            a_info.get("title")
            or proto_ann.get("title")
            or f_info.get("title")
            or r_info.get("title")
            or proto_info.get("summary")
            or ""
        )
        project_dir = (
            r_info.get("workspace")
            or f_info.get("workspace")
            or proto_info.get("project_directory")
            or ""
        )
        if "archived" in a_info:
            archived = bool(a_info["archived"])
        elif "archived" in f_info:
            archived = bool(f_info["archived"])
        elif "archived" in proto_ann:
            archived = bool(proto_ann["archived"])
        else:
            archived = False

        archival_ts = a_info.get("archival_status_timestamp_s") or proto_ann.get(
            "archival_status_timestamp_s"
        )
        step_count = (
            r_info.get("step_count")
            or f_info.get("step_count")
            or proto_info.get("step_count")
            or 0
        )
        last_mod = r_info.get("last_modified_time") or f_info.get("last_modified_time") or ""
        parent_id = r_info.get("parent_conversation_id") or ""
        t_path = transcript_map.get(cid)

        result[cid] = AntigravitySessionMetadata(
            session_id=cid,
            surface=surface,
            title=title,
            project_directory=project_dir,
            archived=archived,
            archival_status_timestamp_s=archival_ts,
            step_count=int(step_count),
            last_modified_time=str(last_mod),
            parent_conversation_id=str(parent_id),
            has_transcript=t_path is not None,
            transcript_path=t_path,
        )

    return result


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
        full_t = entry / TRANSCRIPT_RELATIVE_PATH
        compact_t = entry / COMPACT_TRANSCRIPT_RELATIVE_PATH
        if full_t.is_file():
            sessions.append((entry.name, full_t))
        elif compact_t.is_file():
            sessions.append((entry.name, compact_t))
    return sessions


def _parse_transcript(
    file_path: Path,
    session_id: str,
    surface: str,
    *,
    metadata: AntigravitySessionMetadata | None = None,
) -> ParsedAntigravitySession | None:
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
                    started_at = to_local_date(timestamp)

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

    fallback_title = (first_user_text or session_id).strip().splitlines()[0][:120] or session_id
    title = metadata.title.strip() if metadata and metadata.title.strip() else fallback_title
    project_directory = metadata.project_directory if metadata else ""

    return ParsedAntigravitySession(
        record=SessionRecord(
            source="antigravity",
            surface=surface,
            session_id=session_id,
            title=title,
            date=started_at.isoformat(),
            messages=messages,
            project_directory=project_directory,
            models_used=[],
        ),
        latest_timestamp_ms=latest_timestamp_ms,
    )


def list_antigravity_sessions(
    *,
    brain_dir: Path | None = None,
    brain_dirs: Mapping[str, Path] | None = None,
    status: str = "all",
    query: str | None = None,
    include_subagents: bool = False,
) -> list[dict[str, Any]]:
    """List Antigravity sessions across surfaces with native archive & metadata state."""
    if status not in {"all", "active", "archived"}:
        raise ValueError(f"Unsupported status filter: {status}")
    roots = _resolve_roots(brain_dir=brain_dir, brain_dirs=brain_dirs)
    needle = query.strip().lower() if query and query.strip() else None
    items: list[dict[str, Any]] = []

    for surface, root in roots.items():
        meta_map = _load_surface_metadata(root, surface)
        for cid, meta in meta_map.items():
            if not include_subagents and meta.parent_conversation_id:
                continue
            if status == "active" and meta.archived:
                continue
            if status == "archived" and not meta.archived:
                continue
            if needle:
                haystack = f"{meta.session_id} {meta.title} {meta.project_directory}".lower()
                if needle not in haystack:
                    continue
            items.append(
                {
                    "session_id": cid,
                    "surface": surface,
                    "title": meta.title,
                    "project_directory": meta.project_directory,
                    "archived": meta.archived,
                    "archival_status_timestamp_s": meta.archival_status_timestamp_s,
                    "step_count": meta.step_count,
                    "last_modified_time": meta.last_modified_time,
                    "parent_conversation_id": meta.parent_conversation_id,
                    "has_transcript": meta.has_transcript,
                    "transcript_path": str(meta.transcript_path) if meta.transcript_path else None,
                }
            )
    return items


def parse_antigravity_session(
    session_id: str,
    *,
    brain_dir: Path | None = None,
    brain_dirs: Mapping[str, Path] | None = None,
    include_thinking: bool = False,
    include_tool_calls: bool = False,
) -> dict[str, Any]:
    """Parse a single active or archived Antigravity session by ID."""
    roots = _resolve_roots(brain_dir=brain_dir, brain_dirs=brain_dirs)
    matched_meta: AntigravitySessionMetadata | None = None
    matched_root: Path | None = None

    for surface, root in roots.items():
        meta_map = _load_surface_metadata(root, surface)
        if session_id in meta_map:
            cand = meta_map[session_id]
            if cand.has_transcript or matched_meta is None:
                matched_meta = cand
                matched_root = root
                if cand.has_transcript:
                    break

    if matched_meta is None or matched_root is None:
        raise FileNotFoundError(f"Antigravity session not found: {session_id}")

    t_path = matched_meta.transcript_path
    if t_path is None or not t_path.is_file():
        raise FileNotFoundError(
            f"Antigravity session '{session_id}' exists in metadata on surface '{matched_meta.surface}' "
            "but has no local transcript file."
        )

    parsed = _parse_transcript(
        t_path, session_id, matched_meta.surface, metadata=matched_meta
    )
    steps_out: list[dict[str, Any]] = []
    if include_thinking or include_tool_calls:
        with t_path.open("r", encoding="utf-8") as handle:
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
                step_type = _string_field(step, "type", line_number)
                source = _string_field(step, "source", line_number)
                if step_type not in {"USER_INPUT", "PLANNER_RESPONSE"}:
                    continue
                entry: dict[str, Any] = {
                    "step_index": step.get("step_index"),
                    "type": step_type,
                    "source": source,
                    "created_at": step.get("created_at"),
                    "content": (
                        _extract_user_text(_string_field(step, "content", line_number))
                        if step_type == "USER_INPUT"
                        else _string_field(step, "content", line_number).strip()
                    ),
                }
                if include_thinking and "thinking" in step:
                    entry["thinking"] = step.get("thinking")
                if include_tool_calls and "tool_calls" in step:
                    entry["tool_calls"] = step.get("tool_calls")
                steps_out.append(entry)

    markdown = render_markdown(parsed.record) if parsed is not None else ""
    messages = (
        [
            {
                "role": m.role,
                "content": m.content,
                "time_created": m.time_created,
            }
            for m in parsed.record.messages
        ]
        if parsed is not None
        else []
    )
    title = (
        parsed.record.title
        if parsed is not None
        else (matched_meta.title or session_id)
    )
    date_str = parsed.record.date if parsed is not None else ""

    result: dict[str, Any] = {
        "session_id": session_id,
        "surface": matched_meta.surface,
        "title": title,
        "date": date_str,
        "project_directory": matched_meta.project_directory,
        "archived": matched_meta.archived,
        "archival_status_timestamp_s": matched_meta.archival_status_timestamp_s,
        "step_count": matched_meta.step_count,
        "last_modified_time": matched_meta.last_modified_time,
        "parent_conversation_id": matched_meta.parent_conversation_id,
        "transcript_path": str(t_path),
        "message_count": len(messages),
        "messages": messages,
        "markdown": markdown,
    }
    if include_thinking or include_tool_calls:
        result["steps"] = steps_out
    return result


def set_antigravity_archived(
    session_ids: Sequence[str],
    *,
    archived: bool,
    brain_dir: Path | None = None,
    brain_dirs: Mapping[str, Path] | None = None,
    dry_run: bool = False,
    timestamp_s: int | None = None,
) -> list[dict[str, Any]]:
    """Archive or unarchive Antigravity sessions across native annotations, SQLite FTS/summary, and hub proto."""
    roots = _resolve_roots(brain_dir=brain_dir, brain_dirs=brain_dirs)
    now_ns = time.time_ns()
    ts_sec = timestamp_s if timestamp_s is not None else (now_ns // 1_000_000_000)
    ts_nanos = 0 if timestamp_s is not None else (now_ns % 1_000_000_000)

    results: list[dict[str, Any]] = []
    for session_id in session_ids:
        target_surface: str | None = None
        target_root: Path | None = None
        before_meta: AntigravitySessionMetadata | None = None

        for surface, root in roots.items():
            meta_map = _load_surface_metadata(root, surface)
            if session_id in meta_map:
                target_surface = surface
                target_root = root
                before_meta = meta_map[session_id]
                break

        if target_surface is None or target_root is None or before_meta is None:
            results.append(
                {
                    "session_id": session_id,
                    "archived": archived,
                    "status": "not_found",
                    "stores_updated": [],
                }
            )
            continue

        surface_root = target_root.parent
        ann_dir = surface_root / "annotations"
        ann_file = ann_dir / f"{session_id}.pbtxt"
        db_path = surface_root / "conversation_summaries.db"
        hub_pb_path = surface_root / "agyhub_summaries_proto.pb"

        stores_updated: list[str] = []
        write_failed = False

        if dry_run:
            if ann_dir.is_dir() or ann_file.is_file() or target_root.is_dir():
                stores_updated.append("annotations_pbtxt")
            if db_path.is_file():
                stores_updated.append("conversation_summaries_db")
            if hub_pb_path.is_file():
                stores_updated.append("agyhub_summaries_proto_pb")
            results.append(
                {
                    "session_id": session_id,
                    "surface": target_surface,
                    "previous_archived": before_meta.archived,
                    "archived": archived,
                    "archival_status_timestamp_s": ts_sec,
                    "status": "dry_run",
                    "stores_updated": stores_updated,
                }
            )
            continue

        # 1. Update annotations/<session_id>.pbtxt
        try:
            ann_dir.mkdir(parents=True, exist_ok=True)
            existing_pbtxt = (
                ann_file.read_text(encoding="utf-8", errors="replace")
                if ann_file.is_file()
                else ""
            )
            new_pbtxt = _update_pbtxt_archived(existing_pbtxt, archived, ts_sec, ts_nanos)
            ann_file.write_text(new_pbtxt, encoding="utf-8")
            stores_updated.append("annotations_pbtxt")
        except OSError:
            write_failed = True

        # 2. Update conversation_summaries.db (conversations_fts + conversation_summaries.raw_summary)
        if db_path.is_file():
            db_updated = True
            try:
                with closing(sqlite3.connect(db_path)) as conn:
                    cur = conn.cursor()
                    arch_int = 1 if archived else 0
                    cur.execute(
                        "SELECT name FROM sqlite_master WHERE name IN ('conversations_fts', 'conversation_summaries')"
                    )
                    present_tables = {r[0] for r in cur.fetchall()}

                    if "conversations_fts" in present_tables:
                        cur.execute(
                            "UPDATE conversations_fts SET archived = ? WHERE conversation_id = ? AND chunk_index = -1",
                            (arch_int, session_id),
                        )

                    if "conversation_summaries" in present_tables:
                        cur.execute(
                            "SELECT raw_summary FROM conversation_summaries WHERE conversation_id = ?",
                            (session_id,),
                        )
                        row = cur.fetchone()
                        if row and row[0]:
                            updated_raw = _update_raw_summary_archived(
                                bytes(row[0]), archived, ts_sec, ts_nanos
                            )
                            cur.execute(
                                "UPDATE conversation_summaries SET raw_summary = ? WHERE conversation_id = ?",
                                (updated_raw, session_id),
                            )
                    conn.commit()
            except (sqlite3.Error, ValueError):
                db_updated = False
                write_failed = True

            if db_updated:
                stores_updated.append("conversation_summaries_db")

        # 3. Update agyhub_summaries_proto.pb if present
        hub_matched = False
        if hub_pb_path.is_file():
            try:
                raw_hub = hub_pb_path.read_bytes()
                updated_hub, hub_matched = _update_agyhub_proto_archived(
                    raw_hub, session_id, archived, ts_sec, ts_nanos
                )
                if hub_matched:
                    hub_pb_path.write_bytes(updated_hub)
                    stores_updated.append("agyhub_summaries_proto_pb")
            except (OSError, ValueError):
                write_failed = True

        # 4. Readback verification across all updated stores
        after_map = _load_surface_metadata(target_root, target_surface)
        after_meta = after_map.get(session_id)
        verified = (not write_failed) and (after_meta is not None) and (after_meta.archived == archived)

        if verified and db_path.is_file():
            try:
                with closing(_open_ro_sqlite(db_path)) as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT name FROM sqlite_master WHERE name IN ('conversations_fts', 'conversation_summaries')"
                    )
                    present_tables = {r[0] for r in cur.fetchall()}
                    if "conversations_fts" in present_tables:
                        cur.execute(
                            "SELECT archived FROM conversations_fts WHERE conversation_id = ? AND chunk_index = -1",
                            (session_id,),
                        )
                        fts_row = cur.fetchone()
                        if fts_row is not None and bool(int(fts_row[0])) != archived:
                            verified = False
                    if "conversation_summaries" in present_tables:
                        cur.execute(
                            "SELECT raw_summary FROM conversation_summaries WHERE conversation_id = ?",
                            (session_id,),
                        )
                        cs_row = cur.fetchone()
                        if cs_row and cs_row[0]:
                            parsed_raw = _parse_raw_summary_proto(bytes(cs_row[0]))
                            if parsed_raw.get("annotations", {}).get("archived") != archived:
                                verified = False
            except sqlite3.Error:
                verified = False

        if verified and hub_matched and hub_pb_path.is_file():
            try:
                hub_arch = _read_agyhub_session_archived(hub_pb_path.read_bytes(), session_id)
                if hub_arch != archived:
                    verified = False
            except (OSError, ValueError):
                verified = False

        results.append(
            {
                "session_id": session_id,
                "surface": target_surface,
                "previous_archived": before_meta.archived,
                "archived": after_meta.archived if after_meta else None,
                "archival_status_timestamp_s": ts_sec,
                "status": "verified" if verified else "failed_verification",
                "stores_updated": stores_updated,
            }
        )

    return results


def _is_unchanged_session(
    session_state: dict[str, Any],
    output_dir: Path,
    source_mtime_ns: int,
    source_size: int,
    metadata: AntigravitySessionMetadata | None = None,
) -> bool:
    if int(session_state.get("source_mtime_ns", 0)) != source_mtime_ns:
        return False
    if int(session_state.get("source_size", -1)) != source_size:
        return False
    meta_title = metadata.title.strip() if metadata else ""
    meta_project = metadata.project_directory if metadata else ""
    if str(session_state.get("metadata_title", "")) != meta_title:
        return False
    if str(session_state.get("metadata_project_directory", "")) != meta_project:
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
    roots = _resolve_roots(brain_dir=brain_dir, brain_dirs=brain_dirs)

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
        metadata_map = _load_surface_metadata(root, surface)

        for session_id, transcript in _iter_transcript_files(root):
            scanned += 1
            counts["scanned"] += 1
            stat = transcript.stat()
            previous = sessions.get(session_id, {})
            meta = metadata_map.get(session_id)
            if not full and _is_unchanged_session(
                previous, output_dir, stat.st_mtime_ns, stat.st_size, meta
            ):
                continue

            try:
                parsed = _parse_transcript(
                    transcript,
                    session_id,
                    surface,
                    metadata=meta,
                )
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

            meta_title = meta.title.strip() if meta else ""
            meta_project = meta.project_directory if meta else ""

            if parsed is None:
                if not dry_run:
                    sessions[session_id] = {
                        "status": "ignored",
                        "source_mtime_ns": stat.st_mtime_ns,
                        "source_size": stat.st_size,
                        "metadata_title": meta_title,
                        "metadata_project_directory": meta_project,
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
                        "metadata_title": meta_title,
                        "metadata_project_directory": meta_project,
                    }
                continue

            previous_output = str(previous.get("output_file") or "")
            if previous_output and not dry_run:
                output_path = resolve_output_path(
                    output_dir, previous_output, parsed.record.date, parsed.record.title
                )
            elif previous_output:
                output_path = output_dir / previous_output
            else:
                output_path = unique_output_path(output_dir, parsed.record.date, parsed.record.title)
            if not dry_run:
                output_path.write_text(render_markdown(parsed.record), encoding="utf-8")
                sessions[session_id] = {
                    "status": "complete",
                    "latest_timestamp": parsed.latest_timestamp_ms,
                    "output_file": output_path.name,
                    "source_mtime_ns": stat.st_mtime_ns,
                    "source_size": stat.st_size,
                    "metadata_title": meta_title,
                    "metadata_project_directory": meta_project,
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
