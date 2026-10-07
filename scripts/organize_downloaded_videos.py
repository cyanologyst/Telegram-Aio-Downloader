#!/usr/bin/env python3
"""Lift videos out of sub-folders, using the same safe logic as the bot.

Dry run by default: prints what would happen. Pass --apply to move files.
Folders with downloads in progress are skipped, and left-over files are only
deleted with --delete-leftovers.

    python scripts/organize_downloaded_videos.py [FOLDER] [--apply] [--delete-leftovers]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.organize import apply_organize, plan_organize  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", nargs="?", default="Download", type=Path)
    parser.add_argument("--apply", action="store_true", help="actually move files")
    parser.add_argument(
        "--delete-leftovers",
        action="store_true",
        help="delete sub-folders including files that are not videos",
    )
    args = parser.parse_args()

    if not args.folder.is_dir():
        parser.error(f"not a folder: {args.folder}")

    plan = plan_organize(args.folder)
    for src, dst in plan.moves:
        print(f"{'Moving' if args.apply else 'Would move'}: {src} -> {dst}")
    for name in plan.skipped_busy:
        print(f"Skipped (download in progress): {name}")
    if plan.leftover_files:
        action = "deleted" if args.delete_leftovers else "kept"
        print(f"Other files in those folders: {plan.leftover_files} ({action})")

    if not args.apply:
        print("Dry run only. Re-run with --apply to make these changes.")
        return 0

    result = apply_organize(plan, delete_leftovers=args.delete_leftovers)
    print(
        f"Moved {result.moved} video(s); removed {result.removed_folders} folder(s); "
        f"kept {result.kept_folders}."
    )
    for error in result.errors:
        print(f"Error: {error}", file=sys.stderr)
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
