from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from crypto_monitor.config import Settings
from crypto_monitor.storage import SqliteStorage

logger = logging.getLogger(__name__)


def package_version() -> str:
    try:
        return version("crypto-monitor-gemini")
    except PackageNotFoundError:
        return "unknown"


def build_health_payload(settings: Settings) -> tuple[int, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {
        "database": _check_database(settings),
        "skills_root": _check_path(settings.skills_root, expected="directory"),
        "sources_file": _check_path(settings.sources_file, expected="file"),
        "telegram_bot_token": {
            "ok": bool(settings.telegram_bot_token),
            "configured": bool(settings.telegram_bot_token),
        },
        "gemini_api_key": {
            "ok": bool(settings.gemini_api_key),
            "configured": bool(settings.gemini_api_key),
        },
    }
    ready = all(
        checks[name]["ok"]
        for name in (
            "database",
            "skills_root",
            "sources_file",
            "telegram_bot_token",
            "gemini_api_key",
        )
    )
    payload: dict[str, Any] = {
        "ok": ready,
        "service": "crypto-monitor",
        "version": package_version(),
        "env": settings.env,
        "time": datetime.now(UTC).isoformat(),
        "checks": checks,
    }
    return (200 if ready else 503), payload


def start_health_server(
    settings: Settings,
    *,
    host: str = "0.0.0.0",
    port: int | None = None,
) -> ThreadingHTTPServer:
    bind_port = port or int(os.getenv("PORT", "8080"))

    class HealthHandler(BaseHTTPRequestHandler):
        server_version = "CryptoMonitorHealth/1.0"

        def do_GET(self) -> None:  # noqa: N802
            if self.path in {"/live", "/"}:
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "service": "crypto-monitor",
                        "version": package_version(),
                        "time": datetime.now(UTC).isoformat(),
                    },
                )
                return
            if self.path == "/health":
                status, payload = build_health_payload(settings)
                self._send_json(status, payload)
                return
            self._send_json(404, {"ok": False, "error": "not_found"})

        def log_message(self, format: str, *args: Any) -> None:
            logger.info("healthcheck %s", format % args)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, bind_port), HealthHandler)
    logger.info("health_server_started host=%s port=%s", host, bind_port)
    return server


def _check_database(settings: Settings) -> dict[str, Any]:
    try:
        status = SqliteStorage(settings.db_path).export_json()
    except Exception as exc:
        return {
            "ok": False,
            "path": str(settings.db_path),
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": True,
        "path": status["path"],
        "raw_articles": status["raw_articles"],
        "processed_articles": status["processed_articles"],
        "digests": status["digests"],
        "telegram_chats": status["telegram_chats"],
    }


def _check_path(path: Any, *, expected: str) -> dict[str, Any]:
    if expected == "directory":
        ok = path.exists() and path.is_dir()
    elif expected == "file":
        ok = path.exists() and path.is_file()
    else:
        ok = path.exists()
    return {
        "ok": ok,
        "path": str(path),
        "expected": expected,
    }
