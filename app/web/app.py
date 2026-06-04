"""Flask web application for Telegram mini-app file browser."""

import hashlib
import hmac
import shutil
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_file
from flask_cors import CORS


def create_web_app(download_dir: str, bot_token: str) -> Flask:
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
    app.download_dir = Path(download_dir)
    app.bot_token = bot_token

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
            if path_param:
                target_dir = app.download_dir / path_param
                target_dir = target_dir.resolve()

                # Ensure the resolved path is still within download_dir
                if not str(target_dir).startswith(str(app.download_dir)):
                    return jsonify({"error": "Access denied"}), 403
            else:
                target_dir = app.download_dir

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
                        str(target_dir.relative_to(app.download_dir)) if path_param else ""
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
                file_path = (app.download_dir / path).resolve()
                if not str(file_path).startswith(str(app.download_dir)):
                    errors.append({"path": path, "error": "Access denied"})
                    continue

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
                file_path = (app.download_dir / path).resolve()
                if not str(file_path).startswith(str(app.download_dir)):
                    return jsonify({"error": f"Access denied: {path}"}), 403
                if not file_path.exists():
                    return jsonify({"error": f"Not found: {path}"}), 404
                files_to_archive.append(file_path)

            # Create archive
            archive_path = app.download_dir / f"{archive_name}.{archive_format}"

            if archive_format == "zip":
                shutil.make_archive(
                    str(archive_path.with_suffix("")),
                    "zip",
                    app.download_dir,
                    *[Path(p).name for p in paths],
                )
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

    @app.route("/api/files/download/<path:filename>")
    @require_telegram_auth
    def download_file(filename: str):
        """Download a file."""
        try:
            file_path = (app.download_dir / filename).resolve()

            # Security check
            if not str(file_path).startswith(str(app.download_dir)):
                return jsonify({"error": "Access denied"}), 403

            if not file_path.exists() or not file_path.is_file():
                return jsonify({"error": "File not found"}), 404

            return send_file(file_path, as_attachment=True)

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/thumbnail/<filename>")
    def get_thumbnail(filename: str):
        """Get thumbnail for image files."""
        try:
            file_path = (app.download_dir / filename).resolve()

            # Security check
            if not str(file_path).startswith(str(app.download_dir)):
                return jsonify({"error": "Access denied"}), 403

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
                }
            )

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return app
