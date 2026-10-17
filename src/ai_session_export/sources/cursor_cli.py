from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import ms_to_date, parse_iso_timestamp, should_skip_session, unique_output_path


DEFAULT_CURSOR_CLI_DIR = Path.home() / ".cursor" / "chats"
SUPPORTED_SCHEMA_VERSION = 1

USER_QUERY_PATTERN = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)
TIMESTAMP_PATTERN = re.compile(r"<timestamp>(.*?)</timestamp>", re.DOTALL)
WORKSPACE_PATH_PATTERN = re.compile(r"^\s*Workspace Path:\s*(.+?)\s*$", re.MULTILINE)
# `<Weekday>, <Mon> <D>, <YYYY>, <h>:<mm> <AM|PM> (UTC±H[:MM])`; minute precision.
HUMAN_TIMESTAMP_PATTERN = re.compile(
    r"^\s*[A-Za-z]+,\s*([A-Za-z]{3} \d{1,2}, \d{4}, \d{1,2}:\d{2} [AP]M)\s*"
    r"\(UTC(?:([+-])(\d{1,2})(?::(\d{2}))?)?\)\s*$"
)
SUMMARY_PREFIX = "<summary_content>"
USER_INFO_PREFIX = "<user_info>"


class _Segment(NamedTuple):
    """One root snapshot's message refs and the model of its leading system blob."""

    refs: list[str]
    model: str | None


# --------------------------------------------------------------------------- #
# Protobuf wire format (root snapshots only need field 1)
# --------------------------------------------------------------------------- #


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift >= 64:
            raise ValueError("varint longer than 64 bits")


def _root_refs(data: bytes) -> list[str]:
    """Ordered field-1 message refs (32-byte sha256 ids) of a root snapshot.

    Repeats are kept: identical content is stored once and referenced twice.
    Every other field is skipped by wire type.
    """
    refs: list[str] = []
    pos = 0
    size = len(data)
    while pos < size:
        key, pos = _read_varint(data, pos)
        field, wire_type = key >> 3, key & 0x07
        if field == 0:
            raise ValueError("invalid protobuf field number 0")
        if wire_type == 0:
            _, pos = _read_varint(data, pos)
        elif wire_type == 1:
            pos += 8
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            end = pos + length
            if end > size:
                raise ValueError("length-delimited field overruns the blob")
            if field == 1 and length == 32:
                refs.append(data[pos:end].hex())
            pos = end
        elif wire_type == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        if pos > size:
            raise ValueError("fixed-width field overruns the blob")
    return refs


# --------------------------------------------------------------------------- #
# Store access
# --------------------------------------------------------------------------- #


def _open_store(db_path: Path) -> sqlite3.Connection:
    # Every store is in WAL mode. A read-only connection cannot create the
    # `-shm` file, so a closed store only opens as immutable; a live store keeps
    # recent pages (sometimes the whole schema) in `-wal`, which immutable ignores.
    wal_path = db_path.with_name(db_path.name + "-wal")
    query = "mode=ro" if wal_path.exists() else "mode=ro&immutable=1"
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?{query}", uri=True)


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise ValueError(f"unexpected blob value type {type(value).__name__}")


def _read_meta0(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
    if row is None or row[0] is None:
        raise ValueError("store.db has no meta row '0'")
    raw = row[0].decode("ascii") if isinstance(row[0], bytes) else str(row[0])
    meta0 = json.loads(bytes.fromhex(raw.strip()))
    if not isinstance(meta0, dict):
        raise ValueError("meta row '0' is not a JSON object")
    return meta0


def _read_meta_json(session_dir: Path) -> dict[str, Any] | None:
    path = session_dir / "meta.json"
    if not path.is_file():
        return None
    meta_json = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(meta_json, dict):
        raise ValueError("meta.json is not a JSON object")
    return meta_json


def _int_field(mapping: dict[str, Any] | None, key: str) -> int:
    value = (mapping or {}).get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


class _BlobStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._messages: dict[str, dict[str, Any] | None] = {}

    def data(self, blob_id: str) -> bytes | None:
        row = self._conn.execute("SELECT data FROM blobs WHERE id = ?", (blob_id,)).fetchone()
        return None if row is None or row[0] is None else _as_bytes(row[0])

    def message(self, blob_id: str) -> dict[str, Any] | None:
        """The JSON message behind a ref, or None for non-message or missing blobs."""
        if blob_id not in self._messages:
            self._messages[blob_id] = _decode_message(self.data(blob_id))
        return self._messages[blob_id]

    def rows(self) -> list[tuple[int, str, bytes]]:
        rows = self._conn.execute("SELECT rowid, id, data FROM blobs").fetchall()
        return [(rowid, blob_id, _as_bytes(data)) for rowid, blob_id, data in rows if data is not None]


def _decode_message(data: bytes | None) -> dict[str, Any] | None:
    if not data or not data.lstrip().startswith(b"{"):
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict) or parsed.get("role") not in {"system", "user", "assistant", "tool"}:
        return None
    return parsed


# --------------------------------------------------------------------------- #
# Message classification
# --------------------------------------------------------------------------- #


def _cursor_options(message: dict[str, Any]) -> dict[str, Any]:
    provider_options = message.get("providerOptions")
    if not isinstance(provider_options, dict):
        return {}
    cursor_options = provider_options.get("cursor")
    return cursor_options if isinstance(cursor_options, dict) else {}


def _is_summary(message: dict[str, Any] | None) -> bool:
    if not message or message.get("role") != "user" or not isinstance(message.get("content"), str):
        return False
    if _cursor_options(message).get("isSummary") is True:
        return True
    return message["content"].lstrip().startswith(SUMMARY_PREFIX)


def _system_model(message: dict[str, Any] | None) -> str | None:
    if not message or message.get("role") != "system":
        return None
    fingerprint = _cursor_options(message).get("systemPromptFingerprint")
    if not isinstance(fingerprint, dict):
        return None
    model = str(fingerprint.get("model") or "").strip()
    return model or None


def _text_parts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ]


def _parse_human_timestamp(value: str) -> int | None:
    iso = parse_iso_timestamp(value)
    if iso is not None and iso.tzinfo is not None:
        return int(iso.timestamp() * 1000)
    match = HUMAN_TIMESTAMP_PATTERN.match(value)
    if match is None:
        return None
    body, sign, hours, minutes = match.groups()
    try:
        naive = datetime.strptime(body, "%b %d, %Y, %I:%M %p")
    except ValueError:
        return None
    offset = timedelta(hours=int(hours or 0), minutes=int(minutes or 0))
    if sign == "-":
        offset = -offset
    return int(naive.replace(tzinfo=timezone(offset)).timestamp() * 1000)


def _human_turn(message: dict[str, Any]) -> tuple[str, int | None] | None:
    """(text, ms) for a human `user` blob; injected string-content users are not human."""
    content = message.get("content")
    if not isinstance(content, list):
        return None
    joined = "\n".join(_text_parts(content))
    text = "\n\n".join(body.strip() for body in USER_QUERY_PATTERN.findall(joined) if body.strip()).strip()
    if not text:
        return None
    time_created = None
    for raw in TIMESTAMP_PATTERN.findall(joined):
        time_created = _parse_human_timestamp(raw.strip())
        if time_created is not None:
            break
    return text, time_created


def _assistant_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    return "\n\n".join(text.strip() for text in _text_parts(content) if text.strip()).strip()


# --------------------------------------------------------------------------- #
# Conversation assembly
# --------------------------------------------------------------------------- #


def _is_strict_prefix(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    return len(shorter) < len(longer) and longer[: len(shorter)] == shorter


def _stitch_compacted(store: _BlobStore, latest_refs: list[str]) -> list[list[str]]:
    """Ordered conversation segments when the latest root follows a compaction.

    Conversation roots are protobuf blobs whose first ref is a `system` blob
    (summarizer requests start with a `user` blob). Each maximal root (not a
    strict prefix of another) heads a chain; chains sort by the smallest rowid
    among their roots, and the latest root's chain must sort last.
    """
    roots: dict[tuple[str, ...], list[int]] = {}
    for rowid, _blob_id, data in store.rows():
        if data.lstrip().startswith(b"{"):
            continue
        try:
            refs = tuple(_root_refs(data))
        except ValueError:
            continue
        if not refs:
            continue
        first = store.message(refs[0])
        if not first or first.get("role") != "system":
            continue
        roots.setdefault(refs, []).append(rowid)

    latest = tuple(latest_refs)
    roots.setdefault(latest, [])
    maxima = [refs for refs in roots if not any(_is_strict_prefix(refs, other) for other in roots)]
    if latest not in maxima:
        raise ValueError("latest root is a strict prefix of another conversation root")

    def chain_key(head: tuple[str, ...]) -> tuple[int, int]:
        rowids = [rowid for refs, ids in roots.items() if refs == head or _is_strict_prefix(refs, head) for rowid in ids]
        return (min(rowids), max(rowids)) if rowids else (0, 0)

    ordered = sorted(maxima, key=chain_key)
    if ordered[-1] != latest:
        raise ValueError("latest root chain does not sort last among conversation roots")
    return [list(refs) for refs in ordered]


def _build_segments(
    store: _BlobStore, session_id: str, latest_refs: list[str], warnings: list[dict[str, Any]]
) -> list[_Segment]:
    raw_segments = [latest_refs]
    if any(_is_summary(store.message(ref)) for ref in latest_refs):
        try:
            raw_segments = _stitch_compacted(store, latest_refs)
        except Exception as error:  # noqa: BLE001 - fall back to the live branch
            warnings.append(
                {
                    "session_id": session_id,
                    "error": f"compaction stitching fell back to the latest root ({type(error).__name__}: {error})",
                }
            )
            raw_segments = [latest_refs]

    segments: list[_Segment] = []
    emitted: set[str] = set()
    for refs in raw_segments:
        model = _system_model(store.message(refs[0])) if refs else None
        # Repeats inside one snapshot are real repeated messages; refs shared
        # with an earlier snapshot are the retained tail and emit once.
        kept = [ref for ref in refs if ref not in emitted and not _is_summary(store.message(ref))]
        emitted.update(refs)
        segments.append(_Segment(refs=kept, model=model))
    return segments


def _workspace_path(store: _BlobStore, latest_refs: list[str], workspace_hash: str) -> str:
    for ref in latest_refs:
        message = store.message(ref)
        if not message or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.lstrip().startswith(USER_INFO_PREFIX):
            continue
        match = WORKSPACE_PATH_PATTERN.search(content)
        if match is None:
            continue
        candidate = match.group(1)
        if hashlib.md5(candidate.encode("utf-8")).hexdigest() == workspace_hash:
            return candidate
    return ""


def _parse_store(
    store: _BlobStore,
    session_dir: Path,
    meta0: dict[str, Any],
    meta_json: dict[str, Any] | None,
    warnings: list[dict[str, Any]],
) -> SessionRecord | None:
    session_id = session_dir.name
    root_id = str(meta0.get("latestRootBlobId") or "").strip().lower()
    if not root_id:
        return None
    root_data = store.data(root_id)
    if root_data is None:
        return None
    latest_refs = _root_refs(root_data)
    if not latest_refs:
        return None

    messages: list[MessageTurn] = []
    pending_user_turns: list[int] = []
    models: set[str] = set()
    first_user_text = ""
    for segment in _build_segments(store, session_id, latest_refs, warnings):
        if segment.model:
            models.add(segment.model)
        for ref in segment.refs:
            message = store.message(ref)
            if not message:
                continue
            role = message.get("role")
            if role == "user":
                human = _human_turn(message)
                if human is None:
                    continue
                text, time_created = human
                if not first_user_text:
                    first_user_text = text
                messages.append(MessageTurn(role="user", content=text, time_created=time_created, model=segment.model))
                pending_user_turns.append(len(messages) - 1)
            elif role == "assistant":
                text = _assistant_text(message)
                if not text:
                    continue
                for index in pending_user_turns:
                    messages[index] = messages[index]._replace(model=segment.model)
                pending_user_turns.clear()
                messages.append(MessageTurn(role="assistant", content=text, time_created=None, model=segment.model))

    if not first_user_text:
        return None

    title = first_user_text.splitlines()[0][:120] or session_id
    if should_skip_session(title):
        return None

    created_ms = _int_field(meta_json, "createdAtMs") or _int_field(meta0, "createdAt")
    if not created_ms:
        created_ms = next((m.time_created for m in messages if m.time_created), 0) or 0
    if not created_ms:
        # Without any timestamp the session cannot be placed in the archive.
        return None

    project_directory = str((meta_json or {}).get("cwd") or "").strip()
    if not project_directory:
        project_directory = _workspace_path(store, latest_refs, session_dir.parent.name)

    return SessionRecord(
        source="cursor_cli",
        session_id=session_id,
        title=title,
        date=ms_to_date(created_ms),
        messages=messages,
        project_directory=project_directory,
        models_used=sorted(models),
    )


def _unsupported_schema(meta_json: dict[str, Any] | None) -> Any:
    if meta_json is None or "schemaVersion" not in meta_json:
        return None
    version = meta_json["schemaVersion"]
    return None if version == SUPPORTED_SCHEMA_VERSION else version


def parse_cursor_cli_session(
    session_dir: Path, warnings: list[dict[str, Any]] | None = None
) -> SessionRecord | None:
    """Parse one `~/.cursor/chats/<workspaceHash>/<sessionUuid>/` directory."""
    warnings = warnings if warnings is not None else []
    db_path = session_dir / "store.db"
    if not db_path.is_file():
        return None
    meta_json = _read_meta_json(session_dir)
    with closing(_open_store(db_path)) as conn:
        meta0 = _read_meta0(conn)
        if "subagentInfo" in meta0:
            return None
        version = _unsupported_schema(meta_json)
        if version is not None:
            warnings.append({"session_id": session_dir.name, "error": f"unsupported meta.json schemaVersion {version!r}"})
            return None
        return _parse_store(_BlobStore(conn), session_dir, meta0, meta_json, warnings)


def _iter_session_dirs(chats_dir: Path) -> list[Path]:
    if not chats_dir.is_dir():
        return []
    return sorted(path for path in chats_dir.glob("*/*/") if path.is_dir())


def export_cursor_cli(
    output_dir: Path,
    state: dict[str, Any],
    *,
    chats_dir: Path = DEFAULT_CURSOR_CLI_DIR,
    full: bool,
    dry_run: bool,
    since_date: date | None,
) -> dict[str, Any]:
    source_state = state.setdefault("cursor_cli", {})
    session_state: dict[str, dict[str, Any]] = source_state.setdefault("sessions", {})

    exported = 0
    scanned = 0
    failed = 0
    warnings: list[dict[str, Any]] = []
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for session_dir in _iter_session_dirs(chats_dir):
        db_path = session_dir / "store.db"
        if not db_path.is_file():
            continue
        scanned += 1
        session_id = session_dir.name
        session_warnings: list[dict[str, Any]] = []
        try:
            meta_json = _read_meta_json(session_dir)
            with closing(_open_store(db_path)) as conn:
                meta0 = _read_meta0(conn)
                if "subagentInfo" in meta0:
                    continue
                version = _unsupported_schema(meta_json)
                if version is not None:
                    warnings.append({"session_id": session_id, "error": f"unsupported meta.json schemaVersion {version!r}"})
                    continue

                latest_ms = _int_field(meta_json, "updatedAtMs") or _int_field(meta0, "createdAt")
                root_id = str(meta0.get("latestRootBlobId") or "").strip().lower()
                previous = session_state.get(session_id, {})
                previous_output = str(previous.get("output_file") or "")
                output_path = output_dir / previous_output if previous_output else None
                if (
                    not full
                    and output_path is not None
                    and output_path.is_file()
                    and latest_ms <= int(previous.get("latest_timestamp", 0))
                    and root_id == previous.get("root_blob_id")
                ):
                    continue

                created_ms = _int_field(meta_json, "createdAtMs") or _int_field(meta0, "createdAt")
                if since_date and created_ms and date.fromisoformat(ms_to_date(created_ms)) < since_date:
                    continue

                record = _parse_store(_BlobStore(conn), session_dir, meta0, meta_json, session_warnings)
        except Exception as error:  # noqa: BLE001 - isolate one unreadable session
            failed += 1
            warnings.append({"session_id": session_id, "error": f"{type(error).__name__}: {error}"})
            continue

        warnings.extend(session_warnings)
        if record is None:
            continue
        if since_date and date.fromisoformat(record.date) < since_date:
            continue

        if output_path is None:
            output_path = unique_output_path(output_dir, record.date, record.title)
        if not dry_run:
            output_path.write_text(render_markdown(record), encoding="utf-8")
            session_state[session_id] = {
                "latest_timestamp": latest_ms,
                "output_file": output_path.name,
                "root_blob_id": root_id,
            }
        exported += 1

    result: dict[str, Any] = {"source": "cursor_cli", "scanned": scanned, "exported": exported}
    if failed:
        result["failed"] = failed
    if warnings:
        result["warnings"] = warnings
    return result
