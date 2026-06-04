"""Flask web application for Telegram mini-app file browser."""

import hashlib
import hmac
import json
import shutil
import time
import uuid
import zipfile
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from flask import Flask, jsonify, render_template, request, send_file
from flask_cors import CORS

from app.services.user_settings import (
    DEFAULT_SETTINGS,
    get_user_settings,
    save_user_settings,
    validate_compression_level,
    validate_part_size,
)


def create_web_app(
    download_dir: str,
    bot_token: str,
    *,
    download_jobs: dict[Any, Any] | None = None,
    zip_jobs: dict[Any, Any] | None = None,
    bot_loop: Any = None,
    bot_app: Any = None,
    start_download: Callable[..., Any] | None = None,
    pause_download: Callable[..., Any] | None = None,
    resume_download: Callable[..., Any] | None = None,
    cancel_download: Callable[..., Any] | None = None,
    upload_selected: Callable[..., Any] | None = None,
    zip_selected: Callable[..., Any] | None = None,
    default_chat_id: int | None = None,
) -> Flask:
    """
    Create and configure Flask app for Telegram mini-app.

    Args:
        download_dir: Directory containing downloaded files
        bot_token: Telegram bot token for validating initData
    """
    app = Flask(__name__, template_folder="templates", static_folder="static")
    CORS(app)
    app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100MB max upload

    # Store config
    app.download_dir = Path(download_dir).resolve()
    app.bot_token = bot_token
    app.download_jobs = download_jobs if download_jobs is not None else {}
    app.zip_jobs = zip_jobs if zip_jobs is not None else {}
    app.bot_loop = bot_loop
    app.bot_app = bot_app
    app.start_download = start_download
    app.pause_download = pause_download
    app.resume_download = resume_download
    app.cancel_download = cancel_download
    app.upload_selected = upload_selected
    app.zip_selected = zip_selected
    app.default_chat_id = default_chat_id

    # Ensure download directory exists
    app.download_dir.mkdir(parents=True, exist_ok=True)

    def validate_telegram_init_data(init_data: str) -> bool:
        """Validate Telegram Web App initData signature."""
        if not init_data:
            return False

        try:
            # Parse init data
            params = dict(param.split("=", 1) for param in init_data.split("&"))

            if "hash" not in params:
                return False

            hash_value = params.pop("hash")

            # Sort and format data to check
            data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))

            # Create HMAC
            secret_key = hmac.new(b"WebAppData", app.bot_token.encode(), hashlib.sha256).digest()

            calculated_hash = hmac.new(
                secret_key, data_check_string.encode(), hashlib.sha256
            ).hexdigest()

            return calculated_hash == hash_value
        except Exception as e:
            print(f"Validation error: {e}")
            return False

    def parse_telegram_user(init_data: str) -> dict[str, Any] | None:
        """Parse the Telegram user object from initData without trusting it for auth."""
        if not init_data:
            return None
        try:
            params = dict(parse_qsl(init_data, keep_blank_values=True))
            raw_user = params.get("user")
            return json.loads(raw_user) if raw_user else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def get_request_user() -> dict[str, Any] | None:
        return parse_telegram_user(request.headers.get("X-Init-Data", ""))

    def secure_path(path: str = "") -> Path:
        target = (app.download_dir / path).resolve()
        base = app.download_dir.resolve()
        try:
            target.relative_to(base)
        except ValueError as exc:
            raise PermissionError("Access denied") from exc
        return target

    def run_bot_coroutine(coro: Any) -> Any:
        if app.bot_loop is None:
            raise RuntimeError("Bot event loop is not available")

        import asyncio

        future = asyncio.run_coroutine_threadsafe(coro, app.bot_loop)
        return future.result(timeout=30)

    def schedule_bot_coroutine(coro: Any) -> None:
        if app.bot_loop is None:
            raise RuntimeError("Bot event loop is not available")

        import asyncio

        asyncio.run_coroutine_threadsafe(coro, app.bot_loop)

    def require_telegram_auth(f):
        """Decorator to validate Telegram auth for API endpoints."""

        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Optional: Enable this for production
            # init_data = request.headers.get('X-Init-Data', '')
            # if not validate_telegram_init_data(init_data):
            #     return jsonify({'error': 'Unauthorized'}), 401

            return f(*args, **kwargs)

        return decorated_function

    def get_file_info(file_path: Path) -> dict[str, Any]:
        """Get file metadata."""
        stat = file_path.stat()
        is_dir = file_path.is_dir()

        # Get thumbnail for images/videos if possible
        thumbnail = None
        if file_path.suffix.lower() in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
            thumbnail = f"/api/thumbnail/{file_path.name}"

        return {
            "name": file_path.name,
            "path": str(file_path.relative_to(app.download_dir)),
            "type": "folder" if is_dir else "file",
            "size": stat.st_size if not is_dir else 0,
            "size_readable": format_size(stat.st_size) if not is_dir else "-",
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "thumbnail": thumbnail,
            "extension": file_path.suffix.lower() if not is_dir else "",
        }

    def serialize_download_job(job: dict[str, Any]) -> dict[str, Any]:
        total = int(job.get("total_length", 0) or 0)
        completed = int(job.get("completed_length", 0) or 0)
        progress = float(job.get("progress", 0.0) or 0.0)
        return {
            "id": job.get("id"),
            "name": job.get("name", "Unknown download"),
            "status": job.get("status", "unknown"),
            "aria2_status": job.get("aria2_status", "unknown"),
            "gid": job.get("gid"),
            "source_type": job.get("source_type", "download"),
            "progress": progress,
            "completed_length": completed,
            "total_length": total,
            "completed_readable": format_size(completed),
            "total_readable": format_size(total),
            "download_speed": int(job.get("download_speed", 0) or 0),
            "upload_speed": int(job.get("upload_speed", 0) or 0),
            "eta": job.get("eta", "Unknown"),
            "peers": int(job.get("connections", 0) or 0),
            "seeders": int(job.get("num_seeders", 0) or 0),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
        }

    def expand_selected_files(paths: list[str]) -> tuple[list[str], int]:
        files: list[str] = []
        total_size = 0
        seen: set[str] = set()

        for path in paths:
            target = secure_path(path)
            candidates = (
                [target]
                if target.is_file()
                else [item for item in target.rglob("*") if item.is_file()]
            )
            for item in candidates:
                rel_path = str(item.relative_to(app.download_dir.resolve()))
                if rel_path in seen:
                    continue
                seen.add(rel_path)
                files.append(rel_path)
                total_size += item.stat().st_size

        return files, total_size

    def request_user_id() -> int:
        data = request.get_json(silent=True) or {}
        user = get_request_user() or {}
        return int(
            data.get("user_id")
            or request.args.get("user_id")
            or user.get("id")
            or app.default_chat_id
            or 0
        )

    def request_chat_id() -> int:
        data = request.get_json(silent=True) or {}
        user = get_request_user() or {}
        return int(
            data.get("chat_id")
            or request.args.get("chat_id")
            or user.get("id")
            or app.default_chat_id
            or 0
        )

    def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
        return {
            "zip_part_size": int(settings.get("zip_part_size", DEFAULT_SETTINGS["zip_part_size"])),
            "zip_method": settings.get("zip_method", "zip"),
            "password": settings.get("password", ""),
            "auto_delete_files_after_zip": bool(settings.get("auto_delete_files_after_zip")),
            "auto_delete_zips_after_send": bool(settings.get("auto_delete_zips_after_send")),
            "auto_delete_files_after_upload": bool(settings.get("auto_delete_files_after_upload")),
            "auto_download_forwarded_posts": bool(settings.get("auto_download_forwarded_posts")),
            "compression_level": int(settings.get("compression_level", 3)),
        }

    def format_size(bytes_size: int) -> str:
        """Format bytes to human readable size."""
        for unit in ["B", "KB", "MB", "GB"]:
            if bytes_size < 1024:
                return f"{bytes_size:.2f}{unit}"
            bytes_size /= 1024
        return f"{bytes_size:.2f}TB"

    def get_icon_for_file(filename: str) -> str:
        """Get icon emoji for file type."""
        ext = Path(filename).suffix.lower()

        icons = {
            # Media
            ".mp4": "🎬",
            ".mkv": "🎬",
            ".avi": "🎬",
            ".mov": "🎬",
            ".mp3": "🎵",
            ".wav": "🎵",
            ".flac": "🎵",
            ".aac": "🎵",
            ".jpg": "🖼️",
            ".jpeg": "🖼️",
            ".png": "🖼️",
            ".gif": "🖼️",
            ".webp": "🖼️",
            # Documents
            ".pdf": "📄",
            ".txt": "📝",
            ".doc": "📄",
            ".docx": "📄",
            ".xls": "📊",
            ".xlsx": "📊",
            ".csv": "📊",
            # Archives
            ".zip": "📦",
            ".rar": "📦",
            ".7z": "📦",
            ".tar": "📦",
            ".gz": "📦",
            # Code
            ".py": "🐍",
            ".js": "📜",
            ".ts": "📜",
            ".html": "🌐",
            ".css": "🎨",
        }

        return icons.get(ext, "📄")

    # ==================== ROUTES ====================

    @app.route("/")
    def index():
        """Serve the mini-app HTML."""
        return render_template("index.html")

    @app.route("/api/files")
    @require_telegram_auth
    def list_files():
        """List files in a directory."""
        path_param = request.args.get("path", "").strip()

        # Security: prevent directory traversal
        try:
            target_dir = secure_path(path_param) if path_param else app.download_dir.resolve()

            if not target_dir.exists():
                return jsonify({"error": "Directory not found"}), 404

            if not target_dir.is_dir():
                return jsonify({"error": "Not a directory"}), 400

            # Get all items
            items = []
            try:
                for item in sorted(
                    target_dir.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())
                ):
                    try:
                        file_info = get_file_info(item)
                        file_info["icon"] = get_icon_for_file(item.name)
                        items.append(file_info)
                    except (OSError, PermissionError):
                        continue
            except PermissionError:
                return jsonify({"error": "Permission denied"}), 403

            return jsonify(
                {
                    "current_path": (
                        str(target_dir.relative_to(app.download_dir.resolve()))
                        if path_param
                        else ""
                    ),
                    "items": items,
                    "total_size": format_size(
                        sum(f.stat().st_size for f in target_dir.rglob("*") if f.is_file())
                    ),
                }
            )

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/files/delete", methods=["POST"])
    @require_telegram_auth
    def delete_files():
        """Delete files or directories."""
        data = request.get_json()
        paths = data.get("paths", [])

        if not paths:
            return jsonify({"error": "No paths provided"}), 400

        deleted = []
        errors = []

        for path in paths:
            try:
                # Security check
                file_path = secure_path(path)

                if not file_path.exists():
                    errors.append({"path": path, "error": "File not found"})
                    continue

                if file_path.is_dir():
                    shutil.rmtree(file_path)
                else:
                    file_path.unlink()

                deleted.append(path)

            except Exception as e:
                errors.append({"path": path, "error": str(e)})

        return jsonify(
            {"deleted": deleted, "errors": errors, "message": f"Deleted {len(deleted)} item(s)"}
        )

    @app.route("/api/files/search")
    @require_telegram_auth
    def search_files():
        """Search files by name or extension."""
        query = request.args.get("q", "").lower().strip()
        file_type = request.args.get("type", "").lower()

        if not query and not file_type:
            return jsonify({"error": "No search query"}), 400

        results = []

        try:
            for file_path in app.download_dir.rglob("*"):
                if len(results) >= 100:  # Limit results
                    break

                try:
                    if (
                        query
                        and query in file_path.name.lower()
                        or file_type
                        and file_path.suffix.lower() == f".{file_type}"
                    ):
                        results.append(get_file_info(file_path))
                except (OSError, PermissionError):
                    continue

        except Exception as e:
            return jsonify({"error": str(e)}), 500

        return jsonify({"results": results})

    @app.route("/api/files/create-archive", methods=["POST"])
    @require_telegram_auth
    def create_archive():
        """Create a zip or 7z archive of selected files."""
        data = request.get_json()
        paths = data.get("paths", [])
        archive_name = data.get("name", "archive").strip()
        archive_format = data.get("format", "zip").lower()  # 'zip' or '7z'

        if not paths:
            return jsonify({"error": "No files selected"}), 400

        if not archive_name:
            archive_name = f"archive_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        # Sanitize archive name
        archive_name = "".join(c for c in archive_name if c.isalnum() or c in "._-")

        try:
            # Verify all paths exist and are within download_dir
            files_to_archive = []
            for path in paths:
                file_path = secure_path(path)
                if not file_path.exists():
                    return jsonify({"error": f"Not found: {path}"}), 404
                files_to_archive.append(file_path)

            # Create archive
            archive_path = app.download_dir / f"{archive_name}.{archive_format}"

            if archive_format == "zip":
                with zipfile.ZipFile(
                    archive_path, "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    for file_path in files_to_archive:
                        if file_path.is_file():
                            archive.write(
                                file_path, arcname=file_path.relative_to(app.download_dir)
                            )
                        else:
                            for item in file_path.rglob("*"):
                                if item.is_file():
                                    archive.write(item, arcname=item.relative_to(app.download_dir))
            elif archive_format == "7z":
                # For 7z, we'd need py7zr library (already in requirements)
                import py7zr

                with py7zr.SevenZipFile(str(archive_path), "w") as archive:
                    for file_path in files_to_archive:
                        if file_path.is_file():
                            archive.write(file_path, arcname=file_path.name)
                        else:
                            for item in file_path.rglob("*"):
                                if item.is_file():
                                    rel_path = item.relative_to(file_path.parent)
                                    archive.write(item, arcname=rel_path)
            else:
                return jsonify({"error": "Unsupported archive format"}), 400

            return jsonify(
                {
                    "success": True,
                    "archive": archive_name + f".{archive_format}",
                    "size": format_size(archive_path.stat().st_size),
                }
            )

        except Exception as e:
            return jsonify({"error": f"Archive creation failed: {str(e)}"}), 500

    @app.route("/api/files/zip-upload", methods=["POST"])
    @require_telegram_auth
    def zip_upload_selected_files():
        """Create a ZIP job, upload archives to Telegram, and expose live progress."""
        if app.zip_selected is None or app.bot_app is None or app.bot_loop is None:
            return jsonify({"error": "ZIP upload control is not available"}), 503

        data = request.get_json() or {}
        paths = data.get("paths", [])
        if not paths:
            return jsonify({"error": "No files selected"}), 400

        chat_id = request_chat_id()
        user_id = request_user_id()
        if not chat_id:
            return jsonify({"error": "Open the mini-app from your bot chat first"}), 400

        try:
            files, total_size = expand_selected_files(paths)
            if not files:
                return jsonify({"error": "No files selected"}), 400

            job_id = uuid.uuid4().hex[:12]
            app.zip_jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "phase": "queued",
                "progress_text": "Queued...",
                "file_count": len(files),
                "total_size": format_size(total_size),
                "total_size_bytes": total_size,
                "created": [],
                "started_at": time.time(),
                "updated_at": time.time(),
            }
            schedule_bot_coroutine(
                app.zip_selected(app.bot_app, int(chat_id), files, int(user_id), job_id)
            )
            return jsonify({"job": app.zip_jobs[job_id]})
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/zip-jobs")
    @require_telegram_auth
    def list_zip_jobs():
        return jsonify(
            {
                "jobs": sorted(
                    app.zip_jobs.values(),
                    key=lambda job: job.get("started_at", 0),
                    reverse=True,
                )
            }
        )

    @app.route("/api/files/upload", methods=["POST"])
    @require_telegram_auth
    def upload_files():
        """Upload local browser files into the current mini-app folder."""
        target_path = request.form.get("path", "").strip()
        try:
            target_dir = secure_path(target_path)
            target_dir.mkdir(parents=True, exist_ok=True)
            if not target_dir.is_dir():
                return jsonify({"error": "Upload target is not a directory"}), 400

            saved = []
            for incoming in request.files.getlist("files"):
                if not incoming.filename:
                    continue
                safe_name = Path(incoming.filename).name
                destination = secure_path(str(Path(target_path) / safe_name))
                incoming.save(destination)
                saved.append(str(destination.relative_to(app.download_dir.resolve())))

            return jsonify({"saved": saved, "message": f"Uploaded {len(saved)} file(s)"})
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/files/selection-summary", methods=["POST"])
    @require_telegram_auth
    def selection_summary():
        """Return recursive file count and size for selected files/folders."""
        data = request.get_json() or {}
        paths = data.get("paths", [])
        try:
            files, total_size = expand_selected_files(paths)
            return jsonify(
                {
                    "file_count": len(files),
                    "total_size": format_size(total_size),
                    "total_size_bytes": total_size,
                }
            )
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/files/upload-selected", methods=["POST"])
    @require_telegram_auth
    def upload_selected_files():
        """Upload selected VPS files to the Telegram user account."""
        if app.upload_selected is None or app.bot_app is None or app.bot_loop is None:
            return jsonify({"error": "Telegram upload control is not available"}), 503

        data = request.get_json() or {}
        paths = data.get("paths", [])
        if not paths:
            return jsonify({"error": "No files selected"}), 400

        chat_id = request_chat_id()
        user_id = request_user_id()
        if not chat_id:
            return jsonify({"error": "Open the mini-app from your bot chat first"}), 400

        try:
            files, _ = expand_selected_files(paths)
            if not files:
                return jsonify({"error": "No files selected"}), 400
            result = run_bot_coroutine(
                app.upload_selected(app.bot_app, int(chat_id), files, int(user_id))
            )
            return jsonify({"success": True, "message": result, "file_count": len(files)})
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/downloads")
    @require_telegram_auth
    def list_downloads():
        jobs = [
            serialize_download_job(job)
            for job in sorted(
                app.download_jobs.values(), key=lambda item: item.get("id", 0), reverse=True
            )
        ]
        active = [job for job in jobs if job["status"] not in {"completed", "failed", "cancelled"}]
        return jsonify(
            {
                "jobs": jobs,
                "active": active,
                "updated_at": time.time(),
                "total_down_speed": sum(job["download_speed"] for job in active),
                "total_up_speed": sum(job["upload_speed"] for job in active),
            }
        )

    @app.route("/api/downloads/start", methods=["POST"])
    @require_telegram_auth
    def start_download_route():
        if app.start_download is None or app.bot_app is None or app.bot_loop is None:
            return jsonify({"error": "Download control is not available"}), 503

        data = request.get_json() or {}
        sources = [item.strip() for item in data.get("sources", []) if str(item).strip()]
        if not sources and data.get("source"):
            sources = [str(data["source"]).strip()]
        if not sources:
            return jsonify({"error": "No URL or magnet link provided"}), 400

        chat_id = request_chat_id()
        user_id = request_user_id()
        if not chat_id:
            return jsonify({"error": "Open the mini-app from your bot chat first"}), 400

        started = []
        errors = []
        for source in sources:
            try:
                job = run_bot_coroutine(
                    app.start_download(app.bot_app, int(chat_id), source, int(user_id))
                )
                started.append(serialize_download_job(job))
            except Exception as exc:
                errors.append({"source": source, "error": str(exc)})

        status = 200 if started else 500
        return jsonify({"started": started, "errors": errors}), status

    @app.route("/api/downloads/<int:job_id>/<action>", methods=["POST"])
    @require_telegram_auth
    def control_download_route(job_id: int, action: str):
        handlers = {
            "pause": app.pause_download,
            "resume": app.resume_download,
            "cancel": app.cancel_download,
        }
        handler = handlers.get(action)
        if handler is None:
            return jsonify({"error": "Unknown action"}), 400
        try:
            ok, message = run_bot_coroutine(handler(job_id))
            return jsonify(
                {"success": ok, "message": message, "job": app.download_jobs.get(job_id)}
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/settings", methods=["GET", "POST"])
    @require_telegram_auth
    def settings_route():
        user_id = request_user_id()

        if request.method == "GET":
            return jsonify({"settings": public_settings(get_user_settings(user_id))})

        data = request.get_json() or {}
        key = data.get("key")
        value = data.get("value")
        if key not in DEFAULT_SETTINGS:
            return jsonify({"error": "Unknown setting"}), 400

        try:
            if key == "zip_part_size":
                value = int(value)
                if not validate_part_size(value // (1024 * 1024)):
                    return jsonify({"error": "Part size must be 100 MB to 5 GB"}), 400
            elif key == "compression_level":
                value = int(value)
                if not validate_compression_level(value):
                    return jsonify({"error": "Compression level must be 1 to 9"}), 400
            elif key == "zip_method":
                value = str(value).lower()
                if value not in {"zip", "7z"}:
                    return jsonify({"error": "Method must be ZIP or 7Z"}), 400
            elif key in {
                "auto_delete_files_after_zip",
                "auto_delete_zips_after_send",
                "auto_delete_files_after_upload",
                "auto_download_forwarded_posts",
            }:
                value = bool(value)
            elif key == "password":
                value = str(value or "")[:100]

            settings = get_user_settings(user_id)
            settings[key] = value
            if not save_user_settings(user_id, settings):
                return jsonify({"error": "Could not save settings"}), 500
            return jsonify({"settings": public_settings(settings)})
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid setting value"}), 400

    @app.route("/api/files/download/<path:filename>")
    @require_telegram_auth
    def download_file(filename: str):
        """Download a file."""
        try:
            file_path = secure_path(filename)

            if not file_path.exists() or not file_path.is_file():
                return jsonify({"error": "File not found"}), 404

            return send_file(file_path, as_attachment=True)

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/thumbnail/<filename>")
    def get_thumbnail(filename: str):
        """Get thumbnail for image files."""
        try:
            file_path = secure_path(filename)

            if not file_path.exists() or not file_path.is_file():
                return jsonify({"error": "File not found"}), 404

            # Check if it's an image
            if file_path.suffix.lower() not in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
                return jsonify({"error": "Not an image"}), 400

            return send_file(file_path)

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/stats")
    @require_telegram_auth
    def get_stats():
        """Get storage statistics."""
        try:
            total_size = 0
            file_count = 0
            folder_count = 0

            for item in app.download_dir.rglob("*"):
                try:
                    if item.is_file():
                        total_size += item.stat().st_size
                        file_count += 1
                    elif item.is_dir():
                        folder_count += 1
                except (OSError, PermissionError):
                    continue

            return jsonify(
                {
                    "total_size": format_size(total_size),
                    "total_size_bytes": total_size,
                    "file_count": file_count,
                    "folder_count": folder_count,
                    "download_count": len(app.download_jobs),
                }
            )

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return app
