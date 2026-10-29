from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from .sources import (
    export_antigravity,
    export_claude_code,
    export_codex,
    export_cursor,
    export_cursor_cli,
    export_dsh,
    export_opencode,
    export_second_mind,
    list_antigravity_sessions,
    parse_antigravity_session,
    set_antigravity_archived,
)
from .sources.antigravity import DEFAULT_ANTIGRAVITY_BRAIN_DIRS, MalformedTranscriptError
from .sources.claude_code import DEFAULT_CLAUDE_HISTORY_FILES, DEFAULT_CLAUDE_PROJECT_DIRS
from .sources.codex import DEFAULT_CODEX_SESSION_DIRS, DEFAULT_CODEX_SESSION_INDEX
from .sources.cursor import DEFAULT_CURSOR_DB
from .sources.cursor_cli import DEFAULT_CURSOR_CLI_DIR
from .sources.dsh import DEFAULT_DSH_SESSIONS_DIR
from .sources.gemini import DEFAULT_GEMINI_DIR, export_gemini
from .sources.grok import DEFAULT_GROK_SESSIONS_DIR, export_grok
from .state import load_state, save_state
from .utils import date_from_cli


BASE_DIR = Path.home() / ".local" / "share" / "ai-session-export"
SECOND_MIND_JSON = BASE_DIR / "second_mind_export.json"
STATE_FILE = BASE_DIR / ".export_state.json"
DEFAULT_OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

SOURCE_CHOICES = ["all", "second-mind", "opencode", "claude-code", "antigravity", "codex", "cursor", "cursor-cli", "dsh", "gemini", "grok"]
ANTIGRAVITY_SURFACE_CHOICES = ["all", "2", "ide", "cli"]
ANTIGRAVITY_STATUS_CHOICES = ["all", "active", "archived"]


def run_export(
    source: str,
    *,
    full: bool,
    dry_run: bool,
    base_dir: Path = BASE_DIR,
    state_file: Path = STATE_FILE,
    second_mind_json: Path = SECOND_MIND_JSON,
    opencode_db: Path = DEFAULT_OPENCODE_DB,
    antigravity_brain_dir: Path | None = None,
    antigravity_brain_dirs: Mapping[str, Path] | None = None,
    since_date: date | None = None,
    claude_project_dirs: tuple[Path, ...] | None = None,
    claude_history_files: tuple[Path, ...] | None = None,
    claude_backfill_dir: Path | None = None,
    codex_session_dirs: tuple[Path, ...] | None = None,
    codex_session_index: Path = DEFAULT_CODEX_SESSION_INDEX,
    cursor_db: Path = DEFAULT_CURSOR_DB,
    cursor_cli_dir: Path = DEFAULT_CURSOR_CLI_DIR,
    dsh_sessions_dir: Path = DEFAULT_DSH_SESSIONS_DIR,
    gemini_dir: Path = DEFAULT_GEMINI_DIR,
    grok_sessions_dir: Path = DEFAULT_GROK_SESSIONS_DIR,
) -> list[dict[str, Any]]:
    state = load_state(state_file)
    results: list[dict[str, Any]] = []

    if source == "second-mind":
        results.append(
            export_second_mind(
                base_dir / "second_mind",
                state,
                source_json=second_mind_json,
                full=full,
                dry_run=dry_run,
                since_date=since_date,
            )
        )
    if source in {"opencode", "all"}:
        results.append(
            export_opencode(
                base_dir / "opencode",
                state,
                db_path=opencode_db,
                full=full,
                dry_run=dry_run,
                since_date=since_date,
            )
        )
    if source in {"claude-code", "all"}:
        results.append(
            export_claude_code(
                base_dir / "claude_code",
                state,
                full=full,
                dry_run=dry_run,
                since_date=since_date,
                project_dirs=claude_project_dirs or DEFAULT_CLAUDE_PROJECT_DIRS,
                history_files=claude_history_files or DEFAULT_CLAUDE_HISTORY_FILES,
                backfill_dir=claude_backfill_dir or base_dir / "claude_code_backfill",
            )
        )
    if source in {"antigravity", "all"}:
        results.append(
            export_antigravity(
                base_dir / "antigravity",
                state,
                brain_dir=antigravity_brain_dir,
                brain_dirs=antigravity_brain_dirs,
                full=full,
                dry_run=dry_run,
                since_date=since_date,
            )
        )
    if source in {"codex", "all"}:
        results.append(
            export_codex(
                base_dir / "codex",
                state,
                full=full,
                dry_run=dry_run,
                since_date=since_date,
                session_dirs=codex_session_dirs or DEFAULT_CODEX_SESSION_DIRS,
                session_index=codex_session_index,
            )
        )
    if source in {"cursor", "all"}:
        results.append(
            export_cursor(
                base_dir / "cursor",
                state,
                db_path=cursor_db,
                full=full,
                dry_run=dry_run,
                since_date=since_date,
            )
        )
    if source in {"cursor-cli", "all"}:
        results.append(
            export_cursor_cli(
                base_dir / "cursor_cli",
                state,
                chats_dir=cursor_cli_dir,
                full=full,
                dry_run=dry_run,
                since_date=since_date,
            )
        )
    if source in {"dsh", "all"}:
        results.append(
            export_dsh(
                base_dir / "dsh",
                state,
                full=full,
                dry_run=dry_run,
                since_date=since_date,
                sessions_dir=dsh_sessions_dir,
            )
        )

    if source in {"gemini", "all"}:
        results.append(export_gemini(base_dir / "gemini", state, full=full, dry_run=dry_run,
                                     since_date=since_date, gemini_dir=gemini_dir))
    if source in {"grok", "all"}:
        results.append(export_grok(base_dir / "grok", state, full=full, dry_run=dry_run,
                                   since_date=since_date, sessions_dir=grok_sessions_dir))

    if not dry_run:
        save_state(state, state_file)
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export AI sessions to markdown files.")
    parser.add_argument("--source", choices=SOURCE_CHOICES, default="all")
    parser.add_argument("--full", action="store_true", help="Ignore state and export everything.")
    parser.add_argument("--dry-run", action="store_true", help="Show counts without writing files.")
    parser.add_argument("--base-dir", type=Path, default=BASE_DIR, help="Override export output root for testing.")
    parser.add_argument("--state-file", type=Path, default=STATE_FILE, help="Override state file path for testing.")
    parser.add_argument("--second-mind-json", type=Path, default=SECOND_MIND_JSON, help="Override second mind JSON path.")
    parser.add_argument("--opencode-db", type=Path, default=DEFAULT_OPENCODE_DB, help="Override OpenCode database path.")
    parser.add_argument(
        "--claude-backfill-dir",
        type=Path,
        help=(
            "Directory of <claude-session-id>.jsonl files holding messages recovered from the cloud "
            "transcript of a truncated Claude Code session (default: <base-dir>/claude_code_backfill/)."
        ),
    )
    parser.add_argument(
        "--antigravity-dir",
        type=Path,
        help="Override all default Antigravity surfaces with one IDE-compatible brain directory.",
    )
    parser.add_argument(
        "--antigravity-surface",
        choices=ANTIGRAVITY_SURFACE_CHOICES,
        default="all",
        help="Restrict Antigravity inspection or archive operations to a specific surface.",
    )
    parser.add_argument(
        "--antigravity-list",
        action="store_true",
        help="List Antigravity sessions with native metadata and archived state.",
    )
    parser.add_argument(
        "--antigravity-status",
        choices=ANTIGRAVITY_STATUS_CHOICES,
        default="all",
        help="Filter --antigravity-list by archived state (all, active, archived).",
    )
    parser.add_argument(
        "--antigravity-query",
        type=str,
        help="Substring filter on session_id, title, or project_directory for --antigravity-list.",
    )
    parser.add_argument(
        "--include-subagents",
        action="store_true",
        help="Include child subagent sessions in --antigravity-list.",
    )
    parser.add_argument(
        "--antigravity-parse",
        type=str,
        metavar="SESSION_ID",
        help="Parse and display a single Antigravity session (active or archived) by session ID.",
    )
    parser.add_argument(
        "--include-thinking",
        action="store_true",
        help="Include model thinking steps when running --antigravity-parse --json.",
    )
    parser.add_argument(
        "--include-tool-calls",
        action="store_true",
        help="Include model tool_calls when running --antigravity-parse --json.",
    )
    parser.add_argument(
        "--antigravity-archive",
        nargs="+",
        metavar="SESSION_ID",
        help="Mark one or more Antigravity sessions as archived in native storage.",
    )
    parser.add_argument(
        "--antigravity-unarchive",
        nargs="+",
        metavar="SESSION_ID",
        help="Mark one or more Antigravity sessions as unarchived (active) in native storage.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output for Antigravity list/parse/archive/unarchive operations.",
    )
    parser.add_argument(
        "--codex-dir",
        type=Path,
        action="append",
        help="Override a Codex session directory; repeat for multiple roots.",
    )
    parser.add_argument(
        "--codex-session-index",
        type=Path,
        default=DEFAULT_CODEX_SESSION_INDEX,
        help="Override the Codex session_index.jsonl path.",
    )
    parser.add_argument(
        "--cursor-db",
        type=Path,
        default=DEFAULT_CURSOR_DB,
        help="Override the Cursor state.vscdb path.",
    )
    parser.add_argument(
        "--cursor-cli-dir",
        type=Path,
        default=DEFAULT_CURSOR_CLI_DIR,
        help="Override the Cursor CLI chats root (~/.cursor/chats).",
    )
    parser.add_argument(
        "--dsh-sessions-dir",
        type=Path,
        default=DEFAULT_DSH_SESSIONS_DIR,
        help="Override the DeepSeek Harness sessions root (~/.dsh/sessions).",
    )
    parser.add_argument("--gemini-dir", type=Path, default=DEFAULT_GEMINI_DIR,
                        help="Override the Gemini CLI temporary project root.")
    parser.add_argument("--grok-sessions-dir", type=Path, default=DEFAULT_GROK_SESSIONS_DIR,
                        help="Override the Grok Build sessions root.")
    parser.add_argument("--since-date", type=date_from_cli, help="Only export sessions on or after YYYY-MM-DD.")
    return parser.parse_args()


def _resolve_cli_antigravity_roots(
    args: argparse.Namespace,
) -> tuple[Path | None, Mapping[str, Path] | None]:
    if args.antigravity_dir is not None:
        return args.antigravity_dir, None
    surface = getattr(args, "antigravity_surface", "all")
    if surface and surface != "all":
        return None, {surface: DEFAULT_ANTIGRAVITY_BRAIN_DIRS[surface]}
    return None, None


def _handle_antigravity_action(args: argparse.Namespace) -> int | None:
    list_requested = bool(getattr(args, "antigravity_list", False))
    parse_id = getattr(args, "antigravity_parse", None)
    archive_ids = getattr(args, "antigravity_archive", None)
    unarchive_ids = getattr(args, "antigravity_unarchive", None)

    requested_actions = sum(
        1 for flag in (list_requested, bool(parse_id), bool(archive_ids), bool(unarchive_ids)) if flag
    )
    if requested_actions == 0:
        return None
    if requested_actions > 1:
        print(
            "[antigravity] --antigravity-list, --antigravity-parse, --antigravity-archive, "
            "and --antigravity-unarchive are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    brain_dir, brain_dirs = _resolve_cli_antigravity_roots(args)
    if list_requested:
        sessions = list_antigravity_sessions(
            brain_dir=brain_dir,
            brain_dirs=brain_dirs,
            status=getattr(args, "antigravity_status", "all"),
            query=getattr(args, "antigravity_query", None),
            include_subagents=getattr(args, "include_subagents", False),
        )
        if getattr(args, "json", False):
            print(json.dumps({"sessions": sessions, "count": len(sessions)}, ensure_ascii=False, indent=2))
        else:
            for item in sessions:
                state_tag = "archived" if item["archived"] else "active"
                title = item["title"] or "(untitled)"
                print(f"{item['session_id']}\t{item['surface']}\t{state_tag}\t{title}")
        return 0

    if parse_id:
        try:
            parsed = parse_antigravity_session(
                parse_id,
                brain_dir=brain_dir,
                brain_dirs=brain_dirs,
                include_thinking=getattr(args, "include_thinking", False),
                include_tool_calls=getattr(args, "include_tool_calls", False),
            )
        except (FileNotFoundError, MalformedTranscriptError, ValueError) as exc:
            print(f"[antigravity] {exc}", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            print(json.dumps(parsed, ensure_ascii=False, indent=2))
        else:
            print(parsed["markdown"], end="")
        return 0

    target_ids = archive_ids if archive_ids else unarchive_ids
    target_archived = bool(archive_ids)
    outcomes = set_antigravity_archived(
        target_ids,
        archived=target_archived,
        brain_dir=brain_dir,
        brain_dirs=brain_dirs,
        dry_run=args.dry_run,
    )
    if getattr(args, "json", False):
        print(json.dumps({"results": outcomes}, ensure_ascii=False, indent=2))
    else:
        for item in outcomes:
            print(
                f"[antigravity] {item['session_id']}: status={item['status']} archived={item['archived']}"
            )
    failed = any(item["status"] not in {"verified", "dry_run"} for item in outcomes)
    return 1 if failed else 0


def main() -> None:
    args = parse_args()
    action_code = _handle_antigravity_action(args)
    if action_code is not None:
        if action_code != 0:
            raise SystemExit(action_code)
        return

    results = run_export(
        args.source,
        full=args.full,
        dry_run=args.dry_run,
        base_dir=args.base_dir,
        state_file=args.state_file,
        second_mind_json=args.second_mind_json,
        opencode_db=args.opencode_db,
        antigravity_brain_dir=args.antigravity_dir,
        claude_backfill_dir=getattr(args, "claude_backfill_dir", None),
        codex_session_dirs=tuple(args.codex_dir) if args.codex_dir else None,
        codex_session_index=args.codex_session_index,
        cursor_db=args.cursor_db,
        cursor_cli_dir=args.cursor_cli_dir,
        dsh_sessions_dir=args.dsh_sessions_dir,
        gemini_dir=args.gemini_dir,
        grok_sessions_dir=args.grok_sessions_dir,
        since_date=args.since_date,
    )
    for result in results:
        source = result["source"]
        suffix = " (dry-run)" if args.dry_run else ""
        if source == "second_mind":
            print(f"[second_mind] exported={result['exported']} total={result['total']}{suffix}")
        else:
            failed = int(result.get("failed", 0))
            failure_summary = f" failed={failed}" if failed else ""
            extra = ""
            if source == "opencode":
                overwritten = int(result.get("overwritten", 0))
                skipped_shrunk = int(result.get("skipped_shrunk", 0))
                if overwritten:
                    extra += f" overwritten={overwritten}"
                if skipped_shrunk:
                    extra += f" skipped_shrunk={skipped_shrunk}"
            print(f"[{source}] exported={result['exported']} scanned={result['scanned']}{failure_summary}{extra}{suffix}")
            for warning in result.get("warnings", []):
                if "surface" in warning:
                    print(
                        f"[{source}:{warning['surface']}] line {warning['line']}: {warning['error']}",
                        file=sys.stderr,
                    )
                else:
                    detail = warning.get("session_id", warning.get("path", ""))
                    print(f"[{source}] {detail}: {warning['error']}", file=sys.stderr)
    if any(int(result.get("failed", 0)) for result in results):
        raise SystemExit(1)
