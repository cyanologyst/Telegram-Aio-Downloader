#!/usr/bin/env python3
"""Start the bot with a free public HTTPS URL for the Telegram Mini App.

Telegram only opens native Mini App buttons over public HTTPS. This script:

1. Starts a Cloudflare Quick Tunnel (`cloudflared`) pointing at the local
   Flask server - no account, no domain, no SSL certificates needed.
2. Waits for the temporary ``https://<something>.trycloudflare.com`` URL.
3. Exports it as ``WEB_APP_URL`` (and saves it to ``.env`` for convenience).
4. Starts the bot.

Requirements:
- ``cloudflared`` must be installed and on PATH (or set CLOUDFLARED_BIN,
  or drop the binary into ``scripts/bin/cloudflared[.exe]``).
  https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

Usage:
    python scripts/start_with_tunnel.py

The Quick Tunnel URL changes every restart; send /start in Telegram afterwards
so the Mini App button picks up the fresh URL.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
TUNNEL_URL_PATTERN = re.compile(r"https://[a-zA-Z0-9.-]+\.trycloudflare\.com")

sys.path.insert(0, str(PROJECT_ROOT))


def load_env() -> dict:
    """Load .env values without clobbering real environment variables."""
    values = {}
    if ENV_FILE.exists():
        for raw_line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def save_env_value(key: str, value: str) -> None:
    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    for idx, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[idx] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def find_cloudflared() -> str:
    override = os.environ.get("CLOUDFLARED_BIN")
    if override and Path(override).exists():
        return override

    local = Path(__file__).parent / "bin" / (
        "cloudflared.exe" if os.name == "nt" else "cloudflared"
    )
    if local.exists():
        return str(local)

    found = shutil.which("cloudflared")
    if found:
        return found

    print(
        "ERROR: cloudflared not found.\n"
        "Install it from https://developers.cloudflare.com/cloudflare-one/"
        "connections/connect-networks/downloads/\n"
        "then make sure it is on PATH, or set CLOUDFLARED_BIN=/path/to/cloudflared."
    )
    sys.exit(1)


def wait_for_tunnel_url(process: subprocess.Popen, log_path: Path, timeout_s: int = 60) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if process.poll() is not None:
            print("ERROR: cloudflared exited early. Log tail:")
            print(log_path.read_text(encoding="utf-8", errors="replace")[-2000:])
            sys.exit(1)
        time.sleep(1)
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8", errors="replace")
            match = TUNNEL_URL_PATTERN.search(content)
            if match:
                return match.group(0)
    print(f"ERROR: no tunnel URL within {timeout_s}s. Log tail:")
    print(log_path.read_text(encoding="utf-8", errors="replace")[-2000:])
    sys.exit(1)


def main() -> None:
    env = load_env()
    host = os.environ.get("WEB_APP_HOST") or env.get("WEB_APP_HOST", "127.0.0.1")
    port = os.environ.get("WEB_APP_PORT") or env.get("WEB_APP_PORT", "5000")

    # 0.0.0.0 is a bind address, not a connect address.
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host

    # telegram_bot.main() serves Flask over TLS whenever cert.pem/key.pem sit
    # in the project root, so the tunnel has to speak the same scheme or every
    # request comes back 502.
    use_tls = (PROJECT_ROOT / "cert.pem").exists() and (PROJECT_ROOT / "key.pem").exists()
    scheme = "https" if use_tls else "http"
    target = f"{scheme}://{connect_host}:{port}"

    cloudflared = find_cloudflared()
    log_path = PROJECT_ROOT / ".cloudflared.log"

    print(f"Starting Cloudflare Quick Tunnel -> {target}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    tunnel_args = [
        cloudflared,
        "tunnel",
        "--url",
        target,
        "--no-autoupdate",
    ]
    if use_tls:
        # The local cert is self-signed; cloudflared must not reject it.
        tunnel_args.append("--no-tls-verify")

    process = subprocess.Popen(
        tunnel_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    # Stream stderr into the log file in a background thread so we can poll it.
    import threading

    def pump(stream, path: Path) -> None:
        with open(path, "a", encoding="utf-8", errors="replace") as handle:
            for line in stream:
                handle.write(line)

    t = threading.Thread(target=pump, args=(process.stderr, log_path), daemon=True)
    t.start()

    def shutdown(signum, frame):
        process.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    tunnel_url = wait_for_tunnel_url(process, log_path)
    print(f"Tunnel URL: {tunnel_url}")

    os.environ["WEB_APP_ENABLE"] = "true"
    os.environ["WEB_APP_HOST"] = host
    os.environ["WEB_APP_PORT"] = port
    os.environ["WEB_APP_URL"] = tunnel_url
    save_env_value("WEB_APP_ENABLE", "true")
    save_env_value("WEB_APP_URL", tunnel_url)

    print("Starting bot. Send /start in Telegram to refresh the Mini App button.")
    try:
        from app.bot.telegram_bot import main as bot_main

        bot_main()
    finally:
        process.terminate()


if __name__ == "__main__":
    main()
