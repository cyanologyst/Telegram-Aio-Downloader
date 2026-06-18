"""Discovery helpers for external downloader runtimes."""

import os
import shutil
import subprocess
from pathlib import Path


def configure_deno_runtime(configured_path: str | None = None) -> Path | None:
    """Find Deno, add its directory to PATH, and return the executable path."""
    executable_name = "deno.exe" if os.name == "nt" else "deno"
    candidates: list[Path] = []

    if configured_path:
        configured = Path(configured_path).expanduser()
        candidates.append(configured / executable_name if configured.is_dir() else configured)

    discovered = shutil.which("deno")
    if discovered:
        candidates.append(Path(discovered))

    home = Path.home()
    candidates.extend(
        [
            home / ".deno" / "bin" / executable_name,
            home / ".local" / "bin" / executable_name,
        ]
    )

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue

        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        parent = str(resolved.parent)
        if parent not in path_entries:
            os.environ["PATH"] = f"{parent}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["DENO_BIN"] = str(resolved)
        return resolved

    return None


def get_deno_version(deno_path: Path | None) -> str | None:
    """Return the installed Deno version, or None when it cannot run."""
    if deno_path is None:
        return None
    try:
        result = subprocess.run(
            [str(deno_path), "--version"],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    first_line = result.stdout.splitlines()[0].strip() if result.stdout else ""
    return first_line or None
