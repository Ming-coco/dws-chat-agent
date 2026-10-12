from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..core import MAX_IMAGES, MAX_IMAGE_BYTES, sanitize_text
from ..prompts import (
    STAGE_PROMPT_VARIABLES,
    SUPPLEMENT_PROMPT_VARIABLES,
    render_prompt as _render_text,
    validate_template_variables,
)
from . import claude, codex, custom, pi


_STAGE_ORDER = ("front", "worker")
# 事件流单行上限必须容纳"允许下载的整批图片原样回传"：pi RPC 会把带图用户
# 消息（含 base64 ImageContent）在 message/agent 事件里原样发回，单行最大为
# 全部图片的 base64 膨胀（4/3）+ JSON 结构与文本余量。上限从 core 的图片
# 预算推导，与 service 下载侧保持一致，不得各自硬编码或互相砍对方的上限。
_EVENT_STREAM_LIMIT = (
    (MAX_IMAGES * MAX_IMAGE_BYTES * 4 + 2) // 3 + 8 * 1024 * 1024
)
# pi-worker 冷启动（含扩展/MCP 加载）偶发超过默认 15s，首条 prompt 握手放宽到 3 分钟。
_PI_START_HANDSHAKE_TIMEOUT_SECONDS = 180.0
_DRIVER_PROTOCOLS = {
    "codex": "codex-app-server",
    "claude": "claude-stream-json",
    "pi": "pi-rpc",
}
_SUPPORTED_PROTOCOLS = frozenset(
    (*_DRIVER_PROTOCOLS.values(), "custom-jsonl-v1")
)
SUPPORTED_PROTOCOLS = tuple(sorted(_SUPPORTED_PROTOCOLS))
# pi RPC prompt/steer 命令的 images 字段使用 ImageContent：
# {"type": "image", "data": "<base64>", "mimeType": "..."}（pi docs/rpc.md）。
# service 层按魔数把图片落盘为正确后缀，这里按后缀映射 MIME。
_PI_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _pi_image_payloads(
    paths: Sequence[Path],
) -> tuple[dict[str, str], ...]:
    """把本地图片文件编码成 pi RPC 的 ImageContent 列表。

    不认识的后缀直接报错，而不是发一个 pi 端无法解析的 mimeType。
    """
    payloads: list[dict[str, str]] = []
    for path in paths:
        mime = _PI_IMAGE_MIME_TYPES.get(path.suffix.lower())
        if mime is None:
            raise ValueError(f"unsupported pi image format: {path.suffix}")
        payloads.append(
            {
                "type": "image",
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                "mimeType": mime,
            }
        )
    return tuple(payloads)


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
    # 执行失败时按顺序切换的备用 agent 名（仅对 worker 阶段生效）
    fallbacks: tuple[str, ...] = ()

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


@dataclass(frozen=True)
class PreparedAgent:
    stage: WorkflowStage
    protocol: str
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    prompt: str
    thread_options: Mapping[str, Any]
    result_path: Path
    events_path: Path
    stderr_path: Path
    prompt_path: Path
    local_image_paths: tuple[Path, ...] = ()
    # pi RPC 专用：随首条 prompt 原生发送的 ImageContent（base64 + mimeType）
    pi_images: tuple[dict[str, str], ...] = ()

    @property
    def timeout_seconds(self) -> float:
        return self.stage.profile.timeout_seconds

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
        self._updates: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._closing = False
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[Mapping[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._final_text = ""
        self._escalated = False
        self._progress: list[str] = []
        self._stream_text = ""
        self._codex_item_phases: dict[str, str] = {}
        self._codex_reasoning: dict[str, str] = {}
        self._pi_thinking: dict[int, str] = {}

    @property
    def exit_code(self) -> int | None:
        return self.process.returncode if self.process else None

    @property
    def done(self) -> bool:
        return self._complete.is_set()

    @property
    def latest_progress(self) -> str:
        return self._progress[-1] if self._progress else ""

    @property
    def latest_stream_text(self) -> str:
        return self._stream_text

    @property
    def escalated(self) -> bool:
        return self._escalated

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
                        "summary": str(
                            self.prepared.stage.profile.options.get(
                                "reasoning_summary", "concise"
                            )
                        ),
                    },
                )
                self.turn_id = str(
                    turn.get("result", {}).get("turn", {}).get("id") or ""
                )
                if not self.turn_id:
                    raise RuntimeError("Codex app-server did not return a turn id")
            elif self.prepared.protocol == "pi-rpc":
                response = await self._pi_call(
                    self._pi_prompt_command(),
                    timeout=_PI_START_HANDSHAKE_TIMEOUT_SECONDS,
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

    async def wait_for_update(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._updates.get()
        else:
            await asyncio.wait_for(self._updates.get(), timeout=timeout)

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

    def result(self) -> str:
        if self._escalated:
            return ""
        # 只把真正的最终 assistant 消息当回复；thinking/progress 流文本不参与回退，
        # 避免把内部独白当正式回复发给联系人。无终稿时抛错交由服务层走 fallback 链。
        reply = self._final_text.strip()
        if not reply:
            raise ValueError("agent produced no reply or escalation")
        return reply

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

    def _pi_prompt_command(self) -> dict[str, Any]:
        """构造 pi RPC 首条 prompt 命令；有图片时以 ImageContent 原生附带。"""
        command: dict[str, Any] = {
            "type": "prompt",
            "message": self.prepared.prompt,
        }
        if self.prepared.pi_images:
            command["images"] = list(self.prepared.pi_images)
        return command

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
            if method == "item/started":
                item = params.get("item")
                if isinstance(item, Mapping):
                    item_id = str(item.get("id") or "")
                    if item.get("type") == "agentMessage":
                        self._codex_item_phases[item_id] = str(
                            item.get("phase") or ""
                        )
                    elif _is_escalate_tool(
                        item.get("tool")
                        or item.get("toolName")
                        or item.get("name")
                    ):
                        self._capture_escalation()
            elif method == "item/reasoning/summaryTextDelta":
                item_id = str(params.get("itemId") or "")
                value = self._codex_reasoning.get(item_id, "") + str(
                    params.get("delta") or ""
                )
                self._codex_reasoning[item_id] = value
                self._capture_progress(value)
            elif method == "item/completed":
                item = params.get("item")
                if isinstance(item, Mapping):
                    if item.get("type") == "agentMessage":
                        phase = str(item.get("phase") or "")
                        text = str(item.get("text") or "")
                        if phase == "final_answer":
                            self._capture_body(text, final=True)
                    elif item.get("type") == "reasoning":
                        self._capture_progress(_codex_reasoning_text(item))
                    elif _is_escalate_tool(
                        item.get("tool")
                        or item.get("toolName")
                        or item.get("name")
                    ):
                        self._capture_escalation()
            elif method == "turn/completed":
                turn = params.get("turn")
                if isinstance(turn, Mapping) and (
                    not self.turn_id or str(turn.get("id") or "") == self.turn_id
                ):
                    if str(turn.get("status") or "") == "completed":
                        self.error = ""
                    else:
                        self.error = f"codex_turn_{turn.get('status') or 'failed'}"
                    self._finish()
            elif method == "error":
                if not bool(params.get("willRetry")):
                    self.error = sanitize_text(
                        str(params.get("message") or params), 500
                    )
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
            elif event_type == "message_update":
                update = event.get("assistantMessageEvent")
                update = update if isinstance(update, Mapping) else {}
                update_type = str(update.get("type") or "")
                if update_type == "text_delta":
                    self._capture_stream_delta(str(update.get("delta") or ""))
                elif update_type == "thinking_delta":
                    index = int(update.get("contentIndex") or 0)
                    value = self._pi_thinking.get(index, "") + str(
                        update.get("delta") or ""
                    )
                    self._pi_thinking[index] = value
                    self._capture_progress(value)
                elif update_type == "thinking_end":
                    self._capture_progress(str(update.get("content") or ""))
            elif event_type == "tool_execution_start" and _is_escalate_tool(
                event.get("toolName")
            ):
                self._capture_escalation()
            elif event_type == "message_end":
                text = _assistant_text(event)
                if text and _is_pi_intermediate_message(event.get("message")):
                    # 工具调用间隔中的中间陈述（带 toolCall 或 error/aborted 终止）
                    # 只走进度展示，不能作为最终回复交付。
                    self._capture_progress(text)
                elif text:
                    self._capture_body(text, final=True)
            elif event_type == "agent_end":
                messages = event.get("messages")
                if isinstance(messages, list):
                    reply = _pi_final_reply_text(messages)
                    if reply:
                        self._capture_body(reply, final=True)
                    else:
                        # 本轮没有可交付的终稿：清掉 message_end 期间捕获的
                        # 候选文本，宁可空回复走 fallback 链，也不能把
                        # 中间陈述当正式回复发出。
                        self._final_text = ""
                if (
                    not self._escalated
                    and not self._final_text
                    and event.get("willRetry")
                ):
                    # pi 会在可重试错误后自行续跑（agent_end 之后还有后续 turn）。
                    # 保持会话打开等待下一次 agent_end；不再自行重发 prompt，
                    # 避免与 pi 的重试或 steer 后续 turn 冲突
                    # （Agent is already processing 会作废整场 run）。
                    return
                self._finish()
            elif event_type == "response" and not event.get("success", True):
                error = sanitize_text(str(event.get("error") or "Pi RPC error"), 500)
                if "already processing" in error.lower():
                    # pi 正在处理（或即将开始）后续 turn 时会拒绝新 prompt，
                    # 此时会话本身健康；作废整场会连正在处理的补充消息一起丢，
                    # 这里只忽略该次被拒请求，等 agent_end 或服务层超时兜底。
                    return
                self._fail(error)
            return
        if protocol == "custom-jsonl-v1":
            event_type = str(event.get("type") or "")
            if event_type in {"progress", "thinking"}:
                self._capture_progress(str(event.get("text") or ""))
            elif event_type == "assistant":
                self._capture_body(_assistant_text(event))
            elif event_type == "tool_call" and _is_escalate_tool(
                event.get("name") or event.get("toolName")
            ):
                self._capture_escalation()
            elif event_type == "result":
                result = event.get("result", event.get("value"))
                if isinstance(result, str):
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
            self._signal_update()
            return
        event_type = str(event.get("type") or "")
        if event_type == "assistant":
            for tool_name in _assistant_tool_names(event):
                if _is_escalate_tool(tool_name):
                    self._capture_escalation()
            thinking = _assistant_thinking(event)
            if thinking:
                self._capture_progress(thinking)
            self._capture_body(_assistant_text(event))
        elif event_type == "result":
            result = event.get("result")
            if isinstance(result, str):
                self._final_text = result
            if event.get("is_error"):
                self.error = sanitize_text(str(event.get("result") or "Claude error"), 500)
            self._finish()

    def _capture_body(self, text: str, *, final: bool = False) -> None:
        clean = text.strip()
        if not clean:
            return
        if final:
            self._final_text = clean
            return
        self._stream_text = clean
        self._signal_update()

    def _capture_progress(self, text: str) -> None:
        progress = sanitize_text(text, 500).strip()
        if progress and (not self._progress or self._progress[-1] != progress):
            self._progress.append(progress)
            self._signal_update()

    def _capture_escalation(self) -> None:
        self._escalated = True
        self._complete.set()
        self._signal_update()

    def _capture_stream_delta(self, delta: str) -> None:
        if not delta:
            return
        self._stream_text += delta
        self._signal_update()

    def _signal_update(self) -> None:
        if self._updates.empty():
            self._updates.put_nowait(None)

    def _finish(self) -> None:
        # result.txt 仅作排障留档：无终稿时保留流文本便于回溯，但不作为交付。
        payload = self._final_text or self._stream_text
        if payload:
            self.prepared.result_path.write_text(payload, encoding="utf-8")
        if not self._final_text and not self.error and not self._escalated:
            self.error = "agent_result_missing"
        self._complete.set()
        self._signal_update()

    def _fail(self, error: str) -> None:
        self.error = error
        failure = RuntimeError(error)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(failure)
        self._complete.set()
        self._signal_update()


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
            if timeout < 0:
                raise ValueError(f"agent timeout must be non-negative: {name}")
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
            fallbacks_raw = value.get("fallbacks", [])
            if not isinstance(fallbacks_raw, list) or any(
                not isinstance(item, str) or not item.strip()
                for item in fallbacks_raw
            ):
                raise ValueError(
                    f"agent fallbacks must be an array of agent names: {name}"
                )
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
                fallbacks=tuple(str(item).strip() for item in fallbacks_raw),
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
                if stage_name == "front":
                    continue
                raise ValueError(f"workflow {active} requires stage: {stage_name}")
            profile_name = str(value.get("agent") or "")
            profile = profiles.get(profile_name)
            if profile is None:
                raise ValueError(f"workflow stage references unknown agent: {profile_name}")
            prompt_path = _resolve_path(config_dir, value.get("prompt"))
            if not prompt_path.is_file():
                raise ValueError(f"workflow prompt does not exist: {prompt_path}")
            stages[stage_name] = WorkflowStage(
                name=stage_name,
                label=str(
                    value.get("label")
                    or ("前置模型" if stage_name == "front" else "后置模型")
                ),
                profile=profile,
                prompt_path=prompt_path,
            )
        if "front" in stages and not stages["front"].profile.read_only:
            raise ValueError("workflow front agent must set read_only=true")
        if stages["worker"].profile.read_only:
            raise ValueError("workflow worker agent must set read_only=false")
        for profile in profiles.values():
            unknown = [
                fallback
                for fallback in profile.fallbacks
                if fallback not in profiles
            ]
            if unknown:
                raise ValueError(
                    f"agent {profile.name} references unknown fallback agents: "
                    f"{', '.join(unknown)}"
                )
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
        model_override: str | None = None,
        agent_override: str | None = None,
    ) -> PreparedAgent:
        stage = self.resolve_stage(stage_name, agent_override)
        session_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = session_dir / "prompt.md"
        result_path = session_dir / "result.txt"
        events_path = session_dir / "events.jsonl"
        stderr_path = session_dir / "stderr.log"
        prompt_path.write_text(prompt, encoding="utf-8")
        protocol = stage.profile.protocol
        pi_images: tuple[dict[str, str], ...] = ()
        if local_images and protocol == "pi-rpc":
            # pi RPC 原生支持 prompt.images（ImageContent：base64 + mimeType），
            # 在此一次性编码，首条 prompt 由 AgentSession 直接携带图片。
            pi_images = _pi_image_payloads(local_images)
            local_images = ()
        elif local_images and protocol != "codex-app-server":
            # 其余协议不支持原生图片输入，应在上游 _run_agent 中将图片路径
            # 嵌入 prompt；这里只保留防御性日志，不再硬阻断。
            logging.getLogger(__name__).warning(
                "local_images passed to non-codex protocol %s; images=%d",
                protocol,
                len(local_images),
            )
        mcp_config_path = (
            _write_escalate_mcp_config(session_dir) if stage_name == "front" else None
        )
        argv = _command(stage.profile, mcp_config_path, model_override)
        environment = self._environment(
            stage.profile,
            session_dir / "codex-home"
            if stage_name == "front" and protocol == "codex-app-server"
            else None,
        )
        if mcp_config_path is not None:
            environment["DWS_ESCALATE_MCP_CONFIG"] = str(mcp_config_path)
            environment["DWS_ESCALATE_PYTHON"] = sys.executable
            environment["DWS_ESCALATE_MCP_SCRIPT"] = str(
                Path(__file__).with_name("escalate_mcp.py").resolve()
            )
        return PreparedAgent(
            stage=stage,
            protocol=protocol,
            argv=tuple(argv),
            cwd=self._workspace_root,
            environment=environment,
            prompt=prompt,
            thread_options=_thread_options(stage.profile, self._workspace_root),
            result_path=result_path,
            events_path=events_path,
            stderr_path=stderr_path,
            prompt_path=prompt_path,
            local_image_paths=tuple(local_images),
            pi_images=pi_images,
        )

    def open_session(
        self,
        stage_name: str,
        session_id: str,
        prompt: str,
        session_dir: Path,
        local_images: Sequence[Path] = (),
        model_override: str | None = None,
        agent_override: str | None = None,
    ) -> AgentSession:
        return AgentSession(
            self.prepare(
                stage_name,
                session_id,
                prompt,
                session_dir,
                local_images,
                model_override=model_override,
                agent_override=agent_override,
            ),
            session_id,
        )

    def profile_name(self, stage_name: str) -> str:
        return self._stage(stage_name).profile.name

    def has_stage(self, stage_name: str) -> bool:
        return stage_name in self._stages

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
                "progressEnabled": self.progress_enabled,
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
                if name in self._stages
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
                    "fallbacks": list(profile.fallbacks),
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

    def resolve_stage(
        self, name: str, agent_override: str | None = None
    ) -> WorkflowStage:
        """返回 stage 定义；agent_override 用指定 agent 替换该阶段的执行 profile。"""
        stage = self._stage(name)
        if not agent_override or agent_override == stage.profile.name:
            return stage
        profile = self._profiles.get(agent_override)
        if profile is None:
            raise ValueError(f"unknown agent override: {agent_override}")
        return WorkflowStage(
            name=stage.name,
            label=stage.label,
            profile=profile,
            prompt_path=stage.prompt_path,
        )

    def _environment(
        self, profile: AgentProfile, session_home: Path | None = None
    ) -> dict[str, str]:
        environment = {**os.environ, **profile.environment, "PYTHONUNBUFFERED": "1"}
        if profile.protocol != "codex-app-server":
            return environment
        home_value: Any = session_home or profile.options.get("isolated_home")
        if not home_value:
            return environment
        home = Path(str(home_value)).expanduser()
        if not home.is_absolute():
            home = self._config_dir / home
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        auth_value = profile.options.get("auth_file")
        auth_source = (
            Path(str(auth_value)).expanduser().resolve()
            if auth_value
            else Path(environment.get("CODEX_HOME") or Path.home() / ".codex")
            / "auth.json"
        )
        auth_target = home / "auth.json"
        if (
            auth_source.is_file()
            and not auth_target.exists()
            and not auth_target.is_symlink()
        ):
            auth_target.symlink_to(auth_source)
        environment["CODEX_HOME"] = str(home.resolve())
        return environment


def _resolve_path(base: Path, value: Any) -> Path:
    if not value:
        raise ValueError("workflow path must not be empty")
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _command(profile: AgentProfile, mcp_config_path: Path | None, model_override: str | None = None) -> list[str]:
    protocol = profile.protocol
    if protocol == "codex-app-server":
        return codex.command(profile, bool(mcp_config_path))
    if protocol == "claude-stream-json":
        return claude.command(profile, mcp_config_path)
    if protocol == "custom-jsonl-v1":
        return custom.command(profile)
    if protocol == "pi-rpc":
        return pi.command(profile, bool(mcp_config_path), model_override=model_override)
    raise ValueError(f"unsupported agent protocol: {protocol}")


def _write_escalate_mcp_config(session_dir: Path) -> Path:
    path = session_dir / "mcp.json"
    script = Path(__file__).with_name("escalate_mcp.py").resolve()
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "dws-escalate": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(script)],
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


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


def _is_pi_intermediate_message(message: Any) -> bool:
    """判断 pi 的 assistant 消息是否为工具调用间隔中的中间陈述或错误/中断消息。

    pi 助手消息的 content 块类型为 text/thinking/toolCall；带 toolCall 块
    （stopReason=toolUse）说明模型还要继续执行工具，以 error/aborted 终止的
    消息则不是正常收尾。这两类消息的文本都不能作为最终回复交付。
    """
    if not isinstance(message, Mapping):
        return False
    if str(message.get("stopReason") or "") in {"error", "aborted"}:
        return True
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, Mapping) and item.get("type") == "toolCall"
        for item in content
    )


def _pi_final_reply_text(messages: list[Any]) -> str:
    """从 pi agent_end.messages 提取可交付的终稿文本。

    只认可最后一条 assistant 消息：必须是无 toolCall 的纯文本且非
    error/aborted 终止，才算终稿。工具调用间隔中的中间陈述、错误消息
    一律返回空串，由上层记 agent_result_missing 走 fallback 链，
    避免把计划独白当正式回复发给联系人。
    """
    for message in reversed(messages):
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        if _is_pi_intermediate_message(message):
            return ""
        return _assistant_text({"message": message})
    return ""


def _assistant_thinking(event: Mapping[str, Any]) -> str:
    message = event.get("message")
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("thinking") or "")
        for item in content
        if isinstance(item, Mapping) and item.get("type") == "thinking"
    ).strip()


def _codex_reasoning_text(item: Mapping[str, Any]) -> str:
    summary = item.get("summary")
    if not isinstance(summary, list):
        return ""
    return "\n".join(
        str(part.get("text") or "")
        for part in summary
        if isinstance(part, Mapping)
    ).strip()


def _assistant_tool_names(event: Mapping[str, Any]) -> list[str]:
    message = event.get("message")
    if not isinstance(message, Mapping):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        str(item.get("name") or "")
        for item in content
        if isinstance(item, Mapping) and item.get("type") in {"tool_use", "toolUse"}
    ]


def _is_escalate_tool(value: Any) -> bool:
    name = str(value or "").lower()
    return name == "escalate" or name.endswith(
        ("__escalate", "/escalate", ":escalate")
    )
