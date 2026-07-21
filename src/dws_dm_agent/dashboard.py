from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from datetime import datetime, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any, Callable, ContextManager, Mapping

from .prompts import (
    STAGE_PROMPT_VARIABLES,
    SUPPLEMENT_PROMPT_VARIABLES,
    validate_template_variables,
)
from .runners.runtime import AgentRuntime, SUPPORTED_PROTOCOLS


class ConfigBusyError(RuntimeError):
    pass


class ConfigConflictError(RuntimeError):
    pass


_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_DEFAULT_PROTOCOLS = {
    "codex": "codex-app-server",
    "claude": "claude-stream-json",
    "claude-code": "claude-stream-json",
    "pi": "pi-rpc",
}
_PROFILE_FIELDS = {
    "driver",
    "protocol",
    "command",
    "model",
    "reasoning_effort",
    "read_only",
    "timeout_seconds",
}
_STAGE_FIELDS = {"label", "agent", "prompt", "schema"}
_AUTO_FIELDS = {
    "ack",
    "progress",
    "progress_enabled",
    "progress_interval_seconds",
    "max_progress_updates",
}
_ACK_VARIABLES = frozenset(
    {"request_count", "contact_name", "contact_alias", "self_name"}
)
_PROGRESS_VARIABLES = frozenset({*_ACK_VARIABLES, "progress"})


class AgentConfigStore:
    """Safe, revisioned editor for public agent/workflow/prompt configuration."""

    def __init__(
        self,
        config_path: Path,
        workspace_root: Path,
        *,
        on_apply: Callable[[Mapping[str, Any]], None] | None = None,
        is_idle: Callable[[], bool] | None = None,
        mutation_lock: ContextManager[None] | None = None,
    ) -> None:
        self.config_path = config_path.resolve()
        self.config_dir = self.config_path.parent
        self.workspace_root = workspace_root.resolve()
        self.on_apply = on_apply
        self.is_idle = is_idle or (lambda: True)
        self._lock = threading.Lock()
        self._mutation_lock = mutation_lock or threading.RLock()

    def snapshot(self) -> dict[str, Any]:
        raw_bytes = self.config_path.read_bytes()
        raw = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError("config root must be an object")
        agents: dict[str, dict[str, Any]] = {}
        for raw_name, raw_profile in raw.get("agents", {}).items():
            if not isinstance(raw_profile, Mapping):
                continue
            name = str(raw_name)
            command = raw_profile.get("command")
            if not isinstance(command, list):
                command = [str(raw_profile.get("binary") or raw_profile.get("driver") or "")]
            agents[name] = {
                "driver": str(raw_profile.get("driver") or ""),
                "protocol": str(
                    raw_profile.get("protocol")
                    or _DEFAULT_PROTOCOLS.get(
                        str(raw_profile.get("driver") or "").lower(), ""
                    )
                ),
                "command": [str(item) for item in command],
                "model": str(raw_profile.get("model") or ""),
                "reasoning_effort": str(raw_profile.get("reasoning_effort") or ""),
                "read_only": bool(raw_profile.get("read_only", False)),
                "timeout_seconds": float(raw_profile.get("timeout_seconds", 60)),
            }
        workflows = self._public_workflows(raw.get("workflows", {}))
        prompts: dict[str, str] = {}
        locked_prompts: list[str] = []
        for text_path in sorted(self._prompt_kinds(workflows)):
            path = self._editable_prompt_path(text_path)
            if path is None:
                locked_prompts.append(text_path)
                continue
            try:
                prompts[text_path] = path.read_text(encoding="utf-8")
            except OSError:
                prompts[text_path] = ""
        return {
            "revision": hashlib.sha256(raw_bytes).hexdigest(),
            "supportedProtocols": list(SUPPORTED_PROTOCOLS),
            "agents": agents,
            "workflows": workflows,
            "prompts": prompts,
            "lockedPrompts": locked_prompts,
        }

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("configuration payload must be an object")
        with self._lock, self._mutation_lock:
            if not self.is_idle():
                raise ConfigBusyError("有会话正在执行或排队，请空闲后再保存配置")
            current_bytes = self.config_path.read_bytes()
            current_revision = hashlib.sha256(current_bytes).hexdigest()
            if str(payload.get("revision") or "") != current_revision:
                raise ConfigConflictError("配置已被其他操作修改，请重新载入后再保存")
            current = json.loads(current_bytes.decode("utf-8"))
            if not isinstance(current, dict):
                raise ValueError("config root must be an object")
            candidate = copy.deepcopy(current)
            candidate["agents"] = self._merge_agents(
                current.get("agents", {}), payload.get("agents")
            )
            candidate["workflows"] = self._merge_workflows(
                current.get("workflows", {}), payload.get("workflows")
            )
            prompt_updates = self._validated_prompt_updates(
                candidate["workflows"], payload.get("prompts", {})
            )
            self._validate_candidate(candidate)

            originals = {path: path.read_bytes() for path in prompt_updates}
            try:
                for path, content in prompt_updates.items():
                    self._atomic_write(path, content.encode("utf-8"))
                serialized = (
                    json.dumps(candidate, ensure_ascii=False, indent=2) + "\n"
                ).encode("utf-8")
                self._atomic_write(self.config_path, serialized)
                if self.on_apply is not None:
                    self.on_apply(candidate)
            except Exception:
                self._atomic_write(self.config_path, current_bytes)
                for path, content in originals.items():
                    self._atomic_write(path, content)
                raise
            return self.snapshot()

    @staticmethod
    def _public_workflows(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {"active": "", "presets": {}}
        public: dict[str, Any] = {
            "active": str(value.get("active") or ""),
            "presets": {},
        }
        presets = value.get("presets", {})
        if not isinstance(presets, Mapping):
            return public
        for raw_name, raw_preset in presets.items():
            if not isinstance(raw_preset, Mapping):
                continue
            preset: dict[str, Any] = {
                "supplement_strategy": str(
                    raw_preset.get("supplement_strategy") or "steer"
                ),
                "supplement_prompt": str(
                    raw_preset.get("supplement_prompt") or ""
                ),
                "auto_messages": {
                    key: copy.deepcopy(item)
                    for key, item in (
                        raw_preset.get("auto_messages", {}).items()
                        if isinstance(raw_preset.get("auto_messages"), Mapping)
                        else []
                    )
                    if key in _AUTO_FIELDS
                },
            }
            for stage_name in ("front", "worker"):
                raw_stage = raw_preset.get(stage_name, {})
                preset[stage_name] = {
                    key: copy.deepcopy(item)
                    for key, item in (
                        raw_stage.items() if isinstance(raw_stage, Mapping) else []
                    )
                    if key in _STAGE_FIELDS
                }
            public["presets"][str(raw_name)] = preset
        return public

    @staticmethod
    def _merge_agents(current: Any, submitted: Any) -> dict[str, Any]:
        if not isinstance(submitted, Mapping) or not submitted:
            raise ValueError("at least one agent profile is required")
        current = current if isinstance(current, Mapping) else {}
        merged: dict[str, Any] = {}
        for raw_name, raw_profile in submitted.items():
            name = str(raw_name)
            if not _PROFILE_NAME.fullmatch(name):
                raise ValueError(f"invalid agent profile name: {name}")
            if not isinstance(raw_profile, Mapping):
                raise ValueError(f"agent profile must be an object: {name}")
            profile = copy.deepcopy(
                current.get(name, {}) if isinstance(current.get(name), Mapping) else {}
            )
            for key in _PROFILE_FIELDS:
                if key in raw_profile:
                    profile[key] = copy.deepcopy(raw_profile[key])
            command = profile.get("command")
            if (
                not isinstance(command, list)
                or not command
                or len(command) > 32
                or any(
                    not isinstance(item, str)
                    or not item.strip()
                    or len(item) > 2048
                    or "\x00" in item
                    or "\n" in item
                    for item in command
                )
            ):
                raise ValueError(f"agent command is invalid: {name}")
            protocol = str(profile.get("protocol") or "")
            if protocol not in SUPPORTED_PROTOCOLS:
                raise ValueError(f"unsupported agent protocol: {protocol or name}")
            timeout = float(profile.get("timeout_seconds", 60))
            if timeout <= 0 or timeout > 86400:
                raise ValueError(f"agent timeout must be between 1 and 86400: {name}")
            profile["timeout_seconds"] = int(timeout) if timeout.is_integer() else timeout
            profile["driver"] = str(profile.get("driver") or "custom")[:80]
            profile["model"] = str(profile.get("model") or "")[:200]
            profile["reasoning_effort"] = str(
                profile.get("reasoning_effort") or ""
            )[:80]
            profile["read_only"] = bool(profile.get("read_only", False))
            profile.pop("binary", None)
            merged[name] = profile
        return merged

    @classmethod
    def _merge_workflows(cls, current: Any, submitted: Any) -> dict[str, Any]:
        if not isinstance(submitted, Mapping):
            raise ValueError("workflows configuration must be an object")
        current = current if isinstance(current, Mapping) else {}
        raw_presets = submitted.get("presets")
        if not isinstance(raw_presets, Mapping) or not raw_presets:
            raise ValueError("at least one workflow preset is required")
        current_presets = current.get("presets", {})
        current_presets = current_presets if isinstance(current_presets, Mapping) else {}
        presets: dict[str, Any] = {}
        for raw_name, raw_preset in raw_presets.items():
            name = str(raw_name)
            if not _PROFILE_NAME.fullmatch(name):
                raise ValueError(f"invalid workflow preset name: {name}")
            if not isinstance(raw_preset, Mapping):
                raise ValueError(f"workflow preset must be an object: {name}")
            preset = copy.deepcopy(
                current_presets.get(name, {})
                if isinstance(current_presets.get(name), Mapping)
                else {}
            )
            preset["supplement_strategy"] = str(
                raw_preset.get("supplement_strategy") or "steer"
            )
            preset["supplement_prompt"] = str(
                raw_preset.get("supplement_prompt") or ""
            )
            raw_auto = raw_preset.get("auto_messages", {})
            if not isinstance(raw_auto, Mapping):
                raise ValueError(f"workflow auto_messages must be an object: {name}")
            auto = copy.deepcopy(
                preset.get("auto_messages", {})
                if isinstance(preset.get("auto_messages"), Mapping)
                else {}
            )
            for key in _AUTO_FIELDS:
                if key in raw_auto:
                    auto[key] = copy.deepcopy(raw_auto[key])
            validate_template_variables(str(auto.get("ack") or ""), _ACK_VARIABLES)
            validate_template_variables(
                str(auto.get("progress") or ""), _PROGRESS_VARIABLES
            )
            preset["auto_messages"] = auto
            for stage_name in ("front", "worker"):
                raw_stage = raw_preset.get(stage_name)
                if not isinstance(raw_stage, Mapping):
                    raise ValueError(f"workflow {name} requires stage: {stage_name}")
                stage = copy.deepcopy(
                    preset.get(stage_name, {})
                    if isinstance(preset.get(stage_name), Mapping)
                    else {}
                )
                for key in _STAGE_FIELDS:
                    if key in raw_stage:
                        stage[key] = copy.deepcopy(raw_stage[key])
                preset[stage_name] = stage
            presets[name] = preset
        active = str(submitted.get("active") or "")
        if active not in presets:
            raise ValueError(f"active workflow preset does not exist: {active or '(empty)'}")
        result = copy.deepcopy(dict(current))
        result["active"] = active
        result["presets"] = presets
        return result

    def _validated_prompt_updates(
        self, workflows: Mapping[str, Any], submitted: Any
    ) -> dict[Path, str]:
        if not isinstance(submitted, Mapping):
            raise ValueError("prompts must be an object")
        kinds = self._prompt_kinds(workflows)
        unknown = sorted(set(str(key) for key in submitted) - set(kinds))
        if unknown:
            raise ValueError(f"prompt is not referenced by a workflow: {unknown[0]}")
        updates: dict[Path, str] = {}
        for text_path, raw_content in submitted.items():
            text_path = str(text_path)
            path = self._editable_prompt_path(text_path)
            if path is None:
                raise ValueError(f"dashboard cannot edit prompt outside config directory: {text_path}")
            if not isinstance(raw_content, str) or len(raw_content) > 250_000:
                raise ValueError(f"prompt must be text under 250000 characters: {text_path}")
            if "\x00" in raw_content:
                raise ValueError(f"prompt contains an invalid null byte: {text_path}")
            for kind in kinds[text_path]:
                validate_template_variables(
                    raw_content,
                    SUPPLEMENT_PROMPT_VARIABLES
                    if kind == "supplement"
                    else STAGE_PROMPT_VARIABLES,
                )
            updates[path] = raw_content
        return updates

    def _validate_candidate(self, candidate: Mapping[str, Any]) -> None:
        workflows = candidate.get("workflows", {})
        presets = workflows.get("presets", {}) if isinstance(workflows, Mapping) else {}
        for preset_name in presets:
            check = copy.deepcopy(dict(candidate))
            check["workflows"]["active"] = preset_name
            AgentRuntime.from_config(
                check, self.config_path, self.workspace_root
            )

    @staticmethod
    def _prompt_kinds(workflows: Mapping[str, Any]) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        presets = workflows.get("presets", {}) if isinstance(workflows, Mapping) else {}
        if not isinstance(presets, Mapping):
            return result
        for preset in presets.values():
            if not isinstance(preset, Mapping):
                continue
            supplement = str(preset.get("supplement_prompt") or "")
            if supplement:
                result.setdefault(supplement, set()).add("supplement")
            for stage_name in ("front", "worker"):
                stage = preset.get(stage_name, {})
                prompt = str(stage.get("prompt") or "") if isinstance(stage, Mapping) else ""
                if prompt:
                    result.setdefault(prompt, set()).add("stage")
        return result

    def _editable_prompt_path(self, text_path: str) -> Path | None:
        path = Path(text_path).expanduser()
        path = path.resolve() if path.is_absolute() else (self.config_dir / path).resolve()
        try:
            path.relative_to(self.config_dir)
        except ValueError:
            return None
        if path.suffix.lower() not in {".md", ".txt"}:
            return None
        return path

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode if path.exists() else 0o600
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # Avoid HTTPServer's reverse-DNS lookup, which can stall for ~40s on macOS.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address


class DashboardServer:
    """Small localhost operations dashboard backed by the existing audit store."""

    def __init__(
        self,
        database: Path,
        runtime_state: Path,
        html: Path,
        contacts: Mapping[str, str],
        timezone: tzinfo,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        config_store: AgentConfigStore | None = None,
    ) -> None:
        self.database = database.resolve()
        self.runtime_state = runtime_state.resolve()
        self.html = html.resolve()
        self.settings_html = self.html.with_name("settings.html")
        self.theme_js = self.html.with_name("theme.js")
        self.app_css = self.html.with_name("app.css")
        self.favicon = self.html.with_name("favicon.svg")
        self.contacts = dict(contacts)
        self.timezone = timezone
        self.host = host
        self.port = port
        self.config_store = config_store
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
                if path in {"/favicon.ico", "/favicon.svg"}:
                    if not dashboard.favicon.is_file():
                        self.send_error(404)
                        return
                    self._send(
                        dashboard.favicon.read_bytes(),
                        "image/svg+xml",
                        cache_control="public, max-age=86400",
                    )
                    return
                if path == "/theme.js":
                    if not dashboard.theme_js.is_file():
                        self.send_error(404)
                        return
                    self._send(
                        dashboard.theme_js.read_bytes(),
                        "text/javascript; charset=utf-8",
                    )
                    return
                if path == "/app.css":
                    if not dashboard.app_css.is_file():
                        self.send_error(404)
                        return
                    self._send(
                        dashboard.app_css.read_bytes(),
                        "text/css; charset=utf-8",
                    )
                    return
                if path == "/":
                    self._send(dashboard.html.read_bytes(), "text/html; charset=utf-8")
                    return
                if path in {"/settings", "/settings.html"}:
                    if not dashboard.settings_html.is_file():
                        self.send_error(404)
                        return
                    self._send(
                        dashboard.settings_html.read_bytes(),
                        "text/html; charset=utf-8",
                    )
                    return
                if path == "/api/snapshot":
                    payload = json.dumps(
                        dashboard.snapshot(), ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                    self._send(payload, "application/json; charset=utf-8")
                    return
                if path == "/api/config" and dashboard.config_store is not None:
                    try:
                        payload = json.dumps(
                            dashboard.config_store.snapshot(),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    except Exception as exc:
                        self._send_json(
                            {"error": f"读取配置失败：{exc}"}, status=500
                        )
                        return
                    self._send(payload, "application/json; charset=utf-8")
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                path = self.path.split("?", 1)[0]
                if path != "/api/config" or dashboard.config_store is None:
                    self.send_error(404)
                    return
                origin = self.headers.get("Origin")
                allowed_origins = {
                    dashboard.url,
                    f"http://localhost:{dashboard.port}",
                }
                if origin and origin not in allowed_origins:
                    self._send_json({"error": "不允许的请求来源"}, status=403)
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length <= 0 or length > 1_500_000:
                    self._send_json({"error": "配置请求大小无效"}, status=413)
                    return
                try:
                    value = json.loads(self.rfile.read(length))
                    if not isinstance(value, Mapping):
                        raise ValueError("配置必须是 JSON object")
                    saved = dashboard.config_store.save(value)
                except ConfigBusyError as exc:
                    self._send_json({"error": str(exc)}, status=409)
                    return
                except ConfigConflictError as exc:
                    self._send_json({"error": str(exc)}, status=409)
                    return
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except Exception as exc:
                    self._send_json({"error": f"保存配置失败：{exc}"}, status=500)
                    return
                self._send_json(saved)

            def _send_json(self, value: Mapping[str, Any], status: int = 200) -> None:
                self._send(
                    json.dumps(
                        value, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8"),
                    "application/json; charset=utf-8",
                    status=status,
                )

            def _send(
                self,
                payload: bytes,
                content_type: str,
                *,
                status: int = 200,
                cache_control: str = "no-store",
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", cache_control)
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
            "workflow": runtime.get("workflow", {}),
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
