from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any, Mapping
from zoneinfo import ZoneInfo


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # Avoid HTTPServer's reverse-DNS lookup, which can stall for ~40s on macOS.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address


class DashboardServer:
    """Small, read-only localhost dashboard backed by the existing audit store."""

    def __init__(
        self,
        database: Path,
        runtime_state: Path,
        html: Path,
        contacts: Mapping[str, str],
        timezone: ZoneInfo,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self.database = database.resolve()
        self.runtime_state = runtime_state.resolve()
        self.html = html.resolve()
        self.contacts = dict(contacts)
        self.timezone = timezone
        self.host = host
        self.port = port
        self.server: LocalThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/":
                    self._send(dashboard.html.read_bytes(), "text/html; charset=utf-8")
                    return
                if path == "/api/snapshot":
                    payload = json.dumps(
                        dashboard.snapshot(), ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                    self._send(payload, "application/json; charset=utf-8")
                    return
                self.send_error(404)

            def _send(self, payload: bytes, content_type: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "script-src 'self' 'unsafe-inline'; connect-src 'self'",
                )
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = LocalThreadingHTTPServer((self.host, self.port), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="dws-agent-dashboard",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=5)

    def snapshot(self, limit: int = 100) -> dict[str, Any]:
        runtime = self._runtime()
        active_conversations = {
            item.get("conversationId") for item in runtime.get("active", [])
        }
        with sqlite3.connect(
            f"file:{self.database.as_posix()}?mode=ro", uri=True
        ) as connection:
            connection.row_factory = sqlite3.Row
            recent = self._recent_runs(connection, limit)
            queued = self._queued(connection, active_conversations)
            summary = self._summary(connection, runtime, queued)
        active = []
        for item in runtime.get("active", []):
            visible = dict(item)
            visible.pop("conversationId", None)
            active.append(visible)
        service = {
            "running": bool(runtime),
            "mode": runtime.get("mode", "unknown"),
            "pid": runtime.get("pid"),
            "heartbeatAt": runtime.get("heartbeatAt"),
            "contacts": runtime.get("contacts", len(self.contacts)),
            "capacity": runtime.get("capacity", 0),
        }
        return {
            "generatedAt": datetime.now(self.timezone).isoformat(),
            "service": service,
            "summary": summary,
            "active": active,
            "queue": queued,
            "recent": recent,
        }

    def _runtime(self) -> dict[str, Any]:
        try:
            value = json.loads(self.runtime_state.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _queued(
        self, connection: sqlite3.Connection, active_conversations: set[Any]
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT conversation_id, contact_user_id, MIN(received_at) AS received_at,
                   COUNT(*) AS batch_size, SUM(content_length) AS content_length,
                   GROUP_CONCAT(request_preview, CHAR(10)) AS request_preview
            FROM (
                SELECT * FROM events WHERE status = 'queued' ORDER BY received_at ASC
            )
            GROUP BY conversation_id, contact_user_id
            ORDER BY received_at ASC
            """
        ).fetchall()
        return [
            {
                "contactName": self.contacts.get(row["contact_user_id"], "未知联系人"),
                "receivedAt": row["received_at"],
                "batchSize": row["batch_size"],
                "contentLength": row["content_length"],
                "requestPreview": (row["request_preview"] or "")[:500],
            }
            for row in rows
            if row["conversation_id"] not in active_conversations
        ]

    def _recent_runs(
        self, connection: sqlite3.Connection, limit: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT session_id, contact_name, started_at, finished_at, action, status,
                   handled, reason, changes_json, validation_json, external_calls_json,
                   warnings_json, codex_exit_code, request_preview
            FROM runs ORDER BY finished_at DESC LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            changes = self._json_list(row["changes_json"])
            safe_changes = [
                {
                    key: change.get(key)
                    for key in (
                        "repo",
                        "branch",
                        "head_sha",
                        "commits",
                        "files",
                        "pushed_to",
                        "verified",
                        "warning",
                    )
                }
                for change in changes
                if isinstance(change, dict)
            ]
            result.append(
                {
                    "sessionId": row["session_id"],
                    "contactName": row["contact_name"],
                    "startedAt": row["started_at"],
                    "finishedAt": row["finished_at"],
                    "action": row["action"],
                    "status": row["status"],
                    "handled": row["handled"],
                    "reason": row["reason"],
                    "requestPreview": row["request_preview"],
                    "changes": safe_changes,
                    "validation": self._json_list(row["validation_json"]),
                    "externalCalls": self._json_list(row["external_calls_json"]),
                    "warnings": self._json_list(row["warnings_json"]),
                    "codexExitCode": row["codex_exit_code"],
                }
            )
        return result

    def _summary(
        self,
        connection: sqlite3.Connection,
        runtime: Mapping[str, Any],
        queued: list[dict[str, Any]],
    ) -> dict[str, int]:
        day_start = datetime.now(self.timezone).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        rows = connection.execute(
            "SELECT status, changes_json FROM runs WHERE finished_at >= ?",
            (day_start,),
        ).fetchall()
        sent = errors = changed = 0
        for row in rows:
            sent += row["status"] in {"sent", "shadow"}
            errors += row["status"] == "error"
            changed += bool(self._json_list(row["changes_json"]))
        return {
            "active": len(runtime.get("active", [])),
            "queued": len(queued),
            "sentToday": sent,
            "errorsToday": errors,
            "changesToday": changed,
        }

    @staticmethod
    def _json_list(value: Any) -> list[Any]:
        try:
            parsed = json.loads(value or "[]")
            return parsed if isinstance(parsed, list) else []
        except (TypeError, json.JSONDecodeError):
            return []
