from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..core import sanitize_text
from ..prompts import (
    STAGE_PROMPT_VARIABLES,
    SUPPLEMENT_PROMPT_VARIABLES,
    render_prompt as _render_text,
    validate_template_variables,
)
from . import claude, codex, custom, pi


_STAGE_ORDER = ("front", "worker")
_EVENT_STREAM_LIMIT = 8 * 1024 * 1024
_DRIVER_PROTOCOLS = {
    "codex": "codex-app-server",
    "claude": "claude-stream-json",
    "pi": "pi-rpc",
}
_SUPPORTED_PROTOCOLS = frozenset(
    (*_DRIVER_PROTOCOLS.values(), "custom-jsonl-v1")
)
SUPPORTED_PROTOCOLS = tuple(sorted(_SUPPORTED_PROTOCOLS))


@dataclass(frozen=True)
class AgentProfile:
    name: str
    driver: str
    protocol: str
    command: tuple[str, ...]
    model: str
    reasoning_effort: str
    read_only: bool
    timeout_seconds: float
    options: Mapping[str, Any]
    environment: Mapping[str, str]
    extra_args: tuple[str, ...]

    @property
    def binary(self) -> str:
        """The executable checked by doctor; retained for old callers."""
        return self.command[0]


@dataclass(frozen=True)
class WorkflowStage:
    name: str
    label: str
    profile: AgentProfile
    prompt_path: Path
    schema_path: Path


@dataclass(frozen=True)
class PreparedAgent:
    stage: WorkflowStage
    protocol: str
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    prompt: str
    output_schema: Mapping[str, Any]
    thread_options: Mapping[str, Any]
    result_path: Path
    events_path: Path
    stderr_path: Path
    prompt_path: Path
    local_image_paths: tuple[Path, ...] = ()

    @property
    def timeout_seconds(self) -> float:
        return self.stage.profile.timeout_seconds

    def read_decision(self) -> dict[str, Any]:
        if self.result_path.is_file():
            return _parse_json_text(self.result_path.read_text(encoding="utf-8"))
        events = _read_json_lines(self.events_path)
        for event in reversed(events):
            structured = event.get("structured_output")
            if isinstance(structured, Mapping):
                return dict(structured)
            result = event.get("result")
            if isinstance(result, Mapping):
                return dict(result)
            text = _event_assistant_text(event)
            if not text:
                continue
            try:
                return _parse_json_text(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        raise ValueError(f"{self.stage.profile.driver} produced no structured result")

    def latest_progress(self) -> str:
        latest = ""
        for event in _read_json_lines(self.events_path):
            text = _event_assistant_text(event)
            if not text or _is_final_decision(text):
                continue
            latest = sanitize_text(text, 500).strip()
        return latest


class AgentSession:
    """One live agent process with a provider-neutral steering surface."""

    def __init__(self, prepared: PreparedAgent, session_id: str) -> None:
        self.prepared = prepared
        self.session_id = session_id
        self.process: asyncio.subprocess.Process | None = None
        self.thread_id = ""
        self.turn_id = ""
        self.error = ""
        self._complete = asyncio.Event()
        self._closing = False
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[Mapping[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._final_text = ""
        self._structured_result: dict[str, Any] | None = None
        self._progress: list[str] = []

    @property
    def exit_code(self) -> int | None:
        return self.process.returncode if self.process else None

    @property
    def done(self) -> bool:
        return self._complete.is_set()

    @property
    def latest_progress(self) -> str:
        return self._progress[-1] if self._progress else ""

    async def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("agent session already started")
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        self.process = await asyncio.create_subprocess_exec(
            *self.prepared.argv,
            cwd=self.prepared.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_EVENT_STREAM_LIMIT,
            env=dict(self.prepared.environment),
            **kwargs,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        self._watch_task = asyncio.create_task(self._watch_process())
        try:
            if self.prepared.protocol == "codex-app-server":
                initialized = await self._jsonrpc_call(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "dws-chat-agent",
                            "title": "DWS Chat Agent",
                            "version": "0.1.0",
                        }
                    },
                )
                if "result" not in initialized:
                    raise RuntimeError("Codex app-server initialization failed")
                await self._write_json(
                    {"jsonrpc": "2.0", "method": "initialized", "params": {}}
                )
                started = await self._jsonrpc_call(
                    "thread/start", dict(self.prepared.thread_options)
                )
                self.thread_id = str(
                    started.get("result", {}).get("thread", {}).get("id") or ""
                )
                if not self.thread_id:
                    raise RuntimeError("Codex app-server did not return a thread id")
                inputs: list[dict[str, str]] = [
                    {"type": "text", "text": self.prepared.prompt}
                ]
                inputs.extend(
                    {"type": "localImage", "path": str(path.resolve())}
                    for path in self.prepared.local_image_paths
                )
                turn = await self._jsonrpc_call(
                    "turn/start",
                    {
                        "threadId": self.thread_id,
                        "input": inputs,
                        "effort": self.prepared.stage.profile.reasoning_effort or None,
                        "outputSchema": dict(self.prepared.output_schema),
                    },
                )
                self.turn_id = str(
                    turn.get("result", {}).get("turn", {}).get("id") or ""
                )
                if not self.turn_id:
                    raise RuntimeError("Codex app-server did not return a turn id")
            elif self.prepared.protocol == "pi-rpc":
                response = await self._pi_call(
                    {"type": "prompt", "message": self.prepared.prompt}
                )
                if not response.get("success", False):
                    raise RuntimeError(str(response.get("error") or "Pi rejected prompt"))
            elif self.prepared.protocol == "custom-jsonl-v1":
                response = await self._custom_call(
                    {
                        "type": "start",
                        "protocolVersion": 1,
                        "sessionId": self.session_id,
                        "stage": self.prepared.stage.name,
                        "cwd": str(self.prepared.cwd),
                        "prompt": self.prepared.prompt,
                        "outputSchema": dict(self.prepared.output_schema),
                        "readOnly": self.prepared.stage.profile.read_only,
                        "model": self.prepared.stage.profile.model,
                        "reasoningEffort": self.prepared.stage.profile.reasoning_effort,
                    }
                )
                if not response.get("success", False):
                    raise RuntimeError(
                        str(response.get("error") or "custom agent rejected start")
                    )
            elif self.prepared.protocol == "claude-stream-json":
                await self._write_json(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": self.prepared.prompt},
                    }
                )
            else:
                raise RuntimeError(f"unsupported agent protocol: {self.prepared.protocol}")
        except Exception:
            await self.close()
            raise

    async def wait(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._complete.wait()
        else:
            await asyncio.wait_for(self._complete.wait(), timeout=timeout)

    async def steer(self, prompt: str) -> bool:
        if not prompt.strip() or self.done or self.process is None:
            return False
        try:
            if self.prepared.protocol == "codex-app-server":
                if not self.thread_id or not self.turn_id:
                    return False
                response = await self._jsonrpc_call(
                    "turn/steer",
                    {
                        "threadId": self.thread_id,
                        "expectedTurnId": self.turn_id,
                        "input": [{"type": "text", "text": prompt}],
                    },
                )
                return bool(response.get("result", {}).get("turnId"))
            if self.prepared.protocol == "pi-rpc":
                response = await self._pi_call(
                    {
                        "type": "prompt",
                        "message": prompt,
                        "streamingBehavior": "steer",
                    }
                )
                return bool(response.get("success", False))
            if self.prepared.protocol == "custom-jsonl-v1":
                response = await self._custom_call(
                    {
                        "type": "steer",
                        "sessionId": self.session_id,
                        "message": prompt,
                    }
                )
                return bool(response.get("success", False))
            if self.prepared.protocol == "claude-stream-json":
                await self._write_json(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": prompt},
                    }
                )
                return True
            return False
        except (BrokenPipeError, ConnectionError, RuntimeError, TimeoutError):
            return False

    def decision(self) -> dict[str, Any]:
        if self._structured_result is not None:
            return dict(self._structured_result)
        if self._final_text:
            return _parse_json_text(self._final_text)
        return self.prepared.read_decision()

    async def abort(self) -> None:
        if self.process is None or self.process.returncode is not None:
            return
        with suppress(Exception):
            if self.prepared.protocol == "codex-app-server" and self.thread_id and self.turn_id:
                await self._jsonrpc_call(
                    "turn/interrupt",
                    {"threadId": self.thread_id, "turnId": self.turn_id},
                    timeout=2,
                )
            elif self.prepared.protocol == "pi-rpc":
                await self._pi_call({"type": "abort"}, timeout=2)
            elif self.prepared.protocol == "custom-jsonl-v1":
                await self._custom_call(
                    {"type": "abort", "sessionId": self.session_id}, timeout=2
                )
        await self.close()

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        process = self.process
        if process and process.returncode is None:
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                await _terminate_process_tree(process)
        tasks = [
            task
            for task in (self._reader_task, self._stderr_task, self._watch_task)
            if task is not None and task is not asyncio.current_task()
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    async def _jsonrpc_call(
        self, method: str, params: Mapping[str, Any], *, timeout: float = 15
    ) -> Mapping[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": _without_none(params),
            }
        )
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)
        if response.get("error"):
            raise RuntimeError(str(response["error"]))
        return response

    async def _pi_call(
        self, command: Mapping[str, Any], *, timeout: float = 15
    ) -> Mapping[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write_json({"id": request_id, **command})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def _custom_call(
        self, command: Mapping[str, Any], *, timeout: float = 15
    ) -> Mapping[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write_json({"id": request_id, **command})
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)
        if response.get("type") != "response":
            raise RuntimeError("custom agent response must use type=response")
        return response

    async def _write_json(self, value: Mapping[str, Any]) -> None:
        if not self.process or not self.process.stdin or self.process.returncode is not None:
            raise BrokenPipeError("agent process is not writable")
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            self.process.stdin.write((payload + "\n").encode("utf-8"))
            await self.process.stdin.drain()

    async def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        try:
            while line := await self.process.stdout.readline():
                self.prepared.events_path.parent.mkdir(parents=True, exist_ok=True)
                with self.prepared.events_path.open("ab") as stream:
                    stream.write(line)
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                request_id = event.get("id")
                if isinstance(request_id, int) and request_id in self._pending:
                    future = self._pending[request_id]
                    if not future.done():
                        future.set_result(event)
                    continue
                await self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                self._fail(f"event_stream_error: {exc}")

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        try:
            while line := await self.process.stderr.readline():
                self.prepared.stderr_path.parent.mkdir(parents=True, exist_ok=True)
                with self.prepared.stderr_path.open("ab") as stream:
                    stream.write(line)
        except asyncio.CancelledError:
            raise

    async def _watch_process(self) -> None:
        assert self.process
        code = await self.process.wait()
        if self._reader_task is not None and not self._reader_task.done():
            await self._reader_task
        if not self._closing and not self.done:
            self._fail(f"agent_exit_{code}")

    async def _handle_event(self, event: Mapping[str, Any]) -> None:
        protocol = self.prepared.protocol
        if protocol == "codex-app-server":
            method = str(event.get("method") or "")
            params = event.get("params")
            params = params if isinstance(params, Mapping) else {}
            if method == "item/completed":
                item = params.get("item")
                if isinstance(item, Mapping) and item.get("type") == "agentMessage":
                    self._capture_text(
                        str(item.get("text") or ""),
                        final=item.get("phase") == "final_answer",
                    )
            elif method == "turn/completed":
                turn = params.get("turn")
                if isinstance(turn, Mapping) and (
                    not self.turn_id or str(turn.get("id") or "") == self.turn_id
                ):
                    if str(turn.get("status") or "") != "completed":
                        self.error = f"codex_turn_{turn.get('status') or 'failed'}"
                    self._finish()
            elif method == "error":
                self.error = sanitize_text(str(params.get("message") or params), 500)
            return
        if protocol == "pi-rpc":
            event_type = str(event.get("type") or "")
            if event_type == "extension_ui_request":
                if event.get("method") in {"select", "confirm", "input", "editor"}:
                    await self._write_json(
                        {
                            "type": "extension_ui_response",
                            "id": event.get("id"),
                            "cancelled": True,
                        }
                    )
            elif event_type == "message_end":
                self._capture_text(_assistant_text(event))
            elif event_type == "agent_end":
                self._finish()
            elif event_type == "response" and not event.get("success", True):
                self.error = sanitize_text(str(event.get("error") or "Pi RPC error"), 500)
            return
        if protocol == "custom-jsonl-v1":
            event_type = str(event.get("type") or "")
            if event_type == "progress":
                self._capture_text(str(event.get("text") or ""))
            elif event_type == "assistant":
                self._capture_text(_assistant_text(event))
            elif event_type == "result":
                result = event.get("result", event.get("value"))
                if isinstance(result, Mapping):
                    self._structured_result = dict(result)
                elif isinstance(result, str):
                    self._final_text = result
                else:
                    self.error = "custom_agent_result_missing"
                self._finish()
            elif event_type == "error":
                self._fail(
                    sanitize_text(
                        str(event.get("message") or "custom agent error"), 500
                    )
                )
            return
        if protocol != "claude-stream-json":
            self.error = f"unsupported_agent_protocol: {protocol}"
            self._complete.set()
            return
        event_type = str(event.get("type") or "")
        if event_type == "assistant":
            self._capture_text(_assistant_text(event))
        elif event_type == "result":
            structured = event.get("structured_output")
            if isinstance(structured, Mapping):
                self._structured_result = dict(structured)
            else:
                result = event.get("result")
                if isinstance(result, Mapping):
                    self._structured_result = dict(result)
                elif isinstance(result, str):
                    self._final_text = result
            if event.get("is_error"):
                self.error = sanitize_text(str(event.get("result") or "Claude error"), 500)
            self._finish()

    def _capture_text(self, text: str, *, final: bool = False) -> None:
        clean = text.strip()
        if not clean:
            return
        if final or _is_final_decision(clean):
            self._final_text = clean
            return
        progress = sanitize_text(clean, 500).strip()
        if progress and (not self._progress or self._progress[-1] != progress):
            self._progress.append(progress)

    def _finish(self) -> None:
        if self._structured_result is not None:
            payload = json.dumps(self._structured_result, ensure_ascii=False)
        else:
            payload = self._final_text
        if payload:
            self.prepared.result_path.write_text(payload, encoding="utf-8")
        elif not self.error:
            self.error = "agent_result_missing"
        self._complete.set()

    def _fail(self, error: str) -> None:
        self.error = error
        failure = RuntimeError(error)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(failure)
        self._complete.set()


class AgentRuntime:
    """Loads one two-stage workflow and hides provider-specific protocols."""

    def __init__(
        self,
        name: str,
        stages: Mapping[str, WorkflowStage],
        profiles: Mapping[str, AgentProfile],
        config_dir: Path,
        workspace_root: Path,
        supplement_strategy: str,
        supplement_prompt_path: Path,
        auto_messages: Mapping[str, Any],
    ) -> None:
        self.name = name
        self._stages = dict(stages)
        self._profiles = dict(profiles)
        self._config_dir = config_dir
        self._workspace_root = workspace_root
        self.supplement_strategy = supplement_strategy
        self._supplement_prompt_path = supplement_prompt_path
        self._auto_messages = dict(auto_messages)
        self.progress_enabled = bool(auto_messages.get("progress_enabled", True))
        self.progress_interval_seconds = float(
            auto_messages.get("progress_interval_seconds", 180)
        )
        self.max_progress_updates = int(auto_messages.get("max_progress_updates", 60))

    @classmethod
    def from_config(
        cls,
        raw: Mapping[str, Any],
        config_path: Path,
        workspace_root: Path,
    ) -> AgentRuntime:
        profile_values = raw.get("agents")
        workflow_values = raw.get("workflows")
        if not isinstance(profile_values, Mapping) or not isinstance(workflow_values, Mapping):
            raise ValueError("agents and workflows configuration are required")
        profiles: dict[str, AgentProfile] = {}
        for name, value in profile_values.items():
            if not isinstance(value, Mapping):
                raise ValueError(f"agent profile must be an object: {name}")
            driver = str(value.get("driver") or "").lower().replace("_", "-")
            if driver == "claude-code":
                driver = "claude"
            protocol = str(value.get("protocol") or "").lower().replace("_", "-")
            if not protocol:
                protocol = _DRIVER_PROTOCOLS.get(driver, "")
            if protocol not in _SUPPORTED_PROTOCOLS:
                raise ValueError(
                    f"unsupported agent protocol: {protocol or driver or name}"
                )
            if not driver:
                driver = next(
                    (
                        candidate
                        for candidate, candidate_protocol in _DRIVER_PROTOCOLS.items()
                        if candidate_protocol == protocol
                    ),
                    "custom",
                )
            timeout = float(value.get("timeout_seconds", 60))
            if timeout <= 0:
                raise ValueError(f"agent timeout must be positive: {name}")
            environment = value.get("environment", {})
            options = value.get("options", {})
            extra_args = value.get("extra_args", [])
            if not isinstance(environment, Mapping) or not isinstance(options, Mapping):
                raise ValueError(f"agent environment/options must be objects: {name}")
            if not isinstance(extra_args, list):
                raise ValueError(f"agent extra_args must be an array: {name}")
            command_value = value.get("command")
            if command_value is None:
                command = (str(value.get("binary") or driver),)
            else:
                if not isinstance(command_value, list) or not command_value:
                    raise ValueError(f"agent command must be a non-empty array: {name}")
                if any(
                    not isinstance(item, str) or not item.strip()
                    for item in command_value
                ):
                    raise ValueError(
                        f"agent command entries must be non-empty strings: {name}"
                    )
                command = tuple(command_value)
            profiles[str(name)] = AgentProfile(
                name=str(name),
                driver=driver,
                protocol=protocol,
                command=command,
                model=str(value.get("model") or ""),
                reasoning_effort=str(value.get("reasoning_effort") or ""),
                read_only=bool(value.get("read_only", False)),
                timeout_seconds=timeout,
                options=dict(options),
                environment={str(key): str(item) for key, item in environment.items()},
                extra_args=tuple(str(item) for item in extra_args),
            )
        active = str(workflow_values.get("active") or "")
        presets = workflow_values.get("presets")
        preset = presets.get(active) if isinstance(presets, Mapping) else None
        if not active or not isinstance(preset, Mapping):
            raise ValueError(f"active workflow preset does not exist: {active or '(empty)'}")
        supplement_strategy = str(preset.get("supplement_strategy") or "steer")
        if supplement_strategy not in {"steer", "restart_with_context"}:
            raise ValueError(f"unsupported supplement strategy: {supplement_strategy}")
        auto_messages = preset.get("auto_messages", {})
        if not isinstance(auto_messages, Mapping):
            raise ValueError("workflow auto_messages must be an object")
        if float(auto_messages.get("progress_interval_seconds", 180)) < 0:
            raise ValueError("progress interval must not be negative")
        if int(auto_messages.get("max_progress_updates", 60)) < 0:
            raise ValueError("max progress updates must not be negative")
        config_dir = config_path.resolve().parent
        supplement_prompt_path = _resolve_path(
            config_dir, preset.get("supplement_prompt") or "prompts/supplement.md"
        )
        if not supplement_prompt_path.is_file():
            raise ValueError(
                f"workflow supplement prompt does not exist: {supplement_prompt_path}"
            )
        stages: dict[str, WorkflowStage] = {}
        for stage_name in _STAGE_ORDER:
            value = preset.get(stage_name)
            if not isinstance(value, Mapping):
                raise ValueError(f"workflow {active} requires stage: {stage_name}")
            profile_name = str(value.get("agent") or "")
            profile = profiles.get(profile_name)
            if profile is None:
                raise ValueError(f"workflow stage references unknown agent: {profile_name}")
            prompt_path = _resolve_path(config_dir, value.get("prompt"))
            schema_path = _resolve_path(config_dir, value.get("schema"))
            if not prompt_path.is_file():
                raise ValueError(f"workflow prompt does not exist: {prompt_path}")
            if not schema_path.is_file():
                raise ValueError(f"workflow schema does not exist: {schema_path}")
            stages[stage_name] = WorkflowStage(
                name=stage_name,
                label=str(
                    value.get("label")
                    or ("前置模型" if stage_name == "front" else "后置模型")
                ),
                profile=profile,
                prompt_path=prompt_path,
                schema_path=schema_path,
            )
        if not stages["front"].profile.read_only:
            raise ValueError("workflow front agent must set read_only=true")
        if stages["worker"].profile.read_only:
            raise ValueError("workflow worker agent must set read_only=false")
        return cls(
            active,
            stages,
            profiles,
            config_dir,
            workspace_root.resolve(),
            supplement_strategy,
            supplement_prompt_path,
            auto_messages,
        )

    def render(self, stage_name: str, variables: Mapping[str, Any]) -> str:
        template = self._stage(stage_name).prompt_path.read_text(encoding="utf-8")
        return _render_text(template, variables)

    def render_supplement(self, variables: Mapping[str, Any]) -> str:
        return _render_text(
            self._supplement_prompt_path.read_text(encoding="utf-8"), variables
        )

    def render_auto_message(self, name: str, variables: Mapping[str, Any]) -> str:
        if name == "ack" and not bool(self._auto_messages.get("ack_enabled", True)):
            return ""
        return _render_text(str(self._auto_messages.get(name) or ""), variables)

    @property
    def ack_message(self) -> str:
        if not bool(self._auto_messages.get("ack_enabled", True)):
            return ""
        return str(self._auto_messages.get("ack") or "")

    def prepare(
        self,
        stage_name: str,
        session_id: str,
        prompt: str,
        session_dir: Path,
        local_images: Sequence[Path] = (),
    ) -> PreparedAgent:
        stage = self._stage(stage_name)
        session_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = session_dir / "prompt.md"
        result_path = session_dir / "result.json"
        events_path = session_dir / "events.jsonl"
        stderr_path = session_dir / "stderr.log"
        schema = json.loads(stage.schema_path.read_text(encoding="utf-8"))
        if not isinstance(schema, Mapping):
            raise ValueError(f"workflow schema must be an object: {stage.schema_path}")
        input_prompt = prompt
        if stage.profile.protocol == "pi-rpc":
            input_prompt += (
                "\n\n<output_schema>\n"
                + json.dumps(schema, ensure_ascii=False)
                + "\n</output_schema>\n"
            )
        prompt_path.write_text(input_prompt, encoding="utf-8")
        protocol = stage.profile.protocol
        if local_images and protocol != "codex-app-server":
            raise ValueError("local images require the Codex app-server protocol")
        argv = _command(stage.profile, schema)
        return PreparedAgent(
            stage=stage,
            protocol=protocol,
            argv=tuple(argv),
            cwd=self._workspace_root,
            environment=self._environment(stage.profile),
            prompt=input_prompt,
            output_schema=dict(schema),
            thread_options=_thread_options(stage.profile, self._workspace_root),
            result_path=result_path,
            events_path=events_path,
            stderr_path=stderr_path,
            prompt_path=prompt_path,
            local_image_paths=tuple(local_images),
        )

    def open_session(
        self,
        stage_name: str,
        session_id: str,
        prompt: str,
        session_dir: Path,
        local_images: Sequence[Path] = (),
    ) -> AgentSession:
        return AgentSession(
            self.prepare(stage_name, session_id, prompt, session_dir, local_images),
            session_id,
        )

    def profile_name(self, stage_name: str) -> str:
        return self._stage(stage_name).profile.name

    def required_binaries(self) -> dict[str, str]:
        return {
            stage.profile.name: stage.profile.command[0]
            for stage in self._stages.values()
        }

    def describe(self) -> dict[str, Any]:
        active_profiles = {stage.profile.name for stage in self._stages.values()}
        return {
            "name": self.name,
            "supplementStrategy": self.supplement_strategy,
            "supplementPromptPath": os.path.relpath(
                self._supplement_prompt_path, self._config_dir
            ),
            "supplementPrompt": self._supplement_prompt_path.read_text(encoding="utf-8"),
            "autoMessages": {
                "ack": self.ack_message,
                "progress": str(self._auto_messages.get("progress") or "{{progress}}"),
                "progressEnabled": self.progress_enabled,
                "progressIntervalSeconds": self.progress_interval_seconds,
                "maxProgressUpdates": self.max_progress_updates,
            },
            "stages": [
                {
                    "id": name,
                    "label": self._stages[name].label,
                    "agent": self._stages[name].profile.name,
                    "driver": self._stages[name].profile.driver,
                    "protocol": self._stages[name].profile.protocol,
                    "launchCommand": list(self._stages[name].profile.command),
                    "nativeSteer": True,
                    "model": self._stages[name].profile.model,
                    "reasoningEffort": self._stages[name].profile.reasoning_effort,
                    "readOnly": self._stages[name].profile.read_only,
                    "promptPath": os.path.relpath(
                        self._stages[name].prompt_path, self._config_dir
                    ),
                    "prompt": self._stages[name].prompt_path.read_text(encoding="utf-8"),
                }
                for name in _STAGE_ORDER
            ],
            "availableAgents": [
                {
                    "name": profile.name,
                    "driver": profile.driver,
                    "protocol": profile.protocol,
                    "launchCommand": list(profile.command),
                    "model": profile.model,
                    "reasoningEffort": profile.reasoning_effort,
                    "readOnly": profile.read_only,
                    "active": profile.name in active_profiles,
                }
                for profile in self._profiles.values()
            ],
        }

    def _stage(self, name: str) -> WorkflowStage:
        try:
            return self._stages[name]
        except KeyError as exc:
            raise ValueError(f"unknown workflow stage: {name}") from exc

    def _environment(self, profile: AgentProfile) -> dict[str, str]:
        environment = {**os.environ, **profile.environment, "PYTHONUNBUFFERED": "1"}
        if profile.protocol != "codex-app-server":
            return environment
        home_value = profile.options.get("isolated_home")
        auth_value = profile.options.get("auth_file")
        if not home_value or not auth_value:
            return environment
        home = Path(str(home_value)).expanduser()
        if not home.is_absolute():
            home = self._config_dir / home
        auth_source = Path(str(auth_value)).expanduser().resolve()
        if not auth_source.is_file():
            return environment
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        auth_target = home / "auth.json"
        if not auth_target.exists() and not auth_target.is_symlink():
            auth_target.symlink_to(auth_source)
        environment["CODEX_HOME"] = str(home.resolve())
        return environment


def _resolve_path(base: Path, value: Any) -> Path:
    if not value:
        raise ValueError("workflow path must not be empty")
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _command(profile: AgentProfile, schema: Mapping[str, Any]) -> list[str]:
    protocol = profile.protocol
    if protocol == "codex-app-server":
        return codex.command(profile)
    if protocol == "claude-stream-json":
        return claude.command(profile, schema)
    if protocol == "custom-jsonl-v1":
        return custom.command(profile)
    if protocol == "pi-rpc":
        return pi.command(profile)
    raise ValueError(f"unsupported agent protocol: {protocol}")


def _thread_options(profile: AgentProfile, workspace_root: Path) -> dict[str, Any]:
    if profile.protocol != "codex-app-server":
        return {}
    return codex.thread_options(profile, workspace_root)


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
            return
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


def _without_none(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _assistant_text(event: Mapping[str, Any]) -> str:
    message = event.get("message")
    if not isinstance(message, Mapping) or message.get("role") not in {None, "assistant"}:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, Mapping) and item.get("type") == "text"
    ).strip()


def _event_assistant_text(event: Mapping[str, Any]) -> str:
    if event.get("type") == "progress":
        return str(event.get("text") or "")
    method = str(event.get("method") or "")
    params = event.get("params")
    if method == "item/completed" and isinstance(params, Mapping):
        item = params.get("item")
        if isinstance(item, Mapping) and item.get("type") == "agentMessage":
            return str(item.get("text") or "")
    if event.get("type") == "item.completed":
        item = event.get("item")
        if isinstance(item, Mapping) and item.get("type") in {"agent_message", "agentMessage"}:
            return str(item.get("text") or "")
    return _assistant_text(event)


def _parse_json_text(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value)
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise TypeError("agent result must be a JSON object")
    return parsed


def _is_final_decision(text: str) -> bool:
    try:
        value = _parse_json_text(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return "action" in value
