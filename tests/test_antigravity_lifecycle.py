from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ai_session_export.cli import main as cli_main
from ai_session_export.sources.antigravity import (
    MalformedTranscriptError,
    _encode_length_delimited,
    _encode_timestamp_message,
    _encode_varint_field,
    _parse_pbtxt_annotation,
    _parse_raw_summary_proto,
    _update_pbtxt_archived,
    export_antigravity,
    list_antigravity_sessions,
    parse_antigravity_session,
    set_antigravity_archived,
)


def _build_synthetic_raw_summary(
    summary_title: str,
    workspace_uri: str,
    *,
    archived: bool,
    archival_ts_s: int = 1_786_000_000,
    step_count: int = 4,
    annotation_title: str = "",
) -> bytes:
    """Construct a synthetic CascadeTrajectorySummary protobuf payload."""
    parts: list[bytes] = []
    if summary_title:
        parts.append(_encode_length_delimited(1, summary_title.encode("utf-8")))
    parts.append(_encode_varint_field(2, step_count))
    if workspace_uri:
        ws_msg = _encode_length_delimited(1, workspace_uri.encode("utf-8"))
        parts.append(_encode_length_delimited(9, ws_msg))

    ann_parts: list[bytes] = []
    if annotation_title:
        ann_parts.append(_encode_length_delimited(1, annotation_title.encode("utf-8")))
    ann_parts.append(_encode_varint_field(4, 1 if archived else 0))
    ann_parts.append(_encode_length_delimited(5, _encode_timestamp_message(archival_ts_s, 123000)))
    ann_parts.append(_encode_length_delimited(7, _encode_timestamp_message(archival_ts_s + 1, 0)))
    parts.append(_encode_length_delimited(15, b"".join(ann_parts)))
    return b"".join(parts)


def _build_synthetic_agyhub_proto(entries: list[tuple[str, bytes]]) -> bytes:
    out: list[bytes] = []
    for cid, raw_summary in entries:
        entry = _encode_length_delimited(1, cid.encode("utf-8")) + _encode_length_delimited(
            2, raw_summary
        )
        out.append(_encode_length_delimited(1, entry))
    return b"".join(out)


def _write_transcript(
    brain_dir: Path,
    session_id: str,
    user_text: str = "Fix the auth bug",
    assistant_text: str = "I inspected the auth handler.",
) -> Path:
    logs_dir = brain_dir / session_id / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    transcript = logs_dir / "transcript_full.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "step_index": 0,
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "status": "DONE",
                        "created_at": "2026-08-10T18:00:00Z",
                        "content": f"<USER_REQUEST>\n{user_text}\n</USER_REQUEST>",
                    }
                ),
                json.dumps(
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-08-10T18:00:05Z",
                        "content": assistant_text,
                        "thinking": "Checking token expiration edge cases.",
                        "tool_calls": [{"name": "read_file", "args": {"Path": "/home/example/demo/auth.py"}}],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return transcript


def _seed_surface_root(surface_root: Path) -> Path:
    """Create a complete synthetic Antigravity surface root with brain/, annotations/, SQLite DB, and hub proto."""
    brain_dir = surface_root / "brain"
    ann_dir = surface_root / "annotations"
    brain_dir.mkdir(parents=True, exist_ok=True)
    ann_dir.mkdir(parents=True, exist_ok=True)

    _write_transcript(brain_dir, "ses-active-1", "First user question", "First assistant reply")
    _write_transcript(brain_dir, "ses-archived-1", "Old archived question", "Old archived reply")
    _write_transcript(brain_dir, "ses-subagent-1", "Subagent task prompt", "Subagent result")

    (ann_dir / "ses-active-1.pbtxt").write_text(
        'title:"Active Architecture Review" last_user_view_time:{seconds:1786000100 nanos:0}\n',
        encoding="utf-8",
    )
    (ann_dir / "ses-archived-1.pbtxt").write_text(
        "archived:true archival_status_timestamp:{seconds:1786000200 nanos:500} "
        "last_user_view_time:{seconds:1786000201 nanos:0}\n",
        encoding="utf-8",
    )

    raw_active = _build_synthetic_raw_summary(
        "Active Architecture Review",
        "file:///home/example/project-alpha",
        archived=False,
        archival_ts_s=1786000100,
    )
    raw_archived = _build_synthetic_raw_summary(
        "Archived Security Audit",
        "file:///home/example/project-beta",
        archived=True,
        archival_ts_s=1786000200,
    )
    raw_subagent = _build_synthetic_raw_summary(
        "Subagent Helper",
        "file:///home/example/project-alpha",
        archived=False,
        archival_ts_s=1786000150,
    )

    db_path = surface_root / "conversation_summaries.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE conversation_summaries (
                conversation_id text PRIMARY KEY,
                title text NOT NULL DEFAULT "",
                step_count integer NOT NULL DEFAULT 0,
                last_modified_time datetime NOT NULL,
                workspace_uris text NOT NULL,
                parent_conversation_id text NOT NULL DEFAULT "",
                raw_summary blob
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE conversations_fts USING fts5(
                conversation_id UNINDEXED,
                chunk_index UNINDEXED,
                last_modified_time UNINDEXED,
                step_count UNINDEXED,
                archived UNINDEXED,
                title,
                workspace,
                user_body,
                agent_body
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO conversation_summaries
            (conversation_id, title, step_count, last_modified_time, workspace_uris, parent_conversation_id, raw_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "ses-active-1",
                    "Active Architecture Review",
                    4,
                    "2026-08-10T18:00:05Z",
                    '["file:///home/example/project-alpha"]',
                    "",
                    raw_active,
                ),
                (
                    "ses-archived-1",
                    "",
                    6,
                    "2026-08-10T18:00:05Z",
                    '["file:///home/example/project-beta"]',
                    "",
                    raw_archived,
                ),
                (
                    "ses-subagent-1",
                    "Subagent Helper",
                    2,
                    "2026-08-10T18:00:05Z",
                    '["file:///home/example/project-alpha"]',
                    "ses-active-1",
                    raw_subagent,
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO conversations_fts
            (conversation_id, chunk_index, last_modified_time, step_count, archived, title, workspace, user_body, agent_body)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "ses-active-1",
                    -1,
                    "2026-08-10T18:00:05Z",
                    4,
                    0,
                    "Active Architecture Review",
                    "file:///home/example/project-alpha",
                    "",
                    "",
                ),
                (
                    "ses-archived-1",
                    -1,
                    "2026-08-10T18:00:05Z",
                    6,
                    1,
                    "Archived Security Audit",
                    "file:///home/example/project-beta",
                    "",
                    "",
                ),
            ],
        )
        conn.commit()

    (surface_root / "agyhub_summaries_proto.pb").write_bytes(
        _build_synthetic_agyhub_proto(
            [
                ("ses-active-1", raw_active),
                ("ses-archived-1", raw_archived),
            ]
        )
    )
    return brain_dir


def test_antigravity_export_enriches_title_and_project_from_metadata(tmp_path: Path) -> None:
    brain_dir = _seed_surface_root(tmp_path / "ag_root")
    output_dir = tmp_path / "out"
    result = export_antigravity(
        output_dir,
        {},
        brain_dirs={"2": brain_dir},
        full=True,
        dry_run=False,
        since_date=None,
    )
    assert result["exported"] == 3

    by_id: dict[str, str] = {}
    for md_file in output_dir.glob("*.md"):
        content = md_file.read_text(encoding="utf-8")
        for line in content.splitlines():
            if line.startswith("session_id:"):
                sid = json.loads(line.split(":", 1)[1].strip())
                by_id[sid] = content
                break

    assert 'title: "Active Architecture Review"' in by_id["ses-active-1"]
    assert 'project_directory: "/home/example/project-alpha"' in by_id["ses-active-1"]
    assert 'title: "Archived Security Audit"' in by_id["ses-archived-1"]
    assert 'project_directory: "/home/example/project-beta"' in by_id["ses-archived-1"]


def test_list_antigravity_sessions_filters_status_query_and_subagents(tmp_path: Path) -> None:
    brain_dir = _seed_surface_root(tmp_path / "ag_root")

    all_root = list_antigravity_sessions(brain_dirs={"2": brain_dir}, status="all")
    assert [s["session_id"] for s in all_root] == ["ses-active-1", "ses-archived-1"]

    with_sub = list_antigravity_sessions(
        brain_dirs={"2": brain_dir}, status="all", include_subagents=True
    )
    assert [s["session_id"] for s in with_sub] == [
        "ses-active-1",
        "ses-archived-1",
        "ses-subagent-1",
    ]

    only_active = list_antigravity_sessions(brain_dirs={"2": brain_dir}, status="active")
    assert [s["session_id"] for s in only_active] == ["ses-active-1"]
    assert only_active[0]["archived"] is False

    only_archived = list_antigravity_sessions(brain_dirs={"2": brain_dir}, status="archived")
    assert [s["session_id"] for s in only_archived] == ["ses-archived-1"]
    assert only_archived[0]["archived"] is True
    assert only_archived[0]["title"] == "Archived Security Audit"

    queried = list_antigravity_sessions(brain_dirs={"2": brain_dir}, query="security")
    assert [s["session_id"] for s in queried] == ["ses-archived-1"]


def test_parse_antigravity_session_reads_archived_and_optional_tracks(tmp_path: Path) -> None:
    brain_dir = _seed_surface_root(tmp_path / "ag_root")

    parsed = parse_antigravity_session(
        "ses-archived-1",
        brain_dirs={"2": brain_dir},
        include_thinking=True,
        include_tool_calls=True,
    )
    assert parsed["session_id"] == "ses-archived-1"
    assert parsed["archived"] is True
    assert parsed["title"] == "Archived Security Audit"
    assert parsed["project_directory"] == "/home/example/project-beta"
    assert parsed["message_count"] == 2
    assert "# Archived Security Audit" in parsed["markdown"]
    assert "Old archived reply" in parsed["markdown"]
    assert len(parsed["steps"]) == 2
    assert parsed["steps"][1]["thinking"] == "Checking token expiration edge cases."
    assert parsed["steps"][1]["tool_calls"][0]["name"] == "read_file"

    with pytest.raises(FileNotFoundError):
        parse_antigravity_session("nonexistent-session", brain_dirs={"2": brain_dir})


def test_set_antigravity_archived_roundtrip_and_dry_run(tmp_path: Path) -> None:
    surface_root = tmp_path / "ag_root"
    brain_dir = _seed_surface_root(surface_root)

    # 1. Dry-run archive on active session does not mutate disk
    dry = set_antigravity_archived(
        ["ses-active-1"],
        archived=True,
        brain_dirs={"2": brain_dir},
        dry_run=True,
        timestamp_s=1_790_000_000,
    )
    assert dry[0]["status"] == "dry_run"
    assert dry[0]["previous_archived"] is False
    still_active = parse_antigravity_session("ses-active-1", brain_dirs={"2": brain_dir})
    assert still_active["archived"] is False

    # 2. Real archive updates .pbtxt, SQLite FTS + raw_summary, and agyhub_summaries_proto.pb
    archived_res = set_antigravity_archived(
        ["ses-active-1", "missing-id"],
        archived=True,
        brain_dirs={"2": brain_dir},
        dry_run=False,
        timestamp_s=1_790_000_111,
    )
    assert archived_res[0]["status"] == "verified"
    assert archived_res[0]["previous_archived"] is False
    assert archived_res[0]["archived"] is True
    assert set(archived_res[0]["stores_updated"]) == {
        "annotations_pbtxt",
        "conversation_summaries_db",
        "agyhub_summaries_proto_pb",
    }
    assert archived_res[1]["status"] == "not_found"

    pbtxt_after = (surface_root / "annotations" / "ses-active-1.pbtxt").read_text(encoding="utf-8")
    assert "archived:true" in pbtxt_after
    assert "archival_status_timestamp:{seconds:1790000111 nanos:0}" in pbtxt_after
    # Existing title and last_user_view_time preserved
    assert 'title:"Active Architecture Review"' in pbtxt_after
    assert "last_user_view_time:{seconds:1786000100 nanos:0}" in pbtxt_after

    # Verify hub proto was also updated in-place
    hub_bytes = (surface_root / "agyhub_summaries_proto.pb").read_bytes()
    assert b"Active Architecture Review" in hub_bytes

    after_parse = parse_antigravity_session("ses-active-1", brain_dirs={"2": brain_dir})
    assert after_parse["archived"] is True
    assert after_parse["archival_status_timestamp_s"] == 1_790_000_111

    # 3. Unarchive restores active state across all stores
    unarchived_res = set_antigravity_archived(
        ["ses-active-1", "ses-archived-1"],
        archived=False,
        brain_dirs={"2": brain_dir},
        dry_run=False,
        timestamp_s=1_790_000_222,
    )
    assert all(item["status"] == "verified" for item in unarchived_res)
    assert all(item["archived"] is False for item in unarchived_res)

    pbtxt_unarch = (surface_root / "annotations" / "ses-archived-1.pbtxt").read_text(encoding="utf-8")
    assert "archived:false" in pbtxt_unarch
    assert "archival_status_timestamp:{seconds:1790000222 nanos:0}" in pbtxt_unarch

    with sqlite3.connect(surface_root / "conversation_summaries.db") as conn:
        fts_val = conn.execute(
            "SELECT archived FROM conversations_fts WHERE conversation_id = 'ses-archived-1' AND chunk_index = -1"
        ).fetchone()[0]
        assert int(fts_val) == 0
        raw_blob = conn.execute(
            "SELECT raw_summary FROM conversation_summaries WHERE conversation_id = 'ses-archived-1'"
        ).fetchone()[0]
        parsed_proto = _parse_raw_summary_proto(raw_blob)
        assert parsed_proto["annotations"]["archived"] is False
        assert parsed_proto["annotations"]["archival_status_timestamp_s"] == 1_790_000_222


def test_cli_antigravity_list_parse_archive_and_unarchive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    brain_dir = _seed_surface_root(tmp_path / "ag_root")

    # --antigravity-list --json
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_sessions.py",
            "--antigravity-dir",
            str(brain_dir),
            "--antigravity-list",
            "--antigravity-status",
            "archived",
            "--json",
        ],
    )
    cli_main()
    list_out = json.loads(capsys.readouterr().out)
    assert list_out["count"] == 1
    assert list_out["sessions"][0]["session_id"] == "ses-archived-1"

    # --antigravity-parse --json
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_sessions.py",
            "--antigravity-dir",
            str(brain_dir),
            "--antigravity-parse",
            "ses-archived-1",
            "--include-thinking",
            "--json",
        ],
    )
    cli_main()
    parse_out = json.loads(capsys.readouterr().out)
    assert parse_out["session_id"] == "ses-archived-1"
    assert parse_out["archived"] is True
    assert parse_out["steps"][1]["thinking"] == "Checking token expiration edge cases."

    # --antigravity-unarchive --json
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_sessions.py",
            "--antigravity-dir",
            str(brain_dir),
            "--antigravity-unarchive",
            "ses-archived-1",
            "--json",
        ],
    )
    cli_main()
    unarch_out = json.loads(capsys.readouterr().out)
    assert unarch_out["results"][0]["status"] == "verified"
    assert unarch_out["results"][0]["archived"] is False

    # --antigravity-archive --json
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_sessions.py",
            "--antigravity-dir",
            str(brain_dir),
            "--antigravity-archive",
            "ses-archived-1",
            "--json",
        ],
    )
    cli_main()
    arch_out = json.loads(capsys.readouterr().out)
    assert arch_out["results"][0]["status"] == "verified"
    assert arch_out["results"][0]["archived"] is True


def test_pbtxt_quoted_title_with_archived_substring_is_not_corrupted() -> None:
    pbtxt = (
        'title:"Fix bug where archived: false or archival_status_timestamp:{seconds:1 nanos:2} breaks" '
        "archived:false "
        "archival_status_timestamp:{seconds:1786000000 nanos:0}"
    )
    parsed = _parse_pbtxt_annotation(pbtxt)
    assert parsed["title"] == (
        "Fix bug where archived: false or archival_status_timestamp:{seconds:1 nanos:2} breaks"
    )
    assert parsed["archived"] is False
    assert parsed["archival_status_timestamp_s"] == 1_786_000_000

    updated = _update_pbtxt_archived(pbtxt, True, 1_790_000_555, 0)
    reparsed = _parse_pbtxt_annotation(updated)
    assert reparsed["title"] == (
        "Fix bug where archived: false or archival_status_timestamp:{seconds:1 nanos:2} breaks"
    )
    assert reparsed["archived"] is True
    assert reparsed["archival_status_timestamp_s"] == 1_790_000_555


def test_incremental_export_reexports_when_metadata_title_or_project_changes(
    tmp_path: Path,
) -> None:
    surface_root = tmp_path / "ag_root"
    brain_dir = _seed_surface_root(surface_root)
    output_dir = tmp_path / "out"
    state: dict[str, object] = {}

    first = export_antigravity(
        output_dir,
        state,
        brain_dirs={"2": brain_dir},
        full=False,
        dry_run=False,
        since_date=None,
    )
    assert first["exported"] == 3

    # Second run with no changes exports 0
    second = export_antigravity(
        output_dir,
        state,
        brain_dirs={"2": brain_dir},
        full=False,
        dry_run=False,
        since_date=None,
    )
    assert second["exported"] == 0

    # Mutate only the annotation title without touching transcript_full.jsonl
    (surface_root / "annotations" / "ses-active-1.pbtxt").write_text(
        'title:"Renamed Architecture Title" archived:false',
        encoding="utf-8",
    )
    third = export_antigravity(
        output_dir,
        state,
        brain_dirs={"2": brain_dir},
        full=False,
        dry_run=False,
        since_date=None,
    )
    assert third["exported"] == 1

    # Verify the updated title is written into the exported markdown
    exported_texts = [p.read_text(encoding="utf-8") for p in output_dir.glob("*.md")]
    assert any('title: "Renamed Architecture Title"' in text for text in exported_texts)


def test_compact_transcript_fallback_works_for_export_and_parse(tmp_path: Path) -> None:
    surface_root = tmp_path / "ag_compact"
    brain_dir = surface_root / "brain"
    logs_dir = brain_dir / "ses-compact-only" / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    compact_path = logs_dir / "transcript.jsonl"
    compact_path.write_text(
        json.dumps(
            {
                "step_index": 1,
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": "2026-08-10T18:00:00Z",
                "content": "<USER_REQUEST>\nCompact fallback question\n</USER_REQUEST>",
            }
        )
        + "\n"
        + json.dumps(
            {
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "created_at": "2026-08-10T18:00:05Z",
                "content": "Compact fallback answer",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    out_dir = tmp_path / "out"
    res = export_antigravity(
        out_dir,
        {},
        brain_dirs={"2": brain_dir},
        full=True,
        dry_run=False,
        since_date=None,
    )
    assert res["exported"] == 1

    parsed = parse_antigravity_session("ses-compact-only", brain_dirs={"2": brain_dir})
    assert parsed["message_count"] == 2
    assert "Compact fallback answer" in parsed["markdown"]


def test_corrupt_raw_summary_marks_failed_verification(tmp_path: Path) -> None:
    surface_root = tmp_path / "ag_root"
    brain_dir = _seed_surface_root(surface_root)

    # Corrupt raw_summary in conversation_summaries with truncated varint
    with sqlite3.connect(surface_root / "conversation_summaries.db") as conn:
        conn.execute(
            "UPDATE conversation_summaries SET raw_summary = ? WHERE conversation_id = 'ses-active-1'",
            (b"\x80",),
        )
        conn.commit()

    res = set_antigravity_archived(
        ["ses-active-1"],
        archived=True,
        brain_dirs={"2": brain_dir},
        dry_run=False,
    )
    assert res[0]["status"] == "failed_verification"
    assert "conversation_summaries_db" not in res[0]["stores_updated"]


def test_malformed_transcript_with_include_thinking_and_cli_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    surface_root = tmp_path / "ag_root"
    brain_dir = _seed_surface_root(surface_root)

    # Corrupt transcript_full.jsonl for ses-active-1
    bad_transcript = (
        brain_dir / "ses-active-1" / ".system_generated" / "logs" / "transcript_full.jsonl"
    )
    bad_transcript.write_text("{not-valid-json\n", encoding="utf-8")

    with pytest.raises(MalformedTranscriptError):
        parse_antigravity_session(
            "ses-active-1",
            brain_dirs={"2": brain_dir},
            include_thinking=True,
        )

    # CLI exits 1 on malformed transcript in --antigravity-parse
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_sessions.py",
            "--antigravity-dir",
            str(brain_dir),
            "--antigravity-parse",
            "ses-active-1",
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        cli_main()
    assert exc_info.value.code == 1

    # CLI exits 2 when combining mutually exclusive --antigravity-* actions
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_sessions.py",
            "--antigravity-list",
            "--antigravity-parse",
            "ses-active-1",
        ],
    )
    with pytest.raises(SystemExit) as exc_info2:
        cli_main()
    assert exc_info2.value.code == 2
