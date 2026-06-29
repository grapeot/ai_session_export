from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any

from .sources import export_antigravity, export_claude_code, export_opencode, export_second_mind
from .sources.antigravity import DEFAULT_ANTIGRAVITY_BRAIN_DIR
from .sources.claude_code import DEFAULT_CLAUDE_HISTORY_FILES, DEFAULT_CLAUDE_PROJECT_DIRS
from .state import load_state, save_state
from .utils import date_from_cli


BASE_DIR = Path(__file__).resolve().parents[2]
SECOND_MIND_JSON = BASE_DIR / "second_mind_export.json"
STATE_FILE = BASE_DIR / ".export_state.json"
DEFAULT_OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

SOURCE_CHOICES = ["all", "second-mind", "opencode", "claude-code", "antigravity"]


def run_export(
    source: str,
    *,
    full: bool,
    dry_run: bool,
    base_dir: Path = BASE_DIR,
    state_file: Path = STATE_FILE,
    second_mind_json: Path = SECOND_MIND_JSON,
    opencode_db: Path = DEFAULT_OPENCODE_DB,
    antigravity_brain_dir: Path = DEFAULT_ANTIGRAVITY_BRAIN_DIR,
    since_date: date | None = None,
    claude_project_dirs: tuple[Path, ...] | None = None,
    claude_history_files: tuple[Path, ...] | None = None,
) -> list[dict[str, Any]]:
    state = load_state(state_file)
    results: list[dict[str, Any]] = []

    if source in {"second-mind", "all"}:
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
            )
        )
    if source in {"antigravity", "all"}:
        results.append(
            export_antigravity(
                base_dir / "antigravity",
                state,
                brain_dir=antigravity_brain_dir,
                full=full,
                dry_run=dry_run,
                since_date=since_date,
            )
        )

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
        "--antigravity-dir",
        type=Path,
        default=DEFAULT_ANTIGRAVITY_BRAIN_DIR,
        help="Override Antigravity IDE brain directory.",
    )
    parser.add_argument("--since-date", type=date_from_cli, help="Only export sessions on or after YYYY-MM-DD.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    results = run_export(
        args.source,
        full=args.full,
        dry_run=args.dry_run,
        base_dir=args.base_dir,
        state_file=args.state_file,
        second_mind_json=args.second_mind_json,
        opencode_db=args.opencode_db,
        antigravity_brain_dir=args.antigravity_dir,
        since_date=args.since_date,
    )
    for result in results:
        source = result["source"]
        suffix = " (dry-run)" if args.dry_run else ""
        if source == "second_mind":
            print(f"[second_mind] exported={result['exported']} total={result['total']}{suffix}")
        else:
            print(f"[{source}] exported={result['exported']} scanned={result['scanned']}{suffix}")
