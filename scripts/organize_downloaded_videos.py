#!/usr/bin/env python3
from pathlib import Path
import shutil

# Set this to your parent folder path
PARENT = Path("./Download")  # run script inside parent folder, or change path

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}


def unique_target(dst: Path) -> Path:
    if not dst.exists():
        return dst

    stem, suf = dst.stem, dst.suffix
    i = 1

    while True:
        cand = dst.with_name(f"{stem}_{i}{suf}")
        if not cand.exists():
            return cand
        i += 1


# Keep track of folders that had videos moved out
folders_to_delete = set()

for p in PARENT.rglob("*"):
    if p.is_file() and p.suffix.lower() in VIDEO_EXTS and p.parent != PARENT:
        source_folder = p.parent

        target = unique_target(PARENT / p.name)
        print(f"Moving: {p} -> {target}")

        shutil.move(str(p), str(target))
        folders_to_delete.add(source_folder)

# Delete folders (with all remaining contents) after videos are moved
for folder in sorted(folders_to_delete, key=lambda x: len(x.parts), reverse=True):
    if folder.exists():
        print(f"Deleting folder and contents: {folder}")
        shutil.rmtree(folder, ignore_errors=True)

print("Done.")
