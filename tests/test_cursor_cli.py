from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_session_export.cli as cli_module
from ai_session_export.cli import SOURCE_CHOICES, run_export
from ai_session_export.sources.cursor_cli import (
    DEFAULT_CURSOR_CLI_DIR,
    _parse_human_timestamp,
    _root_refs,
    export_cursor_cli,
    parse_cursor_cli_session,
)
from ai_session_export.state import load_state
from ai_session_export.utils import ms_to_date, ms_to_hhmm


SESSION_ID = "00000000-0000-4000-8000-0000000000c1"
OTHER_SESSION_ID = "00000000-0000-4000-8000-0000000000c2"
CWD = "/home/user/project"
MODEL = "fixture-cli-model"
CREATED_MS = 1781092800000  # 2026-06-10T12:00:00Z
UPDATED_MS = CREATED_MS + 60_000
STAMP = "Wednesday, Jun 10, 2026, 5:05 AM (UTC-7)"
STAMP_MS = int(datetime(2026, 6, 10, 5, 5, tzinfo=timezone(timedelta(hours=-7))).timestamp() * 1000)


# --------------------------------------------------------------------------- #
# Synthetic store builder
# --------------------------------------------------------------------------- #


def system(model: str | None = MODEL) -> dict:
    message = {"role": "system", "content": "fixture system prompt"}
    if model:
        message["providerOptions"] = {"cursor": {"systemPromptFingerprint": {"agentType": "cli", "model": model}}}
    return message


def user_info(path: str = CWD) -> dict:
    return {"role": "user", "content": f"<user_info>\nOS Version: fixture-os\n\nWorkspace Path: {path}\n</user_info>"}


def human(text: str, *, stamp: str | None = None, reminder: str | None = None) -> dict:
    parts = []
    if reminder:
        parts.append({"type": "text", "text": f"<system_reminder>\n{reminder}\n</system_reminder>"})
    if stamp:
        parts.append({"type": "text", "text": f"<timestamp>{stamp}</timestamp>"})
    parts.append({"type": "text", "text": f"<user_query>\n{text}\n</user_query>"})
    return {"role": "user", "content": parts, "providerOptions": {"cursor": {"requestId": "req-fixture"}}}


def reply(*texts: str, reasoning: str | None = None, tool_call: bool = False) -> dict:
    parts = []
    if reasoning:
        parts.append({"type": "reasoning", "text": reasoning})
    parts.extend({"type": "text", "text": text} for text in texts)
    if tool_call:
        parts.append({"type": "tool-call", "toolCallId": "tc-1", "toolName": "Shell",
                      "input": {"command": "tool-call-secret"}})
    return {"id": f"msg-{'-'.join(texts) or 'tool'}", "role": "assistant", "content": parts}


def tool_result() -> dict:
    return {"id": "tool-1", "role": "tool",
            "content": [{"type": "tool-result", "toolCallId": "tc-1", "result": "tool-result-secret"}]}


def summary() -> dict:
    return {"role": "user", "content": "summary-secret of earlier turns",
            "providerOptions": {"cursor": {"isSummary": True}}}


def encode(message: dict) -> bytes:
    return json.dumps(message, sort_keys=True).encode("utf-8")


def blob_id(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def root_blob(messages: list[dict]) -> bytes:
    refs = b"".join(b"\x0a\x20" + bytes.fromhex(blob_id(encode(m))) for m in messages)
    # Unknown fields of every wire type: 22 string `cli`, 26 varint ms, 30 fixed64, 31 fixed32.
    return refs + b"\xb2\x01\x03cli" + b"\xd0\x01" + varint(CREATED_MS) + b"\xf1\x01" + b"\x00" * 8 + b"\xfd\x01" + b"\x00" * 4


def write_session(
    chats_dir: Path,
    roots: list[list[dict] | bytes],
    *,
    session_id: str = SESSION_ID,
    cwd: str = CWD,
    write_meta_json: bool = True,
    include_cwd: bool = True,
    meta_json_extra: dict | None = None,
    extra_messages: tuple[dict, ...] = (),
    reverse_message_rows: bool = False,
    reverse_root_rows: bool = False,
    subagent: bool = False,
    journal: str = "wal",
    keep_open: bool = False,
    latest_root_id: str | None = None,
    created_ms: int = CREATED_MS,
    updated_ms: int = UPDATED_MS,
) -> tuple[Path, sqlite3.Connection | None]:
    """Build `<chats>/<md5(cwd)>/<session>/` with a content-addressed store.db.

    `roots` lists root snapshots oldest first; the last one is `latestRootBlobId`.
    """
    session_dir = chats_dir / hashlib.md5(cwd.encode()).hexdigest() / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    for name in ("store.db", "store.db-wal", "store.db-shm"):
        (session_dir / name).unlink(missing_ok=True)

    message_rows: dict[str, bytes] = {}
    for root in roots:
        for message in root if isinstance(root, list) else []:
            data = encode(message)
            message_rows.setdefault(blob_id(data), data)
    for message in extra_messages:
        data = encode(message)
        message_rows.setdefault(blob_id(data), data)
    message_items = list(message_rows.items())
    if reverse_message_rows:
        message_items.reverse()
    root_items = [(blob_id(data), data) for data in (r if isinstance(r, bytes) else root_blob(r) for r in roots)]
    latest = latest_root_id or root_items[-1][0]
    if reverse_root_rows:
        root_items.reverse()
    noise = [(blob_id(b"# fixture cached file\n"), b"# fixture cached file\n")]

    conn = sqlite3.connect(session_dir / "store.db")
    conn.execute(f"PRAGMA journal_mode={journal}")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    conn.executemany("INSERT OR REPLACE INTO blobs VALUES (?, ?)", message_items + noise + root_items)
    meta0 = {"agentId": session_id, "latestRootBlobId": latest, "name": "New Agent", "mode": "default",
             "createdAt": created_ms, "isRunEverything": False}
    if subagent:
        meta0["subagentInfo"] = {"parentAgentId": OTHER_SESSION_ID, "typeName": "fixture"}
    conn.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps(meta0).encode().hex(),))
    conn.commit()
    if not keep_open:
        conn.close()

    meta_path = session_dir / "meta.json"
    if write_meta_json:
        meta_json = {"schemaVersion": 1, "createdAtMs": created_ms, "updatedAtMs": updated_ms, "hasConversation": True}
        if include_cwd:
            meta_json["cwd"] = cwd
        meta_json.update(meta_json_extra or {})
        meta_path.write_text(json.dumps(meta_json))
    else:
        meta_path.unlink(missing_ok=True)
    return session_dir, (conn if keep_open else None)


def basic_root() -> list[dict]:
    return [
        system(),
        user_info(),
        human("Explain synthetic foxes\nwith detail", stamp=STAMP),
        reply("Looking into foxes.", reasoning="reasoning-secret", tool_call=True),
        tool_result(),
        reply("Foxes are synthetic here."),
    ]


def export(chats_dir: Path, out_dir: Path, state: dict | None = None, **kwargs) -> dict:
    return export_cursor_cli(
        out_dir,
        state if state is not None else {},
        chats_dir=chats_dir,
        full=kwargs.get("full", False),
        dry_run=kwargs.get("dry_run", False),
        since_date=kwargs.get("since_date"),
    )


def bodies(chats_dir: Path, session_id: str = SESSION_ID, cwd: str = CWD) -> list[tuple[str, str]]:
    record = parse_cursor_cli_session(chats_dir / hashlib.md5(cwd.encode()).hexdigest() / session_id)
    assert record is not None
    return [(m.role, m.content) for m in record.messages]


def only_markdown(out_dir: Path) -> str:
    files = list(out_dir.glob("*.md"))
    assert len(files) == 1
    return files[0].read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Protobuf walker and timestamps
# --------------------------------------------------------------------------- #


def test_root_refs_keeps_order_and_repeats_and_skips_unknown_fields() -> None:
    first, second = "11" * 32, "22" * 32
    data = (b"\x0a\x20" + bytes.fromhex(first) + b"\xb2\x01\x03cli" + b"\x0a\x20" + bytes.fromhex(second)
            + b"\x0a\x20" + bytes.fromhex(first) + b"\x0a\x03abc" + b"\x50\x96\x01")
    assert _root_refs(data) == [first, second, first]


@pytest.mark.parametrize(
    "data",
    [b"\x0a", b"\x0a\x20" + b"\x01" * 5, b"\x0b\x00", b"\x08\xff", b"\x09\x00\x00", b"\x00\x00"],
    ids=["truncated-length", "length-overrun", "group-wire-type", "truncated-varint", "fixed64-overrun", "field-zero"],
)
def test_root_refs_rejects_malformed(data: bytes) -> None:
    with pytest.raises(ValueError):
        _root_refs(data)


def test_human_timestamp_formats() -> None:
    assert _parse_human_timestamp(STAMP) == STAMP_MS
    india = datetime(2026, 6, 10, 17, 35, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert _parse_human_timestamp("Wednesday, Jun 10, 2026, 5:35 PM (UTC+5:30)") == int(india.timestamp() * 1000)
    assert _parse_human_timestamp("Wednesday, Jun 10, 2026, 12:05 PM (UTC)") == CREATED_MS + 5 * 60_000
    assert _parse_human_timestamp("2026-06-10T12:00:00Z") == CREATED_MS
    assert _parse_human_timestamp("sometime yesterday") is None


# --------------------------------------------------------------------------- #
# Adapter behavior
# --------------------------------------------------------------------------- #


def test_basic_export(tmp_path: Path) -> None:
    chats = tmp_path / "chats"
    write_session(chats, [basic_root()])
    state: dict = {}
    result = export(chats, tmp_path / "out", state)
    assert result == {"source": "cursor_cli", "scanned": 1, "exported": 1}

    rendered = only_markdown(tmp_path / "out")
    assert rendered.startswith("---\nsource: cursor_cli\n")
    assert f'session_id: "{SESSION_ID}"' in rendered
    assert 'title: "Explain synthetic foxes"' in rendered
    assert f'date: "{ms_to_date(CREATED_MS)}"' in rendered
    assert "message_count: 3" in rendered
    assert f'project_directory: "{CWD}"' in rendered
    assert f'models_used: ["{MODEL}"]' in rendered
    assert f'turn_models: ["{MODEL}", "{MODEL}", "{MODEL}"]' in rendered
    assert f"## User [{ms_to_hhmm(STAMP_MS)}]\n\nExplain synthetic foxes\nwith detail\n" in rendered
    assert "## Assistant\n\nLooking into foxes.\n\n## Assistant\n\nFoxes are synthetic here." in rendered
    for leaked in ("reasoning-secret", "tool-call-secret", "tool-result-secret", "fixture system prompt",
                   "<user_info>", "Workspace Path", "<timestamp>", "<user_query>", "fixture cached file"):
        assert leaked not in rendered

    record = parse_cursor_cli_session(chats / hashlib.md5(CWD.encode()).hexdigest() / SESSION_ID)
    assert record is not None and record.messages[0].time_created == STAMP_MS
    entry = state["cursor_cli"]["sessions"][SESSION_ID]
    assert entry["latest_timestamp"] == UPDATED_MS
    assert entry["output_file"].endswith(".md")
    assert len(entry["root_blob_id"]) == 64


def test_root_order_beats_rowid(tmp_path: Path) -> None:
    root = [system(), user_info(), human("first question"), reply("first answer", "more"),
            reply("second step"), human("second question"), reply("second answer")]
    write_session(tmp_path / "chats", [root], reverse_message_rows=True)
    assert bodies(tmp_path / "chats") == [
        ("user", "first question"),
        ("assistant", "first answer\n\nmore"),
        ("assistant", "second step"),
        ("user", "second question"),
        ("assistant", "second answer"),
    ]


def test_orphan_blobs_are_ignored(tmp_path: Path) -> None:
    write_session(tmp_path / "chats", [basic_root()],
                  extra_messages=(human("orphan-question"), reply("orphan-answer"), {"role": "reviewer", "note": "x"}))
    export(tmp_path / "chats", tmp_path / "out")
    rendered = only_markdown(tmp_path / "out")
    assert "orphan-question" not in rendered and "orphan-answer" not in rendered


def test_repeated_content_renders_each_reference(tmp_path: Path) -> None:
    root = [system(), user_info(), human("continue"), reply("first pass"), human("continue"), reply("second pass")]
    session_dir, _ = write_session(tmp_path / "chats", [root])
    with sqlite3.connect(session_dir / "store.db") as conn:
        rows = conn.execute("SELECT count(*) FROM blobs WHERE id = ?", (blob_id(encode(human("continue"))),)).fetchone()
    assert rows == (1,)
    assert bodies(tmp_path / "chats") == [
        ("user", "continue"), ("assistant", "first pass"), ("user", "continue"), ("assistant", "second pass"),
    ]


def compaction_roots() -> list[list[dict]]:
    sys_blob, ui = system(), user_info()
    h1, a1, h2, a2, h3, a3 = (human("q1"), reply("a1"), human("q2"), reply("a2"), human("q3"), reply("a3"))
    summarizer = [{"role": "user", "content": [{"type": "text", "text": "<user_query>summarizer-request-secret</user_query>"}]},
                  reply("summarizer-output-secret")]
    return [
        [sys_blob, ui, h1, a1],
        [sys_blob, ui, h1, a1, h2, a2],
        summarizer,
        [sys_blob, ui, summary(), h2, a2],
        [sys_blob, ui, summary(), h2, a2, h3, a3],
    ]


def test_compaction_stitches_older_snapshots(tmp_path: Path) -> None:
    write_session(tmp_path / "chats", compaction_roots())
    result = export(tmp_path / "chats", tmp_path / "out")
    assert result == {"source": "cursor_cli", "scanned": 1, "exported": 1}
    assert [content for _, content in bodies(tmp_path / "chats")] == ["q1", "a1", "q2", "a2", "q3", "a3"]
    rendered = only_markdown(tmp_path / "out")
    assert 'title: "q1"' in rendered
    for leaked in ("summary-secret", "summarizer-request-secret", "summarizer-output-secret"):
        assert leaked not in rendered


def test_compaction_falls_back_to_latest_root_with_warning(tmp_path: Path) -> None:
    write_session(tmp_path / "chats", compaction_roots(), reverse_root_rows=True)
    result = export(tmp_path / "chats", tmp_path / "out")
    assert result["exported"] == 1 and "failed" not in result
    assert [w["session_id"] for w in result["warnings"]] == [SESSION_ID]
    assert "fell back to the latest root" in result["warnings"][0]["error"]
    rendered = only_markdown(tmp_path / "out")
    assert "## User\n\nq2" in rendered and "q1" not in rendered and "summary-secret" not in rendered


def test_rewind_exports_latest_branch_only(tmp_path: Path) -> None:
    sys_blob, ui, h1, a1 = system(), user_info(), human("q1"), reply("a1")
    roots = [
        [sys_blob, ui, h1, a1, human("abandoned-question"), reply("abandoned-answer")],
        [sys_blob, ui, h1, a1, human("q2"), reply("a2")],
    ]
    write_session(tmp_path / "chats", roots)
    assert [content for _, content in bodies(tmp_path / "chats")] == ["q1", "a1", "q2", "a2"]


def test_injected_user_content_is_dropped(tmp_path: Path) -> None:
    root = [
        system(),
        user_info(),
        {"role": "user", "content": "<system_reminder>\nreminder-secret\n</system_reminder>"},
        {"role": "user", "content": f"<timestamp>{STAMP}</timestamp>\n<system_notification>notification-secret</system_notification>"},
        {"role": "user", "content": "<summary_content>tagged-summary-secret</summary_content>"},
        human("Real question", reminder="part-reminder-secret"),
        reply("Real answer"),
    ]
    write_session(tmp_path / "chats", [root])
    assert bodies(tmp_path / "chats") == [("user", "Real question"), ("assistant", "Real answer")]


def test_missing_meta_json_uses_meta0_and_verified_workspace_path(tmp_path: Path) -> None:
    chats = tmp_path / "chats"
    write_session(chats, [basic_root()], write_meta_json=False, created_ms=CREATED_MS - 86_400_000)
    record = parse_cursor_cli_session(chats / hashlib.md5(CWD.encode()).hexdigest() / SESSION_ID)
    assert record is not None
    assert record.date == ms_to_date(CREATED_MS - 86_400_000)
    assert record.project_directory == CWD

    mismatched = [system(), user_info("/home/user/other"), human("q"), reply("a")]
    write_session(chats, [mismatched], session_id=OTHER_SESSION_ID, include_cwd=False)
    other = parse_cursor_cli_session(chats / hashlib.md5(CWD.encode()).hexdigest() / OTHER_SESSION_ID)
    assert other is not None and other.project_directory == ""


def test_meta_json_created_at_wins_over_meta0(tmp_path: Path) -> None:
    write_session(tmp_path / "chats", [basic_root()], meta_json_extra={"createdAtMs": CREATED_MS + 3 * 86_400_000})
    record = parse_cursor_cli_session(tmp_path / "chats" / hashlib.md5(CWD.encode()).hexdigest() / SESSION_ID)
    assert record is not None and record.date == ms_to_date(CREATED_MS + 3 * 86_400_000)


def test_subagent_session_is_skipped(tmp_path: Path) -> None:
    write_session(tmp_path / "chats", [basic_root()], subagent=True)
    result = export(tmp_path / "chats", tmp_path / "out")
    assert result == {"source": "cursor_cli", "scanned": 1, "exported": 0}


def test_unsupported_schema_version_is_skipped_with_warning(tmp_path: Path) -> None:
    write_session(tmp_path / "chats", [basic_root()], meta_json_extra={"schemaVersion": 2})
    result = export(tmp_path / "chats", tmp_path / "out")
    assert result["exported"] == 0 and "failed" not in result
    assert result["warnings"] == [{"session_id": SESSION_ID, "error": "unsupported meta.json schemaVersion 2"}]


def test_empty_and_unusable_sessions_are_skipped(tmp_path: Path) -> None:
    chats = tmp_path / "chats"
    no_store = chats / hashlib.md5(b"/home/user/empty").hexdigest() / "00000000-0000-4000-8000-0000000000e1"
    no_store.mkdir(parents=True)
    (no_store / "meta.json").write_text(json.dumps({"schemaVersion": 1, "createdAtMs": CREATED_MS,
                                                    "updatedAtMs": CREATED_MS, "hasConversation": False}))
    write_session(chats, [basic_root()], session_id=OTHER_SESSION_ID, latest_root_id="ab" * 32)
    injected_only = [system(), user_info(), {"role": "user", "content": "<system_reminder>only</system_reminder>"},
                     reply("unprompted reply")]
    write_session(chats, [injected_only])
    result = export(chats, tmp_path / "out")
    assert result == {"source": "cursor_cli", "scanned": 2, "exported": 0}


def test_corrupt_root_is_isolated_and_retried_after_repair(tmp_path: Path) -> None:
    chats = tmp_path / "chats"
    write_session(chats, [basic_root()])
    write_session(chats, [b"\x0a\x20" + b"\x01" * 5], session_id=OTHER_SESSION_ID, cwd="/home/user/other")
    state: dict = {}
    result = export(chats, tmp_path / "out", state)
    assert result["exported"] == 1 and result["failed"] == 1
    assert [w["session_id"] for w in result["warnings"]] == [OTHER_SESSION_ID]
    assert result["warnings"][0]["error"].startswith("ValueError:")
    assert OTHER_SESSION_ID not in state["cursor_cli"]["sessions"]

    write_session(chats, [[system(), user_info("/home/user/other"), human("repaired"), reply("ok")]],
                  session_id=OTHER_SESSION_ID, cwd="/home/user/other")
    result = export(chats, tmp_path / "out", state)
    assert result == {"source": "cursor_cli", "scanned": 2, "exported": 1}
    assert OTHER_SESSION_ID in state["cursor_cli"]["sessions"]


def test_incremental_root_id_timestamp_full_and_dry_run(tmp_path: Path) -> None:
    chats, out = tmp_path / "chats", tmp_path / "out"
    write_session(chats, [basic_root()])
    state: dict = {}
    assert export(chats, out, state)["exported"] == 1
    assert export(chats, out, state)["exported"] == 0
    output_file = out / state["cursor_cli"]["sessions"][SESSION_ID]["output_file"]

    # New root, same updatedAtMs: the root id alone triggers a rewrite in place.
    grown = basic_root()
    write_session(chats, [grown, grown + [human("follow-up"), reply("follow-up answer")]])
    assert export(chats, out, state)["exported"] == 1
    assert list(out.glob("*.md")) == [output_file]
    assert "follow-up answer" in output_file.read_text(encoding="utf-8")

    # Same root, newer updatedAtMs.
    write_session(chats, [grown, grown + [human("follow-up"), reply("follow-up answer")]], updated_ms=UPDATED_MS + 1)
    assert export(chats, out, state)["exported"] == 1
    assert export(chats, out, state)["exported"] == 0

    assert export(chats, out, state, full=True)["exported"] == 1
    assert list(out.glob("*.md")) == [output_file]

    write_session(chats, [grown], updated_ms=UPDATED_MS + 2)
    before_state = copy.deepcopy(state)
    before_text = output_file.read_text(encoding="utf-8")
    assert export(chats, out, state, dry_run=True)["exported"] == 1
    assert state == before_state
    assert output_file.read_text(encoding="utf-8") == before_text


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    write_session(tmp_path / "chats", [basic_root()])
    state: dict = {}
    result = export(tmp_path / "chats", tmp_path / "out", state, dry_run=True)
    assert result["exported"] == 1
    assert not (tmp_path / "out").exists()
    assert state["cursor_cli"]["sessions"] == {}


def test_since_date_filters_old_sessions(tmp_path: Path) -> None:
    chats = tmp_path / "chats"
    write_session(chats, [basic_root()])
    write_session(chats, [[system(), user_info(), human("old question"), reply("old answer")]],
                  session_id=OTHER_SESSION_ID, created_ms=CREATED_MS - 30 * 86_400_000)
    since = date.fromisoformat(ms_to_date(CREATED_MS))
    result = export(chats, tmp_path / "out", since_date=since)
    assert result == {"source": "cursor_cli", "scanned": 2, "exported": 1}
    assert "old question" not in only_markdown(tmp_path / "out")


@pytest.mark.parametrize("journal", ["wal", "delete"])
def test_closed_store_opens_immutable_without_sidecars(tmp_path: Path, journal: str) -> None:
    session_dir, _ = write_session(tmp_path / "chats", [basic_root()], journal=journal)
    assert export(tmp_path / "chats", tmp_path / "out")["exported"] == 1
    assert sorted(path.name for path in session_dir.iterdir()) == ["meta.json", "store.db"]


def test_live_store_with_wal_opens_read_only(tmp_path: Path) -> None:
    session_dir, writer = write_session(tmp_path / "chats", [basic_root()], keep_open=True)
    assert writer is not None
    try:
        assert (session_dir / "store.db-wal").exists()
        uri = (session_dir / "store.db").resolve().as_uri()
        with pytest.raises(sqlite3.OperationalError):
            sqlite3.connect(f"{uri}?mode=ro&immutable=1", uri=True).execute("SELECT count(*) FROM blobs").fetchone()
        assert export(tmp_path / "chats", tmp_path / "out")["exported"] == 1
    finally:
        writer.close()


def test_missing_model_fingerprint_omits_model_fields(tmp_path: Path) -> None:
    write_session(tmp_path / "chats", [[system(model=None), user_info(), human("q"), reply("a")]])
    export(tmp_path / "chats", tmp_path / "out")
    rendered = only_markdown(tmp_path / "out")
    assert "models_used" not in rendered and "turn_models" not in rendered


def test_user_turn_without_reply_keeps_snapshot_model(tmp_path: Path) -> None:
    write_session(tmp_path / "chats", [[system(), user_info(), human("q"), reply(tool_call=True), tool_result()]])
    record = parse_cursor_cli_session(tmp_path / "chats" / hashlib.md5(CWD.encode()).hexdigest() / SESSION_ID)
    assert record is not None
    assert [(m.role, m.model) for m in record.messages] == [("user", MODEL)]


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_cli_registers_cursor_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert SOURCE_CHOICES[SOURCE_CHOICES.index("cursor") + 1] == "cursor-cli"
    monkeypatch.setattr(sys, "argv", ["export_sessions.py"])
    assert cli_module.parse_args().cursor_cli_dir == DEFAULT_CURSOR_CLI_DIR
    monkeypatch.setattr(sys, "argv", ["export_sessions.py", "--source", "cursor-cli", "--cursor-cli-dir", "/tmp/example-chats"])
    args = cli_module.parse_args()
    assert args.source == "cursor-cli" and args.cursor_cli_dir == Path("/tmp/example-chats")

    write_session(tmp_path / "chats", [basic_root()])
    results = run_export("cursor-cli", full=False, dry_run=False, base_dir=tmp_path / "archive",
                         state_file=tmp_path / "state.json", cursor_cli_dir=tmp_path / "chats")
    assert results == [{"source": "cursor_cli", "scanned": 1, "exported": 1}]
    assert list((tmp_path / "archive" / "cursor_cli").glob("*.md"))
    assert SESSION_ID in load_state(tmp_path / "state.json")["cursor_cli"]["sessions"]


# --------------------------------------------------------------------------- #
# Live end-to-end (real data; skipped unless AI_SESSION_EXPORT_LIVE=1)
# --------------------------------------------------------------------------- #


@pytest.mark.live_e2e
def test_live_cursor_cli_export(tmp_path: Path) -> None:
    """Export 7 days of real Cursor CLI sessions without exposing transcript content."""
    if not os.environ.get("AI_SESSION_EXPORT_LIVE"):
        pytest.skip("Set AI_SESSION_EXPORT_LIVE=1 to run live tests")
    if not DEFAULT_CURSOR_CLI_DIR.is_dir():
        pytest.skip(f"Cursor CLI chats directory not found: {DEFAULT_CURSOR_CLI_DIR}")

    since = date.today() - timedelta(days=7)
    state: dict = {}
    dry = export_cursor_cli(tmp_path / "dry", {}, chats_dir=DEFAULT_CURSOR_CLI_DIR, full=True, dry_run=True,
                            since_date=since)
    assert not (tmp_path / "dry").exists()
    result = export_cursor_cli(tmp_path / "cursor_cli", state, chats_dir=DEFAULT_CURSOR_CLI_DIR, full=False,
                               dry_run=False, since_date=since)
    if result["exported"] == 0:
        pytest.skip("No recent Cursor CLI sessions to export")
    assert result["exported"] == dry["exported"]
    files = list((tmp_path / "cursor_cli").glob("*.md"))
    assert len(files) == result["exported"]
    assert files[0].read_text(encoding="utf-8").startswith("---\nsource: cursor_cli\n")
    again = export_cursor_cli(tmp_path / "cursor_cli", state, chats_dir=DEFAULT_CURSOR_CLI_DIR, full=False,
                              dry_run=False, since_date=since)
    # Only stores with a live writer (`-wal` present) may legitimately grow between runs.
    assert again["exported"] <= sum(1 for _ in DEFAULT_CURSOR_CLI_DIR.glob("*/*/store.db-wal"))
