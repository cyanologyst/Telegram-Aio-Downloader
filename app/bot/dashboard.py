import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class DashboardHandler(BaseHTTPRequestHandler):
    download_jobs: dict[Any, Any] = {}
    upload_jobs: dict[Any, Any] = {}

    def log_message(self, format: str, *args: object) -> None:
        # Silence default logging; the main bot logger handles runtime diagnostics.
        return

    def do_GET(self) -> None:
        if self.path.startswith("/api/jobs"):
            self._send_json(self._jobs_payload())
            return

        if self.path in {"/", "/index.html"}:
            self._send_html(self._render_dashboard())
            return

        self.send_error(404, "Not Found")

    def _send_json(self, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_html(self, html_body: str) -> None:
        encoded = html_body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _jobs_payload(self) -> dict[str, object]:
        return {
            "download_jobs": [self._serialize_job(j) for j in self.download_jobs.values()],
            "upload_jobs": [self._serialize_job(j) for j in self.upload_jobs.values()],
            "summary": {
                "downloads": len(self.download_jobs),
                "uploads": len(self.upload_jobs),
            },
        }

    def _serialize_job(self, job: dict[str, object]) -> dict[str, object]:
        return {
            "id": job.get("id"),
            "name": job.get("name"),
            "status": job.get("status"),
            "progress": job.get("progress"),
            "completed_length": job.get("completed_length"),
            "total_length": job.get("total_length"),
            "download_speed": job.get("download_speed"),
            "uploaded": job.get("uploaded"),
            "current_file": job.get("current_file"),
            "sent_count": job.get("sent_count"),
        }

    def _render_dashboard(self) -> str:
        title = "Telegram Downloader Bot Dashboard"
        body = [
            f"<h1>{html.escape(title)}</h1>",
            "<p>Browse active download and upload jobs. The dashboard refreshes automatically every 15 seconds.</p>",
            '<p><a href="/api/jobs">JSON status endpoint</a></p>',
            "<h2>Download Jobs</h2>",
            self._render_jobs_table(
                self.download_jobs.values(),
                [
                    "id",
                    "name",
                    "status",
                    "progress",
                    "completed_length",
                    "total_length",
                    "download_speed",
                ],
            ),
            "<h2>Upload Jobs</h2>",
            self._render_jobs_table(
                self.upload_jobs.values(), ["id", "name", "status", "current_file", "sent_count"]
            ),
            "<footer><small>Hosted by the local telegram downloader bot.</small></footer>",
        ]
        return self._wrap_html(title, "\n".join(body))

    def _render_jobs_table(self, jobs: list[dict[str, object]], columns: list[str]) -> str:
        if not jobs:
            return "<p>No jobs available.</p>"

        headers = "".join(f"<th>{html.escape(col)}</th>" for col in columns)
        rows = []
        for job in sorted(jobs, key=lambda item: item.get("id", 0)):
            cells = []
            for col in columns:
                value = job.get(col, "-")
                cells.append(f"<td>{html.escape(str(value))}</td>")
            rows.append(f"<tr>{''.join(cells)}</tr>")

        return f"<table>{headers}{''.join(rows)}</table>"

    def _wrap_html(self, title: str, body: str) -> str:
        return f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta http-equiv="refresh" content="15" />
    <title>{html.escape(title)}</title>
    <style>
      body {{ font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.5; color: #222; }}
      table {{ border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; }}
      th, td {{ border: 1px solid #ccc; padding: 0.5rem; text-align: left; }}
      th {{ background: #f0f0f0; }}
      tr:nth-child(even) {{ background: #fafafa; }}
      h1, h2 {{ margin-top: 1.5rem; }}
      footer {{ margin-top: 2rem; color: #666; font-size: 0.9rem; }}
      a {{ color: #0077cc; }}
    </style>
  </head>
  <body>
    {body}
  </body>
</html>
"""


def start_dashboard_server(
    host: str,
    port: int,
    download_jobs: dict[Any, Any],
    upload_jobs: dict[Any, Any],
) -> ThreadingHTTPServer:
    handler_cls = type(
        "DashboardHandlerWithState",
        (DashboardHandler,),
        {"download_jobs": download_jobs, "upload_jobs": upload_jobs},
    )

    server = ThreadingHTTPServer((host, port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="dashboard-server")
    thread.start()
    return server


def stop_dashboard_server(server: ThreadingHTTPServer | None) -> None:
    if not server:
        return
    server.shutdown()
    server.server_close()
