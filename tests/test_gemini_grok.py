from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_session_export.sources.gemini import export_gemini, parse_gemini_session_file
from ai_session_export.sources.grok import export_grok, parse_grok_session_file


TIME = "2026-06-10T12:00:00Z"


def gemini_message(message_id, kind, text, **extra):
    return {"id": message_id, "timestamp": TIME, "type": kind, "content": text, **extra}


def gemini_fixture(root: Path, suffix=".json") -> Path:
    path = root / "example-project-hash" / "chats" / ("session-example" + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "sessionId": "gemini-fixture", "projectHash": "example-project-hash",
        "startTime": TIME, "lastUpdated": TIME,
        "messages": [
            gemini_message("u1", "user", "expanded prompt with machine context", displayContent="Explain synthetic foxes"),
            gemini_message("a1", "gemini", [{"text": "A fox "}, {"text": "can be synthetic."},
                                          {"text": "thought-secret", "thought": True},
                                          {"functionCall": {"name": "tool-secret"}}],
                           model="gemini-example-1", thoughts=[{"text": "thought-secret"}],
                           toolCalls=[{"result": "tool-secret"}]),
            gemini_message("info1", "info", "info-secret"),
            gemini_message("u2", "user", [{"text": "And owls?"}, {"inlineData": {"data": "binary-secret"}}]),
            gemini_message("a2", "gemini", "Owls too.", model="gemini-example-2"),
        ],
    }
    if suffix == ".json":
        path.write_text(json.dumps(data, indent=2))
    else:
        messages = data.pop("messages")
        path.write_text("\n".join(map(json.dumps, [data, *messages])) + "\n")
    return path


def grok_event(kind, text="", *, index=None, timestamp=1781092800, model=None, **extra):
    update = {"sessionUpdate": kind, **extra}
    if kind.endswith("_chunk"):
        update["content"] = {"type": "text", "text": text}
    meta = {}
    if index is not None:
        meta["promptIndex"] = index
    if model is not None:
        meta["modelId"] = model
    if meta:
        update["_meta"] = meta
    return {"timestamp": timestamp, "method": "session/update",
            "params": {"sessionId": "grok-fixture", "update": update}}


def grok_fixture(root: Path) -> Path:
    path = root / "%2Fexample%2Fproject" / "grok-fixture" / "updates.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_name("summary.json").write_text(json.dumps({
        "info": {"id": "grok-fixture", "cwd": "/example/project"},
        "created_at": TIME, "updated_at": TIME, "session_summary": "Synthetic foxes",
        "generated_title": "Grok fixture", "current_model_id": "grok-example",
        "session_kind": "fork", "parent_session_id": "original-session",
    }))
    events = [
        grok_event("user_message_chunk", "Explain ", index=0),
        grok_event("user_message_chunk", "synthetic foxes", index=0),
        grok_event("agent_thought_chunk", "thought-secret"),
        grok_event("agent_message_chunk", "A fox "),
        grok_event("agent_message_chunk", "can be synthetic."),
        grok_event("tool_call", title="tool-secret"),
        grok_event("user_message_chunk", "And owls?", index=1),
        grok_event("agent_message_chunk", "Owls too."),
    ]
    path.write_text("\n".join(map(json.dumps, events)) + "\n")
    return path


@pytest.mark.parametrize("suffix", [".json", ".jsonl"])
def test_gemini_public_formats_filter_and_attribute(suffix, tmp_path):
    record = parse_gemini_session_file(gemini_fixture(tmp_path, suffix))
    assert record.session_id == "gemini-fixture"
    assert record.date == "2026-06-10"
    assert record.project_directory == ""
    assert [m.content for m in record.messages] == ["Explain synthetic foxes", "A fox can be synthetic.", "And owls?", "Owls too."]
    assert [m.model for m in record.messages] == ["gemini-example-1"] * 2 + ["gemini-example-2"] * 2
    assert all(m.time_created for m in record.messages)


def test_gemini_jsonl_replacements_rewind_and_checkpoint(tmp_path):
    path = gemini_fixture(tmp_path, ".jsonl")
    with path.open("a") as handle:
        for update in [gemini_message("a2", "gemini", "Updated owl.", model="gemini-example-2"),
                       {"$rewindTo": "u2"}, gemini_message("u3", "user", "New question")]:
            handle.write(json.dumps(update) + "\n")
        handle.write('{"incomplete":')
    record = parse_gemini_session_file(path)
    assert [m.content for m in record.messages] == ["Explain synthetic foxes", "A fox can be synthetic.", "New question"]
    with path.open("a") as handle:
        handle.write("\n" + json.dumps({"$set": {"summary": "Checkpoint title", "messages": [
            gemini_message("u4", "user", "Checkpoint question"), gemini_message("a4", "gemini", "Checkpoint answer")
        ]}}) + "\n")
    record = parse_gemini_session_file(path)
    assert record.title == "Checkpoint title"
    assert [m.content for m in record.messages] == ["Checkpoint question", "Checkpoint answer"]


def test_gemini_missing_model_does_not_leak_future_model(tmp_path):
    path = gemini_fixture(tmp_path)
    data = json.loads(path.read_text())
    del data["messages"][1]["model"]
    path.write_text(json.dumps(data))
    record = parse_gemini_session_file(path)
    assert [m.model for m in record.messages] == [None, None, "gemini-example-2", "gemini-example-2"]


def test_gemini_prefers_migrated_jsonl_and_skips_subagents(tmp_path):
    gemini_fixture(tmp_path)
    path = gemini_fixture(tmp_path, ".jsonl")
    with path.open("a") as handle:
        handle.write(json.dumps({"$set": {"kind": "subagent"}}) + "\n")
    assert export_gemini(tmp_path / "out", {}, full=False, dry_run=False, since_date=None,
                         gemini_dir=tmp_path)["exported"] == 0


def test_grok_public_format_stitches_and_keeps_forks(tmp_path):
    record = parse_grok_session_file(grok_fixture(tmp_path))
    assert record.session_id == "grok-fixture"
    assert record.project_directory == "/example/project"
    assert record.title == "Grok fixture"
    assert [m.content for m in record.messages] == ["Explain synthetic foxes", "A fox can be synthetic.", "And owls?", "Owls too."]
    assert record.models_used == ["grok-example"]
    assert all(m.time_created for m in record.messages)


def test_gemini_ignores_machine_injected_user_content(tmp_path):
    path = gemini_fixture(tmp_path)
    data = json.loads(path.read_text())
    data["messages"].insert(1, gemini_message("ctx1", "user", "<session_context>PRIVATE MACHINE CONTEXT</session_context>"))
    data["messages"].insert(2, gemini_message("hook1", "user", "<hook_context>HOOK SECRET</hook_context>"))
    data["messages"].insert(3, gemini_message("cmd1", "user", "/help"))
    path.write_text(json.dumps(data))
    record = parse_gemini_session_file(path)
    assert [m.content for m in record.messages] == ["Explain synthetic foxes", "A fox can be synthetic.", "And owls?", "Owls too."]
    assert "PRIVATE MACHINE CONTEXT" not in str(record)


def test_gemini_empty_display_content_falls_back_to_content(tmp_path):
    path = gemini_fixture(tmp_path)
    data = json.loads(path.read_text())
    data["messages"][0]["displayContent"] = ""
    path.write_text(json.dumps(data))
    record = parse_gemini_session_file(path)
    assert record.messages[0].content == "expanded prompt with machine context"


def test_gemini_partial_migration_prefers_complete_json(tmp_path):
    json_path = gemini_fixture(tmp_path, ".json")
    jsonl_path = json_path.with_suffix(".jsonl")
    jsonl_path.write_text("\n".join(map(json.dumps, [
        {"sessionId": "gemini-fixture", "startTime": TIME, "lastUpdated": TIME},
        gemini_message("u1", "user", "Explain synthetic foxes"),
    ])) + "\n")
    result = export_gemini(tmp_path / "out", {}, full=False, dry_run=False, since_date=None, gemini_dir=tmp_path)
    assert result["scanned"] == 1 and result["exported"] == 1
    assert result.get("migration_fallbacks") == 1
    exported = next((tmp_path / "out").glob("*.md"))
    assert "Owls too." in exported.read_text()
    jsonl_path.write_text("\n".join(map(json.dumps, [
        {"sessionId": "gemini-fixture", "startTime": TIME, "lastUpdated": TIME},
        gemini_message("u1", "user", "Explain synthetic foxes", displayContent="Explain synthetic foxes"),
        gemini_message("a1", "gemini", "A fox can be synthetic.", model="gemini-example-1"),
        gemini_message("info1", "info", "info"),
        gemini_message("u2", "user", "And owls?"),
        gemini_message("a2", "gemini", "Owls too.", model="gemini-example-2"),
        gemini_message("u3", "user", "New follow-up after migration"),
    ])) + "\n")
    state = {"gemini": {"sessions": {"gemini-fixture": {"output_file": exported.name}}}}
    result = export_gemini(tmp_path / "out", state, full=False, dry_run=False, since_date=None, gemini_dir=tmp_path)
    assert result["exported"] == 1
    assert result.get("migration_fallbacks") is None
    assert result["scanned"] == 1
    assert "New follow-up after migration" in exported.read_text()


def test_grok_hides_model_only_prompt_echoes(tmp_path):
    path = grok_fixture(tmp_path)
    hidden = grok_event("user_message_chunk", "PRIVATE MODEL-ONLY BODY", index=2)
    hidden["params"]["_meta"] = {"promptId": "task-completed-bg-1"}
    with path.open("a") as handle:
        handle.write(json.dumps(hidden) + "\n")
    record = parse_grok_session_file(path)
    assert [m.content for m in record.messages] == ["Explain synthetic foxes", "A fox can be synthetic.", "And owls?", "Owls too."]
    assert "PRIVATE MODEL-ONLY BODY" not in str(record)


def test_grok_hides_legacy_auto_wake_text(tmp_path):
    path = grok_fixture(tmp_path)
    reminder = grok_event("user_message_chunk", "<system-reminder>cron-secret</system-reminder>", index=2)
    monitor = grok_event("user_message_chunk", "3 monitor events from background (use /events to view)", index=2)
    divider = grok_event("user_message_chunk", "---", index=2)
    visible = grok_event("user_message_chunk", "And owls? (real)", index=2)
    with path.open("a") as handle:
        for event in [reminder, monitor, divider, visible]:
            handle.write(json.dumps(event) + "\n")
    record = parse_grok_session_file(path)
    assert [m.content for m in record.messages if m.role == "user"] == ["Explain synthetic foxes", "And owls?", "And owls? (real)"]
    assert "cron-secret" not in str(record)


def test_grok_prefix_matches_subagent_kinds(tmp_path):
    path = grok_fixture(tmp_path)
    summary = json.loads(path.with_name("summary.json").read_text())
    summary["session_kind"] = "subagent_resume"
    path.with_name("summary.json").write_text(json.dumps(summary))
    assert parse_grok_session_file(path) is None


def test_grok_attributes_per_turn_models(tmp_path):
    path = grok_fixture(tmp_path)
    events = [
        grok_event("user_message_chunk", "Explain ", index=0, model="grok-example"),
        grok_event("user_message_chunk", "synthetic foxes", index=0, model="grok-example"),
        grok_event("agent_message_chunk", "A fox ", model="grok-example"),
        grok_event("agent_message_chunk", "can be synthetic.", model="grok-example"),
        grok_event("user_message_chunk", "And owls?", index=1, model="grok-example"),
        grok_event("agent_message_chunk", "Owls too.", model="grok-example"),
    ]
    path.write_text("\n".join(map(json.dumps, events)) + "\n")
    record = parse_grok_session_file(path)
    assert [m.model for m in record.messages] == ["grok-example"] * 4
    assert record.models_used == ["grok-example"]


def test_grok_model_switch_mid_session(tmp_path):
    path = grok_fixture(tmp_path)
    events = [
        grok_event("user_message_chunk", "Explain ", index=0, model="grok-example"),
        grok_event("user_message_chunk", "synthetic foxes", index=0, model="grok-example"),
        grok_event("agent_message_chunk", "A fox ", model="grok-example"),
        grok_event("agent_message_chunk", "can be synthetic.", model="grok-example"),
        grok_event("user_message_chunk", "And owls?", index=1, model="grok-next"),
        grok_event("agent_message_chunk", "Owls too."),
    ]
    path.write_text("\n".join(map(json.dumps, events)) + "\n")
    summary = json.loads(path.with_name("summary.json").read_text())
    summary["current_model_id"] = "grok-next"
    path.with_name("summary.json").write_text(json.dumps(summary))
    record = parse_grok_session_file(path)
    assert [m.model for m in record.messages] == ["grok-example", "grok-example", "grok-next", "grok-next"]
    assert record.models_used == ["grok-example", "grok-next"]


@pytest.mark.parametrize("source,writer,exporter,option", [
    ("gemini", gemini_fixture, export_gemini, "gemini_dir"),
    ("grok", grok_fixture, export_grok, "sessions_dir"),
])
def test_rewound_to_empty_session_retires_stale_archive(source, writer, exporter, option, tmp_path):
    root, output, state = tmp_path / "input", tmp_path / "output", {}
    path = writer(root)
    kwargs = {option: root, "full": False, "since_date": None}
    first = exporter(output, state, dry_run=False, **kwargs)
    assert first["exported"] == 1
    archived = next(output.glob("*.md"))
    assert "synthetic" in archived.read_text()
    if source == "gemini":
        # A JSON snapshot is rewritten in place; simulate the provider clearing it
        # after a rewind to the very first prompt.
        data = json.loads(path.read_text())
        data["messages"] = []
        path.write_text(json.dumps(data, indent=2))
    else:
        with path.open("a") as handle:
            handle.write(json.dumps({"method": "_x.ai/session/update", "params": {"sessionId": "grok-fixture",
                "update": {"sessionUpdate": "rewind_marker", "target_prompt_index": 0}}}) + "\n")
    dry = exporter(output, state, dry_run=True, **kwargs)
    assert dry["exported"] == 0
    assert any("stale archive" in warning["error"] for warning in dry.get("warnings", []))
    assert archived.is_file(), "dry-run must not delete"
    second = exporter(output, state, dry_run=False, **kwargs)
    assert second["exported"] == 0
    assert not archived.is_file()
    assert state[source]["sessions"] == {}


def test_export_failures_report_path_and_exception(tmp_path):
    root, output = tmp_path / "input", tmp_path / "output"
    grok_fixture(root)
    bad = root / "malformed" / "grok-malformed" / "updates.jsonl"
    bad.parent.mkdir(parents=True)
    bad.with_name("summary.json").write_text("{not json")
    bad.write_text("[]")
    result = export_grok(output, {}, full=False, dry_run=False, since_date=None, sessions_dir=root)
    assert result["failed"] == 1
    warning = result["warnings"][0]
    assert "malformed" in warning["path"] and "JSONDecodeError" in warning["error"]


def test_export_isolates_unexpected_per_session_exceptions(tmp_path):
    root, output = tmp_path / "input", tmp_path / "output"
    grok_fixture(root)
    bad = root / "overflow" / "grok-overflow" / "updates.jsonl"
    bad.parent.mkdir(parents=True)
    bad.with_name("summary.json").write_text(json.dumps({
        "info": {"id": "grok-overflow"}, "created_at": TIME,
    }))
    event = grok_event("user_message_chunk", "overflow", index=0)
    event["params"]["sessionId"] = "grok-overflow"
    event["timestamp"] = 1e309
    bad.write_text(json.dumps(event) + "\n")
    result = export_grok(output, {}, full=False, dry_run=False, since_date=None, sessions_dir=root)
    assert result["failed"] == 1
    assert "OverflowError" in result["warnings"][0]["error"]


@pytest.mark.parametrize("source,writer,exporter,option", [
    ("gemini", gemini_fixture, export_gemini, "gemini_dir"),
    ("grok", grok_fixture, export_grok, "sessions_dir"),
])
def test_noise_title_rename_does_not_retire_archive(source, writer, exporter, option, tmp_path):
    root, output, state = tmp_path / "input", tmp_path / "output", {}
    path = writer(root)
    kwargs = {option: root, "full": False, "since_date": None}
    assert exporter(output, state, dry_run=False, **kwargs)["exported"] == 1
    archived = next(output.glob("*.md"))
    if source == "gemini":
        data = json.loads(path.read_text())
        data["summary"] = "Renamed topic (@general subagent)"
        path.write_text(json.dumps(data))
    else:
        summary_path = path.with_name("summary.json")
        data = json.loads(summary_path.read_text())
        data["generated_title"] = "Renamed topic (@general subagent)"
        summary_path.write_text(json.dumps(data))
    result = exporter(output, state, dry_run=False, **kwargs)
    assert result["exported"] == 0
    assert not result.get("warnings")
    assert archived.is_file()
    assert state[source]["sessions"]


def test_since_date_scopes_archive_retirement(tmp_path):
    for exporter, option, writer in [(export_gemini, "gemini_dir", gemini_fixture),
                                     (export_grok, "sessions_dir", grok_fixture)]:
        root, output, state = tmp_path / f"in-{option}", tmp_path / f"out-{option}", {}
        path = writer(root)
        kwargs = {option: root, "full": False}
        assert exporter(output, state, dry_run=False, since_date=None, **kwargs)["exported"] == 1
        archived = next(output.glob("*.md"))
        if option == "gemini_dir":
            data = json.loads(path.read_text())
            data["messages"] = []
            path.write_text(json.dumps(data, indent=2))
        else:
            with path.open("a") as handle:
                handle.write(json.dumps({"method": "_x.ai/session/update", "params": {"sessionId": "grok-fixture",
                    "update": {"sessionUpdate": "rewind_marker", "target_prompt_index": 0}}}) + "\n")
        result = exporter(output, state, dry_run=False, since_date=date(2026, 6, 11), **kwargs)
        assert not result.get("warnings")
        assert archived.is_file(), "out-of-scope archive must survive"
        result = exporter(output, state, dry_run=False, since_date=None, **kwargs)
        assert any("retired stale archive" in w["error"] for w in result.get("warnings", []))
        assert not archived.is_file()


def test_gemini_checkpoint_migration_prefers_caught_up_jsonl(tmp_path):
    json_path = gemini_fixture(tmp_path, ".json")
    legacy = json.loads(json_path.read_text())
    legacy["messages"].extend([
        gemini_message("u3", "user", "Second prompt"),
        gemini_message("a3", "gemini", "Second answer", model="gemini-example-2"),
    ])
    json_path.write_text(json.dumps(legacy))
    checkpoint = legacy["messages"]
    jsonl_path = json_path.with_suffix(".jsonl")
    jsonl_path.write_text("\n".join(map(json.dumps, [
        {"sessionId": "gemini-fixture", "startTime": TIME, "lastUpdated": TIME},
        checkpoint[0],
        {"$set": {"messages": checkpoint}},
        gemini_message("u4", "user", "NEW AFTER CHECKPOINT"),
    ])) + "\n")
    output = tmp_path / "out"
    result = export_gemini(output, {}, full=False, dry_run=False, since_date=None, gemini_dir=tmp_path)
    assert result["exported"] == 1 and not result.get("migration_fallbacks")
    assert "NEW AFTER CHECKPOINT" in next(output.glob("*.md")).read_text()


def test_gemini_duplicate_id_replacement_jsonl_does_not_shadow_json(tmp_path):
    json_path = gemini_fixture(tmp_path, ".json")
    legacy = json.loads(json_path.read_text())
    legacy["messages"].extend([
        gemini_message("u3", "user", "Second prompt"),
        gemini_message("a3", "gemini", "Complete answer", model="gemini-example-2"),
    ])
    json_path.write_text(json.dumps(legacy))
    repeated = [gemini_message("u1", "user", f"replacement {i}") for i in range(5)]
    json_path.with_suffix(".jsonl").write_text("\n".join(map(json.dumps, repeated)) + "\n")
    output = tmp_path / "out"
    result = export_gemini(output, {}, full=False, dry_run=False, since_date=None, gemini_dir=tmp_path)
    assert result["exported"] == 1 and result.get("migration_fallbacks") == 1
    assert "Complete answer" in next(output.glob("*.md")).read_text()


def test_grok_hidden_prompt_advances_model_state(tmp_path):
    path = grok_fixture(tmp_path)
    events = [
        grok_event("user_message_chunk", "Explain ", index=0, model="grok-example"),
        grok_event("user_message_chunk", "synthetic foxes", index=0, model="grok-example"),
        grok_event("agent_message_chunk", "A fox can be synthetic."),
        grok_event("user_message_chunk", "And owls?", index=1, model="grok-example"),
    ]
    path.write_text("\n".join(map(json.dumps, events)) + "\n")
    hidden = grok_event("user_message_chunk", "hidden wake", index=1, model="grok-next")
    hidden["params"]["_meta"] = {"promptId": "task-completed-bg"}
    with path.open("a") as handle:
        handle.write(json.dumps(hidden) + "\n")
        handle.write(json.dumps(grok_event("agent_message_chunk", "Visible result")) + "\n")
    record = parse_grok_session_file(path)
    assert [m.model for m in record.messages] == ["grok-example", "grok-example", "grok-example", "grok-next"]
    assert "grok-next" in record.models_used
    assert "hidden wake" not in str(record)


def test_grok_rewind_is_stitching_boundary(tmp_path):
    path = grok_fixture(tmp_path)
    with path.open("a") as handle:
        handle.write(json.dumps({"method": "_x.ai/session/update", "params": {"sessionId": "grok-fixture",
            "update": {"sessionUpdate": "rewind_marker", "target_prompt_index": 1}}}) + "\n")
        handle.write(json.dumps(grok_event("agent_message_chunk", "Post-rewind agent")) + "\n")
    record = parse_grok_session_file(path)
    assert [m.content for m in record.messages] == ["Explain synthetic foxes", "A fox can be synthetic.", "Post-rewind agent"]


def test_grok_rewind_and_legacy_records(tmp_path):
    path = grok_fixture(tmp_path)
    with path.open("a") as handle:
        handle.write(json.dumps({"method": "_x.ai/session/update", "params": {"sessionId": "grok-fixture",
            "update": {"sessionUpdate": "rewind_marker", "target_prompt_index": 1}}}) + "\n")
        handle.write(json.dumps(grok_event("user_message_chunk", "New question", index=1)["params"]) + "\n")
        handle.write(json.dumps(grok_event("agent_message_chunk", "New answer")["params"]) + "\n")
        handle.write('{"torn":')
    assert [m.content for m in parse_grok_session_file(path).messages] == [
        "Explain synthetic foxes", "A fox can be synthetic.", "New question", "New answer"]


@pytest.mark.parametrize("first_index", [None, 0])
def test_grok_rewind_preserves_answer_after_unmarked_mid_turn_context(first_index, tmp_path):
    path = grok_fixture(tmp_path)
    events = [
        grok_event("user_message_chunk", "First question", index=first_index),
        grok_event("agent_message_chunk", "First answer"),
        grok_event("user_message_chunk", "Second question", index=1),
        grok_event("agent_message_chunk", "Beginning second answer"),
        grok_event("user_message_chunk", "Unmarked mid-turn context"),
        grok_event("agent_message_chunk", "Final second answer"),
        grok_event("user_message_chunk", "Abandoned question", index=2),
        grok_event("agent_message_chunk", "Abandoned answer"),
        {"method": "_x.ai/session/update", "params": {"sessionId": "grok-fixture",
            "update": {"sessionUpdate": "rewind_marker", "target_prompt_index": 2}}},
        grok_event("user_message_chunk", "Replacement question", index=2),
        grok_event("agent_message_chunk", "Replacement answer"),
    ]
    path.write_text("\n".join(map(json.dumps, events)) + "\n")
    assert [message.content for message in parse_grok_session_file(path).messages] == [
        "First question", "First answer", "Second question", "Beginning second answer",
        "Unmarked mid-turn context", "Final second answer", "Replacement question", "Replacement answer",
    ]


@pytest.mark.parametrize("field", ["info", "chunk_meta", "content", "content_meta", "notification_meta"])
def test_grok_wrong_shaped_session_is_isolated_and_retryable(field, tmp_path):
    root, output, state = tmp_path / "input", tmp_path / "output", {}
    good = grok_fixture(root)
    bad = good.parent.parent / "malformed" / "updates.jsonl"
    bad.parent.mkdir()
    summary = json.loads(good.with_name("summary.json").read_text())
    summary["info"]["id"] = "malformed-fixture"
    events = [json.loads(line) for line in good.read_text().splitlines()]
    for event in events:
        event["params"]["sessionId"] = "malformed-fixture"
    valid_summary, valid_events = copy.deepcopy(summary), copy.deepcopy(events)
    update = events[0]["params"]["update"]
    if field == "info":
        summary["info"] = ["wrong shape"]
    elif field == "chunk_meta":
        update["_meta"] = ["wrong shape"]
    elif field == "content":
        update["content"] = ["wrong shape"]
    elif field == "content_meta":
        update["content"]["_meta"] = ["wrong shape"]
    else:
        events[0]["params"]["_meta"] = ["wrong shape"]
    bad.with_name("summary.json").write_text(json.dumps(summary))
    bad.write_text("\n".join(map(json.dumps, events)) + "\n")
    kwargs = {"full": False, "since_date": None, "sessions_dir": root}
    first_dry = export_grok(output, state, dry_run=True, **kwargs)
    assert {key: first_dry[key] for key in ["source", "scanned", "exported", "failed"]} == {
        "source": "grok", "scanned": 2, "exported": 1, "failed": 1,
    }
    assert len(first_dry["warnings"]) == 1
    assert "malformed" in first_dry["warnings"][0]["path"]
    assert state == {} and not output.exists()
    assert export_grok(output, state, dry_run=False, **kwargs)["failed"] == 1
    assert len(list(output.glob("*.md"))) == 1
    assert set(state["grok"]["sessions"]) == {"grok-fixture"}
    bad.with_name("summary.json").write_text(json.dumps(valid_summary))
    bad.write_text("\n".join(map(json.dumps, valid_events)) + "\n")
    assert export_grok(output, state, dry_run=False, **kwargs) == {
        "source": "grok", "scanned": 2, "exported": 1,
    }
    assert len(list(output.glob("*.md"))) == 2


def test_grok_timestamped_log_dates_legacy_summary(tmp_path):
    path = grok_fixture(tmp_path)
    summary = json.loads(path.with_name("summary.json").read_text())
    del summary["created_at"]
    path.with_name("summary.json").write_text(json.dumps(summary))
    record = parse_grok_session_file(path)
    assert record.date == "2026-06-10"


def test_grok_display_content_and_hidden_host_messages(tmp_path):
    path = grok_fixture(tmp_path)
    display = grok_event("user_message_chunk", "injected context", index=2)
    display["params"]["update"]["content"]["_meta"] = {"displayText": "Human wording"}
    hidden = grok_event("user_message_chunk", "hidden-secret")
    hidden["params"]["update"]["_meta"] = {"hideFromScrollback": True, "hostTurn": True}
    shell = grok_event("user_message_chunk", "shell-secret")
    shell["params"]["update"]["content"]["_meta"] = {"bash_command": "echo example"}
    with path.open("a") as handle:
        for event in [display, hidden, shell]:
            handle.write(json.dumps(event) + "\n")
    record = parse_grok_session_file(path)
    assert record.messages[-1].content == "Human wording"
    assert "secret" not in str(record)


@pytest.mark.parametrize("kind", ["subagent", "subagent_fork"])
def test_grok_skips_subagents(kind, tmp_path):
    path = grok_fixture(tmp_path)
    summary = json.loads(path.with_name("summary.json").read_text())
    summary["session_kind"] = kind
    path.with_name("summary.json").write_text(json.dumps(summary))
    assert parse_grok_session_file(path) is None


@pytest.mark.parametrize("source,writer,exporter,option", [
    ("gemini", gemini_fixture, export_gemini, "gemini_dir"),
    ("grok", grok_fixture, export_grok, "sessions_dir"),
])
def test_adapter_incremental_growth_rename_dry_run_and_failure(source, writer, exporter, option, tmp_path):
    root, output, state = tmp_path / "input", tmp_path / "output", {}
    path = writer(root)
    kwargs = {option: root, "full": False, "since_date": None}
    assert exporter(output, state, dry_run=True, **kwargs)["exported"] == 1
    assert state == {} and not output.exists()
    assert exporter(output, state, dry_run=False, **kwargs)["exported"] == 1
    first_path = next(output.glob("*.md"))
    assert exporter(output, state, dry_run=False, **kwargs)["exported"] == 0
    if source == "gemini":
        data = json.loads(path.read_text())
        data["summary"] = "Renamed session"
        data["messages"].append(gemini_message("u3", "user", "New follow-up"))
        path.write_text(json.dumps(data))
        bad = path.with_name("session-malformed.json")
    else:
        summary_path = path.with_name("summary.json")
        data = json.loads(summary_path.read_text())
        data["generated_title"] = "Renamed session"
        summary_path.write_text(json.dumps(data))
        with path.open("a") as handle:
            handle.write(json.dumps(grok_event("user_message_chunk", "New follow-up", index=2)) + "\n")
        bad = path.parent.parent / "malformed" / "updates.jsonl"
        bad.parent.mkdir()
    bad.write_text("broken JSON")
    snapshot = copy.deepcopy(state)
    before = first_path.read_bytes()
    assert exporter(output, state, dry_run=True, **kwargs)["failed"] == 1
    assert state == snapshot and first_path.read_bytes() == before
    result = exporter(output, state, dry_run=False, **kwargs)
    assert result["exported"] == 1 and result["failed"] == 1
    assert list(output.glob("*.md")) == [first_path]
    assert "New follow-up" in first_path.read_text() and "Renamed session" in first_path.read_text()
    assert exporter(output, state, dry_run=False, **kwargs)["exported"] == 0
    assert exporter(output, {}, dry_run=True, **{**kwargs, "since_date": date(2026, 6, 11)})["exported"] == 0


@pytest.mark.parametrize("source,writer,flag", [
    ("gemini", gemini_fixture, "--gemini-dir"), ("grok", grok_fixture, "--grok-sessions-dir"),
])
def test_cli_source_registration_and_state_roundtrip(source, writer, flag, tmp_path):
    writer(tmp_path / "input")
    command = [sys.executable, "export_sessions.py", "--source", source, flag, str(tmp_path / "input"),
               "--base-dir", str(tmp_path / "archive"), "--state-file", str(tmp_path / "state.json")]
    repo = Path(__file__).resolve().parents[1]
    first = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert "exported=1 scanned=1" in first.stdout
    assert source in json.loads((tmp_path / "state.json").read_text())
    second = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    assert second.returncode == 0 and "exported=0 scanned=1" in second.stdout
