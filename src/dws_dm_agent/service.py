#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import uuid
import webbrowser
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as fixed_timezone, tzinfo
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .runners.runtime import AgentRuntime
from .core import (
    AuditStore,
    ChangeEvidence,
    Contact,
    GateResult,
    HistoryMessage,
    IncomingEvent,
    SecurityGate,
    evidence_to_mapping,
    human_owns_conversation,
    normalize_decision,
    parse_dws_event,
    parse_local_datetime,
    render_reply,
    sanitize_text,
    sha256_text,
)
from .config import DEFAULT_CONFIG, DEFAULT_ENV, WEB_DIR, Settings, _load_timezone, load_settings
from .dashboard import AgentConfigStore, DashboardServer
from .delivery import _is_beneath, build_change_archive
from .dws import DwsClient, _find_dws_error_code


LOG = logging.getLogger("dws-chat-agent")


@dataclass
class AgentResult:
    session_id: str
    decision: dict[str, Any] | None
    exit_code: int | None
    started_at: datetime
    finished_at: datetime
    manual_takeover: bool = False
    supplements: list[IncomingEvent] | None = None
    consumed_supplements: list[IncomingEvent] | None = None
    error: str = ""
    workspace_drift: list[str] | None = None


def configure_logging(state_dir: Path, verbose: bool = False) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    file_handler = logging.FileHandler(state_dir / "agent.log", encoding="utf-8")
    handlers.append(file_handler)
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=handlers,
        force=True,
    )


class AgentService:
    def __init__(
        self,
        settings: Settings,
        mode_override: str | None = None,
        *,
        open_dashboard: bool = False,
    ) -> None:
        self.settings = settings
        self.mode = mode_override or settings.mode
        if self.mode not in {"shadow", "live"}:
            raise ValueError("mode must be shadow or live")
        self.agent_runtime = AgentRuntime.from_config(
            settings.raw, settings.config_path, settings.workspace_root
        )
        self.open_dashboard = open_dashboard
        self.store = AuditStore(settings.state_dir / "audit.sqlite3")
        security = settings.raw.get("security", {})
        self.gate = SecurityGate(
            max_chars=int(security.get("max_message_chars", 12000)),
            allowed_execution_domains=security.get("allowed_execution_domains", []),
            allowed_reference_domains=security.get("allowed_reference_domains", []),
        )
        self.dws = DwsClient(settings)
        self.queues: dict[str, asyncio.Queue[IncomingEvent]] = {}
        self.workers: dict[str, asyncio.Task[None]] = {}
        self.listeners: list[asyncio.Task[None]] = []
        self.runtime_heartbeat: asyncio.Task[None] | None = None
        self.listener_processes: set[asyncio.subprocess.Process] = set()
        self.stop_event = asyncio.Event()
        self.capacity = int(settings.raw.get("max_parallel_conversations", 2))
        self.global_slots = asyncio.Semaphore(self.capacity)
        self.runtime_path = settings.state_dir / "runtime.json"
        self.runtime_states: dict[str, dict[str, Any]] = {}
        # Ponytail ceiling: this lock only covers idle-check/config-swap/enqueue;
        # move config application onto the event loop if those operations grow.
        self.configuration_lock = threading.RLock()
        self.dashboard = DashboardServer(
            settings.state_dir / "audit.sqlite3",
            self.runtime_path,
            WEB_DIR / "dashboard.html",
            {item.user_id: item.display_name for item in settings.contacts},
            settings.timezone,
            config_store=AgentConfigStore(
                settings.config_path,
                settings.workspace_root,
                on_apply=self._apply_agent_configuration,
                is_idle=self._configuration_is_idle,
                mutation_lock=self.configuration_lock,
            ),
        )
        self.main_repo_roots = tuple(
            child.resolve()
            for child in settings.workspace_root.iterdir()
            if child.is_dir()
            and (child / ".git").exists()
            and not _is_beneath(child, settings.worktree_root)
        )

    def close(self) -> None:
        self.dashboard.stop()
        self.runtime_path.unlink(missing_ok=True)
        self.store.close()

    async def run(self) -> None:
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings.worktree_root.mkdir(parents=True, exist_ok=True)
        LOG.info("starting mode=%s contacts=%d", self.mode, len(self.settings.contacts))
        self._write_runtime()
        try:
            self.dashboard.start()
            LOG.info("dashboard ready url=%s", self.dashboard.url)
            if self.open_dashboard:
                await asyncio.to_thread(webbrowser.open, self.dashboard.url)
        except OSError as exc:
            LOG.error("dashboard failed error=%s", exc)
        await self._recover_pending_events()
        self.listeners = [
            asyncio.create_task(self._listen_contact(contact), name=f"listen:{contact.alias}")
            for contact in self.settings.contacts
        ]
        self.runtime_heartbeat = asyncio.create_task(
            self._keep_runtime_alive(), name="runtime-heartbeat"
        )
        await self.stop_event.wait()
        LOG.info("stopping listeners gracefully")
        for task in self.listeners:
            task.cancel()
        for task in self.workers.values():
            task.cancel()
        self.runtime_heartbeat.cancel()
        await asyncio.gather(
            *self.listeners,
            *self.workers.values(),
            self.runtime_heartbeat,
            return_exceptions=True,
        )

    def request_stop(self) -> None:
        self.stop_event.set()

    async def _recover_pending_events(self) -> None:
        async def recover(contact: Contact) -> None:
            pending = self.store.pending_event_ids(contact.user_id)
            if not pending:
                return
            try:
                history = await self.dws.history(contact)
            except Exception as exc:
                LOG.warning("queued event recovery failed contact=%s error=%s", contact.alias, exc)
                return
            for message in history:
                if (
                    message.message_id not in pending
                    or message.sender_open_dingtalk_id != contact.open_dingtalk_id
                ):
                    continue
                self._enqueue(
                    IncomingEvent(
                        event_id=f"recovery:{message.message_id}",
                        message_id=message.message_id,
                        conversation_id=message.conversation_id,
                        contact=contact,
                        content=message.content,
                        created_at=message.created_at,
                    )
                )
                pending.remove(message.message_id)
            if pending:
                LOG.info(
                    "queued events remain pending after limited history recovery "
                    "contact=%s count=%d",
                    contact.alias,
                    len(pending),
                )

        await asyncio.gather(*(recover(contact) for contact in self.settings.contacts))

    async def _keep_runtime_alive(self) -> None:
        while True:
            await asyncio.sleep(5)
            self._write_runtime()

    def _configuration_is_idle(self) -> bool:
        with self.configuration_lock:
            return not self.runtime_states and all(
                queue.empty() for queue in self.queues.values()
            )

    def _apply_agent_configuration(self, raw: Mapping[str, Any]) -> None:
        env = _read_env_file(self.settings.env_path)
        env.update(os.environ)
        candidate = dict(self.settings.raw)
        candidate["agents"] = _resolve_env(raw.get("agents", {}), env)
        candidate["workflows"] = _resolve_env(raw.get("workflows", {}), env)
        runtime = AgentRuntime.from_config(
            candidate, self.settings.config_path, self.settings.workspace_root
        )
        missing = [
            f"{name}: {binary}"
            for name, binary in runtime.required_binaries().items()
            if shutil.which(binary) is None
        ]
        if missing:
            raise ValueError(f"active agent launcher not found: {', '.join(missing)}")
        previous_agents = self.settings.raw.get("agents")
        previous_workflows = self.settings.raw.get("workflows")
        previous_runtime = self.agent_runtime
        try:
            self.settings.raw["agents"] = candidate["agents"]
            self.settings.raw["workflows"] = candidate["workflows"]
            self.agent_runtime = runtime
            self._write_runtime()
        except Exception:
            self.settings.raw["agents"] = previous_agents
            self.settings.raw["workflows"] = previous_workflows
            self.agent_runtime = previous_runtime
            raise

    def _write_runtime(self) -> None:
        payload = {
            "pid": os.getpid(),
            "mode": self.mode,
            "contacts": len(self.settings.contacts),
            "capacity": self.capacity,
            "workflow": self.agent_runtime.describe(),
            "heartbeatAt": datetime.now(self.settings.timezone).isoformat(),
            "active": sorted(
                self.runtime_states.values(), key=lambda item: item["startedAt"]
            ),
        }
        temporary = self.runtime_path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, self.runtime_path)
        except OSError as exc:
            LOG.warning("runtime snapshot failed error=%s", exc)

    def _set_runtime(
        self,
        batch: Sequence[IncomingEvent],
        phase: str,
        *,
        attempt: int | None = None,
        session_id: str | None = None,
    ) -> None:
        conversation_id = batch[0].conversation_id
        now = datetime.now(self.settings.timezone).isoformat()
        state = self.runtime_states.setdefault(
            conversation_id,
            {
                "conversationId": conversation_id,
                "contactName": batch[0].contact.display_name,
                "contactAlias": batch[0].contact.alias,
                "startedAt": min(item.created_at for item in batch).isoformat(),
                "attempt": 1,
                "sessionId": "",
            },
        )
        labels = {
            "stabilizing": "正在合并消息",
            "screening": "正在判断是否处理",
            "acknowledged": "已确认，准备执行",
            "routing": "前置 Agent 正在只读处理",
            "worker": "后置 Agent 正在执行",
            "steering": "已收到补充消息，正在调整处理",
            "verifying": "正在核验结果",
            "freshness": "正在检查补充消息",
            "sending": "正在发送结果",
        }
        state.update(
            {
                "phase": phase,
                "phaseLabel": labels.get(phase, phase),
                "updatedAt": now,
                "batchSize": len(batch),
                "requestPreview": self._request_preview(batch),
            }
        )
        if attempt is not None:
            state["attempt"] = attempt
        if session_id is not None:
            state["sessionId"] = session_id
        self._write_runtime()

    @staticmethod
    def _request_preview(batch: Sequence[IncomingEvent]) -> str:
        return sanitize_text("\n".join(item.content for item in batch), 500)

    def _touch_runtime(self, conversation_id: str) -> None:
        if conversation_id in self.runtime_states:
            self.runtime_states[conversation_id]["updatedAt"] = datetime.now(
                self.settings.timezone
            ).isoformat()
            self._write_runtime()

    def _clear_runtime(self, conversation_id: str) -> None:
        self.runtime_states.pop(conversation_id, None)
        self._write_runtime()

    async def _listen_contact(self, contact: Contact) -> None:
        delay = 1.0
        while not self.stop_event.is_set():
            command = [
                str(self.settings.raw.get("dws", {}).get("binary", "dws")),
                "event",
                "consume",
                "user_im_message_receive_o2o",
                "--user",
                contact.user_id,
                "--format",
                "ndjson",
                "--ephemeral",
            ]
            profile = self.settings.raw.get("dws", {}).get("profile")
            if profile:
                command.extend(["--profile", str(profile)])
            process: asyncio.subprocess.Process | None = None
            stderr_task: asyncio.Task[None] | None = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self.listener_processes.add(process)
                stderr_task = asyncio.create_task(self._drain_listener_stderr(process, contact))
                assert process.stdout is not None
                async for raw_line in process.stdout:
                    event = parse_dws_event(
                        raw_line.decode("utf-8", errors="replace"), contact, self.settings.timezone
                    )
                    if event is None:
                        continue
                    if not self.store.claim_event(event) and not self.store.event_is_pending(
                        event.message_id
                    ):
                        continue
                    self._enqueue(event)
                return_code = await process.wait()
                if not self.stop_event.is_set():
                    LOG.warning("listener exited contact=%s code=%s", contact.alias, return_code)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.error("listener error contact=%s error=%s", contact.alias, exc)
            finally:
                if stderr_task:
                    stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)
                if process is not None:
                    self.listener_processes.discard(process)
                    if process.returncode is None:
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.communicate(), timeout=10)
                    except TimeoutError:
                        LOG.error("listener did not stop after SIGTERM contact=%s", contact.alias)
            if not self.stop_event.is_set():
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _drain_listener_stderr(
        self, process: asyncio.subprocess.Process, contact: Contact
    ) -> None:
        assert process.stderr is not None
        async for raw_line in process.stderr:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if "ready" in line.lower():
                LOG.info("listener ready contact=%s", contact.alias)
            elif line:
                LOG.debug(
                    "listener status contact=%s detail_hash=%s detail_length=%d",
                    contact.alias,
                    sha256_text(line)[:12],
                    len(line),
                )

    def _enqueue(self, event: IncomingEvent) -> None:
        with self.configuration_lock:
            queue = self.queues.setdefault(event.conversation_id, asyncio.Queue())
            queue.put_nowait(event)
            if (
                event.conversation_id not in self.workers
                or self.workers[event.conversation_id].done()
            ):
                self.workers[event.conversation_id] = asyncio.create_task(
                    self._conversation_worker(event.conversation_id),
                    name=f"conversation:{sha256_text(event.conversation_id)[:8]}",
                )

    async def _conversation_worker(self, conversation_id: str) -> None:
        queue = self.queues[conversation_id]
        while not self.stop_event.is_set():
            first = await queue.get()
            batch = [first]
            self._set_runtime(batch, "stabilizing")
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=self.settings.quiet_window)
                    batch.append(event)
                    self._set_runtime(batch, "stabilizing")
                except TimeoutError:
                    break
            try:
                async with self.global_slots:
                    pending = [
                        item
                        for item in self._deduplicate(batch)
                        if self.store.event_is_pending(item.message_id)
                    ]
                    if pending:
                        await self._handle_batch(pending)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.exception(
                    "conversation failure conversation=%s error=%s",
                    sha256_text(conversation_id)[:10],
                    exc,
                )
                reason = sanitize_text(str(exc), 500) or type(exc).__name__
                try:
                    self._record_without_agent(
                        batch,
                        status="error",
                        action="handoff",
                        handled="处理过程中发生异常，未完成回复",
                        reason=reason,
                    )
                except Exception:
                    LOG.exception(
                        "failed to record conversation error conversation=%s",
                        sha256_text(conversation_id)[:10],
                    )
                self.store.update_event_status(
                    (item.message_id for item in batch), "error", reason
                )
            finally:
                self._clear_runtime(conversation_id)
                for _ in batch:
                    queue.task_done()

    @staticmethod
    def _deduplicate(events: Sequence[IncomingEvent]) -> list[IncomingEvent]:
        by_id: dict[str, IncomingEvent] = {}
        for event in events:
            by_id[event.message_id] = event
        return sorted(by_id.values(), key=lambda item: item.created_at)

    def _register_manual_messages(self, history: Sequence[HistoryMessage]) -> datetime | None:
        for message in history:
            if message.sender_open_dingtalk_id != self.settings.self_open_id:
                continue
            if self.store.is_agent_outgoing(message):
                # A send response may omit message_id. Reconcile it from history
                # and undo any earlier false manual-takeover classification.
                self.store.remove_manual(message.message_id)
            else:
                self.store.record_manual(message)
        if not history:
            return None
        return self.store.latest_manual(history[-1].conversation_id)

    def _human_owns_conversation(
        self, latest_manual: datetime | None, earliest_incoming: datetime
    ) -> bool:
        return human_owns_conversation(latest_manual, earliest_incoming, self.settings.cooldown)

    def _history_events(
        self,
        contact: Contact,
        conversation_id: str,
        history: Sequence[HistoryMessage],
        not_before: datetime,
    ) -> list[IncomingEvent]:
        result: list[IncomingEvent] = []
        for message in history:
            if message.sender_open_dingtalk_id != contact.open_dingtalk_id:
                continue
            if message.created_at < not_before - timedelta(seconds=2):
                continue
            result.append(
                IncomingEvent(
                    event_id=f"history:{message.message_id}",
                    message_id=message.message_id,
                    conversation_id=conversation_id,
                    contact=contact,
                    content=message.content,
                    created_at=message.created_at,
                )
            )
        return result

    def _recent_context(
        self,
        history: Sequence[HistoryMessage],
        batch: Sequence[IncomingEvent],
        *,
        limit: int | None = None,
    ) -> tuple[list[dict[str, str]], bool]:
        current_ids = {item.message_id for item in batch}
        latest_current = max(item.created_at for item in batch)
        contact = batch[0].contact
        conversation_id = batch[0].conversation_id
        messages: list[dict[str, str]] = []
        for message in sorted(history, key=lambda item: item.created_at):
            if message.message_id in current_ids or message.created_at > latest_current:
                continue
            if message.conversation_id != conversation_id:
                continue
            if message.sender_open_dingtalk_id == contact.open_dingtalk_id:
                role = "contact"
            elif message.sender_open_dingtalk_id == self.settings.self_open_id:
                role = "agent" if self.store.is_agent_outgoing(message) else "operator"
            else:
                continue
            execution = ""
            if role == "agent":
                execution = self.store.outgoing_session_id(message.message_id).partition(":")[0]
            messages.append(
                {
                    "role": role,
                    "sender": contact.display_name if role == "contact" else getattr(self.settings, "self_name", ""),
                    "execution": execution,
                    "time": message.created_at.isoformat(),
                    "message_id": message.message_id,
                    "conversation_id": message.conversation_id,
                    "text": message.content,
                }
            )
        context_limit = int(
            limit
            if limit is not None
            else self.settings.raw.get("recent_context_messages", 6)
        )
        recent = messages[-context_limit:] if context_limit > 0 else []
        follows_plan = bool(
            recent
            and recent[-1]["role"] == "agent"
            and recent[-1]["execution"] == "plan_large_change"
        )
        return recent, follows_plan

    async def _stabilize(
        self, batch: list[IncomingEvent]
    ) -> tuple[list[IncomingEvent], list[HistoryMessage], datetime | None]:
        contact = batch[0].contact
        earliest = min(item.created_at for item in batch)
        known = {item.message_id for item in batch}
        history: list[HistoryMessage] = []
        for _ in range(4):
            history = await self.dws.history(contact)
            latest_manual = self._register_manual_messages(history)
            if self._human_owns_conversation(latest_manual, earliest):
                return batch, history, latest_manual
            discovered = [
                item
                for item in self._history_events(
                    contact, batch[0].conversation_id, history, earliest
                )
                if item.message_id not in known
            ]
            if not discovered:
                return self._deduplicate(batch), history, latest_manual
            for item in discovered:
                self.store.claim_event(item)
                known.add(item.message_id)
                batch.append(item)
            await asyncio.sleep(self.settings.quiet_window)
        return self._deduplicate(batch), history, self.store.latest_manual(batch[0].conversation_id)

    async def _handle_batch(self, initial_batch: list[IncomingEvent]) -> None:
        batch = initial_batch
        prior_attempt = ""
        acknowledgement_sent = False
        front_enabled = True
        front_attempted = not front_enabled
        front_escalated = False
        execution_mode = "read_only"
        expanded_context = False
        max_replans = int(self.settings.raw.get("max_replans", 5))
        for attempt in range(max_replans):
            batch, history, latest_manual = await self._stabilize(batch)
            recent_context, follows_plan = self._recent_context(
                history,
                batch,
                limit=(
                    int(self.settings.raw.get("history_limit", 80))
                    if expanded_context
                    else None
                ),
            )
            self._set_runtime(batch, "screening", attempt=attempt + 1)
            earliest = min(item.created_at for item in batch)
            if self._human_owns_conversation(latest_manual, earliest):
                self._record_without_agent(
                    batch,
                    status="human_cooldown",
                    action="no_reply",
                    handled="人工会话仍在接管期，未自动回复",
                    reason="manual_message_within_cooldown",
                )
                self.store.update_event_status(
                    (item.message_id for item in batch), "skipped", "human_cooldown"
                )
                return

            gate = self.gate.inspect([item.content for item in batch])
            if gate.action == "drop":
                self._record_without_agent(
                    batch,
                    status="no_reply",
                    action="no_reply",
                    handled="无需回复",
                    reason=gate.reason,
                )
                self.store.update_event_status(
                    (item.message_id for item in batch), "processed", gate.reason
                )
                return
            if gate.action == "refuse":
                decision = {
                    "action": "refuse",
                    "reply": gate.reply,
                    "handled": "拒绝高风险请求",
                    "reason": gate.reason,
                    "changes": [],
                    "validation": ["确定性安全规则拦截；未调用 Agent"],
                    "external_calls": [],
                    "warnings": [],
                }
                outcome = await self._send_after_freshness_check(batch, decision, [])
                if outcome == "supplement":
                    batch = await self._merge_latest(batch)
                    continue
                status = "refused" if outcome in {"sent", "shadow"} else outcome
                self._record_without_agent(
                    batch,
                    status=status,
                    action="refuse",
                    handled=decision["handled"],
                    reason=decision["reason"],
                    validation=decision["validation"],
                )
                self.store.update_event_status(
                    (item.message_id for item in batch), "processed", gate.reason
                )
                return

            if self._agent_rate_limited(batch[0].contact):
                self._record_without_agent(
                    batch,
                    status="no_reply",
                    action="no_reply",
                    handled="触发本地 Agent 频率保护，未自动回复",
                    reason="agent_rate_limit",
                )
                self.store.update_event_status(
                    (item.message_id for item in batch), "skipped", "agent_rate_limit"
                )
                return

            workspace_before = await self._workspace_snapshot()
            run_sol = front_attempted
            front_started_at: datetime | None = None
            if not front_attempted:
                front_attempted = True
                self._set_runtime(batch, "routing", attempt=attempt + 1)
                result = await self._run_agent(
                    batch,
                    prior_attempt,
                    front=True,
                    recent_context=recent_context,
                )
                if result.consumed_supplements:
                    batch = self._deduplicate([*batch, *result.consumed_supplements])
                front_value = result.decision or {}
                execution_mode = str(front_value.get("execution", "read_only"))
                requested_more_context = bool(front_value.get("need_more_context"))
                if requested_more_context and not expanded_context:
                    expanded_context = True
                    recent_context, follows_plan = self._recent_context(
                        history,
                        batch,
                        limit=int(self.settings.raw.get("history_limit", 80)),
                    )
                    # The front agent is allowed one context expansion before
                    # handing off. This lets it answer after seeing the real
                    # preceding messages instead of turning an ambiguous
                    # short continuation into an unnecessary worker run.
                    front_attempted = False
                    continue
                force_sol = requested_more_context or execution_mode != "read_only"
                front_decision = (
                    None if force_sol else self._front_reply_decision(result.decision)
                )
                if front_decision is not None:
                    result.decision = front_decision
                    run_sol = False
                elif result.manual_takeover or result.supplements:
                    run_sol = False
                elif not result.manual_takeover and not result.supplements:
                    front_started_at = result.started_at
                    prior_attempt = self._front_route_note(result, prior_attempt)
                    front_escalated = True
                    run_sol = True
            if run_sol:
                if not acknowledgement_sent:
                    freshness, _ = await self._freshness_state(batch)
                    if freshness == "manual":
                        self._record_without_agent(
                            batch,
                            status="human_cooldown",
                            action="no_reply",
                            handled="发送处理中提示前检测到人工接管",
                            reason="manual_takeover_before_ack",
                        )
                        self.store.update_event_status(
                            (item.message_id for item in batch),
                            "skipped",
                            "human_cooldown",
                        )
                        return
                    if freshness == "supplement":
                        batch = await self._merge_latest(batch)
                        front_attempted = not front_enabled
                        front_escalated = False
                        execution_mode = "read_only"
                        expanded_context = False
                        continue
                    acknowledgement = self.agent_runtime.render_auto_message(
                        "ack",
                        {
                            "request_count": len(batch),
                            "contact_name": batch[0].contact.display_name,
                            "contact_alias": batch[0].contact.alias,
                            "self_name": self.settings.self_name,
                        },
                    )
                    if acknowledgement:
                        await self._send_supervisor_text(
                            batch[0].contact,
                            batch[0].conversation_id,
                            acknowledgement,
                            f"ack-{uuid.uuid4().hex[:12]}",
                        )
                    acknowledgement_sent = True
                    self._set_runtime(batch, "acknowledged", attempt=attempt + 1)
                self._set_runtime(batch, "worker", attempt=attempt + 1)
                result = await self._run_agent(
                    batch,
                    prior_attempt,
                    front=False,
                    escalated=front_escalated,
                    recent_context=recent_context,
                    allow_write=self._write_allowed(execution_mode, follows_plan),
                    execution_mode=execution_mode,
                )
                if result.consumed_supplements:
                    batch = self._deduplicate([*batch, *result.consumed_supplements])
                if front_started_at:
                    result.started_at = front_started_at
            result.workspace_drift = await self._workspace_drift(workspace_before)
            if result.manual_takeover:
                discovered = await self._discover_session_changes(result, [])
                cancelled_changes = await self._verify_changes(discovered)
                self._record_agent_result(
                    batch,
                    result,
                    status="human_cooldown",
                    decision={
                        "action": "no_reply",
                        "handled": "检测到人工回复，已中止 Agent",
                        "reason": "manual_takeover",
                        "changes": [],
                        "validation": [],
                        "external_calls": [],
                        "warnings": [
                            "被中止的 session worktree 如存在需在控制台复核",
                            *self._workspace_drift_warnings(result.workspace_drift),
                        ],
                    },
                    changes=cancelled_changes,
                )
                self.store.update_event_status(
                    (item.message_id for item in batch), "skipped", "manual_takeover"
                )
                return
            if result.decision is None:
                discovered = await self._discover_session_changes(result, [])
                failed_changes = await self._verify_changes(discovered)
                self._record_agent_result(
                    batch,
                    result,
                    status="error",
                    decision={
                        "action": "handoff",
                        "handled": "Agent 处理失败",
                        "reason": result.error,
                        "changes": [],
                        "validation": [],
                        "external_calls": [],
                        "warnings": self._workspace_drift_warnings(result.workspace_drift),
                    },
                    changes=failed_changes,
                )
                if result.supplements:
                    prior_attempt = self._prior_attempt_note(result, failed_changes)
                    batch = self._deduplicate([*batch, *result.supplements])
                    front_attempted = not front_enabled
                    front_escalated = False
                    execution_mode = "read_only"
                    expanded_context = False
                    self._set_runtime(batch, "stabilizing", attempt=attempt + 1)
                    await asyncio.sleep(self.settings.quiet_window)
                    continue
                self.store.update_event_status(
                    (item.message_id for item in batch), "error", result.error[:200]
                )
                return

            decision = result.decision
            decision["session_id"] = f"{execution_mode}:{result.session_id}"
            self._set_runtime(
                batch, "verifying", attempt=attempt + 1, session_id=result.session_id
            )
            declared_changes = list(decision.get("changes", []))
            declared_changes.extend(
                await self._discover_session_changes(result, declared_changes)
            )
            changes = await self._verify_changes(declared_changes)
            external_warnings = self._external_call_warnings(decision.get("external_calls", []))
            if external_warnings:
                decision["warnings"].extend(external_warnings)
                decision["action"] = "handoff"
                decision["reply"] = (
                    "这里碰到了不在白名单里的外部地址，我先停了。"
                    "这个调用得确认过用途和授权范围后才能继续。"
                )
                decision["handled"] = "外部调用证据异常，转人工复核"
            if result.workspace_drift:
                decision["warnings"].extend(
                    self._workspace_drift_warnings(result.workspace_drift)
                )
                decision["action"] = "handoff"
                decision["reply"] = (
                    "主工作区里出现了不属于这次独立分支的改动，我先没往下做。"
                    "得先确认这些改动是谁在处理。"
                )
                decision["handled"] = "主工作区状态漂移，转人工复核"

            if result.supplements:
                self._record_agent_result(
                    batch,
                    result,
                    status="stale_supplement",
                    decision=decision,
                    changes=changes,
                )
                prior_attempt = self._prior_attempt_note(result, changes)
                batch = self._deduplicate([*batch, *result.supplements])
                front_attempted = not front_enabled
                front_escalated = False
                execution_mode = "read_only"
                expanded_context = False
                self._set_runtime(batch, "stabilizing", attempt=attempt + 1)
                await asyncio.sleep(self.settings.quiet_window)
                continue

            if decision["action"] == "no_reply":
                if changes:
                    decision["action"] = "handoff"
                    decision["reply"] = (
                        "这次已经产生了代码改动，不能当成无需回复。"
                        "我把分支和文件信息一起发出来。"
                    )
                    decision["handled"] = "发现代码状态，转人工复核"
                    decision["warnings"].append("有代码状态时禁止静默 no_reply")
                else:
                    self._record_agent_result(
                        batch, result, status="no_reply", decision=decision, changes=[]
                    )
                    self.store.update_event_status(
                        (item.message_id for item in batch), "processed", decision["reason"]
                    )
                    return

            if changes and any(not item.verified for item in changes):
                decision["action"] = "handoff"
                decision["reply"] = "这次改动还没有形成可核验的提交，我先不把它当成完成结果。"
                decision["handled"] = "代码证据不完整，转人工复核"
                decision["warnings"].append("存在未核验代码状态，未自动宣称完成")

            required_pushes = {
                "dev": {"origin/dev"},
                "test": {"origin/dev", "origin/test"},
            }.get(decision["delivery"], set())
            if (
                changes
                and all(item.verified for item in changes)
                and decision["delivery"] != "attachment"
                and any(
                    not item.pushed_to
                    or not required_pushes.issubset(set(item.pushed_to))
                    for item in changes
                )
            ):
                self._record_agent_result(
                    batch,
                    result,
                    status="delivery_replan",
                    decision=decision,
                    changes=changes,
                )
                prior_attempt = self._prior_attempt_note(
                    result,
                    changes,
                    retry_instruction=(
                        "上一版未发送：按你选择的 delivery 完成交付。"
                        "attachment 由钉钉回复服务发送；其他代码交付需 push 并在 pushed_to 写真实远端分支。"
                        "delivery=dev 必须包含 origin/dev；delivery=test 必须同时包含 origin/dev 和 origin/test。"
                    ),
                )
                continue

            self._set_runtime(batch, "freshness", attempt=attempt + 1)
            try:
                outcome = await self._send_after_freshness_check(batch, decision, changes)
            except Exception as exc:
                reason = sanitize_text(str(exc), 500) or type(exc).__name__
                failed_decision = dict(decision)
                failed_decision["handled"] = (
                    f"{decision.get('handled') or 'Agent 已完成处理'}；最终回复发送失败"
                )
                failed_decision["reason"] = f"send_failed: {reason}"
                failed_decision["warnings"] = [
                    *decision.get("warnings", []),
                    "最终回复未送达",
                ]
                self._record_agent_result(
                    batch,
                    result,
                    status="error",
                    decision=failed_decision,
                    changes=changes,
                )
                self.store.update_event_status(
                    (item.message_id for item in batch),
                    "error",
                    failed_decision["reason"],
                )
                return
            if outcome == "supplement":
                self._record_agent_result(
                    batch,
                    result,
                    status="stale_supplement",
                    decision=decision,
                    changes=changes,
                )
                prior_attempt = self._prior_attempt_note(result, changes)
                batch = await self._merge_latest(batch)
                front_attempted = not front_enabled
                front_escalated = False
                execution_mode = "read_only"
                expanded_context = False
                self._set_runtime(batch, "stabilizing", attempt=attempt + 1)
                await asyncio.sleep(self.settings.quiet_window)
                continue
            status = outcome
            self._record_agent_result(
                batch, result, status=status, decision=decision, changes=changes
            )
            self.store.update_event_status(
                (item.message_id for item in batch), "processed", decision["reason"]
            )
            return

        self._record_without_agent(
            batch,
            status="no_reply",
            action="no_reply",
            handled="对方持续补充消息，本轮未抢答",
            reason="max_replans_reached",
        )
        self.store.update_event_status(
            (item.message_id for item in batch), "skipped", "max_replans_reached"
        )

    def _agent_rate_limited(self, contact: Contact) -> bool:
        limits = self.settings.raw.get("rate_limit", {})
        contact_limit = int(limits.get("per_contact_per_hour", 12))
        global_limit = int(limits.get("global_per_hour", 40))
        since = datetime.now(self.settings.timezone) - timedelta(hours=1)
        contact_count, global_count = self.store.agent_run_counts_since(since, contact.user_id)
        return contact_count >= contact_limit or global_count >= global_limit

    async def _merge_latest(self, batch: list[IncomingEvent]) -> list[IncomingEvent]:
        contact = batch[0].contact
        history = await self.dws.history(contact)
        self._register_manual_messages(history)
        earliest = min(item.created_at for item in batch)
        discovered = self._history_events(
            contact, batch[0].conversation_id, history, earliest
        )
        for item in discovered:
            self.store.claim_event(item)
        return self._deduplicate([*batch, *discovered])

    def _record_without_agent(
        self,
        batch: Sequence[IncomingEvent],
        *,
        status: str,
        action: str,
        handled: str,
        reason: str,
        validation: Sequence[str] = (),
    ) -> None:
        now = datetime.now(self.settings.timezone)
        self.store.record_run(
            session_id=f"code-{uuid.uuid4().hex[:12]}",
            conversation_id=batch[0].conversation_id,
            contact=batch[0].contact,
            started_at=now,
            finished_at=now,
            action=action,
            status=status,
            handled=handled,
            reason=reason,
            changes=[],
            validation=validation,
            external_calls=[],
            warnings=[],
            codex_exit_code=None,
            request_preview=self._request_preview(batch),
        )

    def _record_agent_result(
        self,
        batch: Sequence[IncomingEvent],
        result: AgentResult,
        *,
        status: str,
        decision: Mapping[str, Any],
        changes: Sequence[ChangeEvidence],
    ) -> None:
        self.store.record_run(
            session_id=result.session_id,
            conversation_id=batch[0].conversation_id,
            contact=batch[0].contact,
            started_at=result.started_at,
            finished_at=result.finished_at,
            action=str(decision.get("action") or "handoff"),
            status=status,
            handled=str(decision.get("handled") or ""),
            reason=str(decision.get("reason") or result.error),
            changes=[evidence_to_mapping(item) for item in changes],
            validation=[str(x) for x in decision.get("validation", [])],
            external_calls=[str(x) for x in decision.get("external_calls", [])],
            warnings=[str(x) for x in decision.get("warnings", [])],
            codex_exit_code=result.exit_code,
            request_preview=self._request_preview(batch),
        )

    def _prior_attempt_note(
        self,
        result: AgentResult,
        changes: Sequence[ChangeEvidence] = (),
        retry_instruction: str = "",
    ) -> str:
        decision = result.decision or {}
        compact = {
            "previous_session": result.session_id,
            "handled": decision.get("handled", ""),
            "reason": decision.get("reason", ""),
            "changes": [evidence_to_mapping(item) for item in changes]
            or decision.get("changes", []),
            "instruction": retry_instruction
            or "这是前一独立 session 的可核验结果；如已有 worktree，先检查并复用，禁止重复或覆盖。",
        }
        return json.dumps(compact, ensure_ascii=False)

    @staticmethod
    def _front_reply_decision(
        value: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not value or value.get("route") != "reply":
            return None
        action = str(value.get("action") or "")
        reply = str(value.get("reply") or "")
        if action not in {"reply", "no_reply"} or (
            action == "reply" and not reply.strip()
        ):
            return None
        return normalize_decision(
            {
                "action": action,
                "delivery": "none",
                "reply": reply,
                "handled": value.get("handled", ""),
                "reason": value.get("reason", ""),
                "changes": [],
                "validation": value.get("validation", []),
                "external_calls": [],
                "warnings": value.get("warnings", []),
            }
        )

    @staticmethod
    def _write_allowed(execution_mode: str, follows_plan: bool) -> bool:
        return execution_mode == "small_change" or (
            execution_mode == "approved_plan" and follows_plan
        )

    @staticmethod
    def _front_route_note(result: AgentResult, prior_attempt: str) -> str:
        decision = result.decision or {}
        return json.dumps(
            {
                "front_session": result.session_id,
                "front_route": decision.get("route", "error"),
                "handled": decision.get("handled", ""),
                "reason": decision.get("reason", "") or result.error,
                "execution": decision.get("execution", "read_only"),
                "need_more_context": decision.get("need_more_context", False),
                "validation": decision.get("validation", []),
                "previous_attempt": prior_attempt or "无",
                "instruction": "前置 Agent 未直接完成；由后置 Agent 继续处理完整任务。",
            },
            ensure_ascii=False,
        )

    def _prompt_variables(
        self,
        stage: str,
        session_id: str,
        batch: Sequence[IncomingEvent],
        prior_attempt: str,
        recent_context: Sequence[Mapping[str, str]],
        *,
        allow_write: bool,
        execution_mode: str,
    ) -> dict[str, Any]:
        contact = batch[0].contact
        messages = [
            {
                "time": item.created_at.isoformat(),
                "sender": item.contact.display_name,
                "role": "contact",
                "conversation_id": item.conversation_id,
                "message_id": item.message_id,
                "text": item.content,
            }
            for item in batch
        ]
        if allow_write:
            execution_rule = "对方已明确授权这个边界清楚的小改动，可以按代码规则执行。"
        elif execution_mode == "plan_large_change":
            execution_rule = (
                "这是大改方案阶段：只读核对影响面，给出改动方案、涉及模块和验证计划，"
                "并明确询问对方是否按该方案执行。收到首肯前禁止修改文件、创建 worktree、"
                "commit、push、合并或发布，changes 必须为 []。"
            )
        else:
            execution_rule = (
                "这是连续对话的只读分析阶段。结合最近上下文回答原问题；禁止修改文件、"
                "创建 worktree、commit、push、合并或发布，changes 必须为 []。"
            )
        security = self.settings.raw.get("security", {})
        return {
            "self_name": self.settings.self_name,
            "agent_name": self.agent_runtime.profile_name(stage),
            "worker_name": self.agent_runtime.profile_name("worker"),
            "contact_name": contact.display_name,
            "contact_alias": contact.alias,
            "contact_user_id": contact.user_id,
            "session_id": session_id,
            "prior_attempt": prior_attempt or "无",
            "workspace_root": self.settings.workspace_root,
            "worktree_root": self.settings.worktree_root,
            "recent_context_json": json.dumps(
                list(recent_context), ensure_ascii=False, indent=2
            ),
            "current_messages_json": json.dumps(
                messages, ensure_ascii=False, indent=2
            ),
            "execution_domains_json": json.dumps(
                security.get("allowed_execution_domains", []), ensure_ascii=False
            ),
            "reference_domains_json": json.dumps(
                security.get("allowed_reference_domains", []), ensure_ascii=False
            ),
            "execution_mode": execution_mode,
            "execution_rule": execution_rule,
            "now": datetime.now(self.settings.timezone).strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def _run_agent(
        self,
        batch: Sequence[IncomingEvent],
        prior_attempt: str,
        *,
        front: bool = False,
        escalated: bool = False,
        recent_context: Sequence[Mapping[str, str]] = (),
        allow_write: bool = True,
        execution_mode: str = "small_change",
    ) -> AgentResult:
        stage = "front" if front else "worker"
        profile = re.sub(
            r"[^a-z0-9]+",
            "-",
            self.agent_runtime.profile_name(stage).lower(),
        ).strip("-")
        session_id = (
            f"dm-{datetime.now(self.settings.timezone):%Y%m%d}-{profile}-"
            f"{uuid.uuid4().hex[:10]}"
        )
        self._set_runtime(
            batch, "routing" if front else "worker", session_id=session_id
        )
        started_at = datetime.now(self.settings.timezone)
        temp_root = self.settings.state_dir / "sessions"
        temp_root.mkdir(parents=True, exist_ok=True)
        session_dir = Path(tempfile.mkdtemp(prefix=f"{session_id}-", dir=temp_root))
        prompt = self.agent_runtime.render(
            stage,
            self._prompt_variables(
                stage,
                session_id,
                batch,
                prior_attempt,
                recent_context,
                allow_write=allow_write,
                execution_mode=execution_mode,
            ),
        )
        session = self.agent_runtime.open_session(
            stage, session_id, prompt, session_dir
        )
        supplements: dict[str, IncomingEvent] = {}
        consumed: dict[str, IncomingEvent] = {}
        try:
            await session.start()
            interval = float(self.settings.raw.get("monitor_interval_seconds", 5))
            timeout = session.prepared.timeout_seconds
            progress_interval = (
                self.agent_runtime.progress_interval_seconds
                if not front and self.agent_runtime.progress_enabled
                else 0.0
            )
            max_progress_updates = self.agent_runtime.max_progress_updates
            deadline = asyncio.get_running_loop().time() + timeout
            next_progress_at = asyncio.get_running_loop().time() + progress_interval
            progress_updates = 0
            last_progress = ""
            known_ids = {item.message_id for item in batch}
            earliest = min(item.created_at for item in batch)
            while not session.done:
                self._touch_runtime(batch[0].conversation_id)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await session.abort()
                    return AgentResult(
                        session_id,
                        None,
                        session.exit_code,
                        started_at,
                        datetime.now(self.settings.timezone),
                        consumed_supplements=sorted(
                            consumed.values(), key=lambda item: item.created_at
                        ),
                        error="agent_timeout",
                    )
                try:
                    await session.wait(timeout=min(interval, remaining))
                except TimeoutError:
                    pass
                if session.done:
                    break
                try:
                    history = await self.dws.history(batch[0].contact)
                except Exception as exc:
                    LOG.warning(
                        "freshness poll failed session=%s error=%s", session_id, exc
                    )
                    continue
                latest_manual = self._register_manual_messages(history)
                if latest_manual and latest_manual >= started_at - timedelta(seconds=2):
                    await session.abort()
                    return AgentResult(
                        session_id,
                        None,
                        session.exit_code,
                        started_at,
                        datetime.now(self.settings.timezone),
                        manual_takeover=True,
                        consumed_supplements=sorted(
                            consumed.values(), key=lambda item: item.created_at
                        ),
                        error="manual_takeover",
                    )
                new_supplements: list[IncomingEvent] = []
                for item in self._history_events(
                    batch[0].contact, batch[0].conversation_id, history, earliest
                ):
                    if item.message_id in known_ids:
                        continue
                    known_ids.add(item.message_id)
                    supplements[item.message_id] = item
                    new_supplements.append(item)
                    self.store.claim_event(item)
                if new_supplements:
                    steer_prompt = self.agent_runtime.render_supplement(
                        {
                            "supplement_messages_json": json.dumps(
                                [
                                    {
                                        "time": item.created_at.isoformat(),
                                        "message_id": item.message_id,
                                        "text": item.content,
                                    }
                                    for item in new_supplements
                                ],
                                ensure_ascii=False,
                                indent=2,
                            ),
                            "session_id": session_id,
                            "contact_name": batch[0].contact.display_name,
                            "contact_alias": batch[0].contact.alias,
                            "self_name": self.settings.self_name,
                        }
                    )
                    steered = (
                        self.agent_runtime.supplement_strategy == "steer"
                        and await session.steer(steer_prompt)
                    )
                    if steered:
                        for item in new_supplements:
                            consumed[item.message_id] = item
                            supplements.pop(item.message_id, None)
                        self._set_runtime(
                            [*batch, *consumed.values()],
                            "steering",
                            session_id=session_id,
                        )
                    else:
                        await session.abort()
                        return AgentResult(
                            session_id,
                            None,
                            session.exit_code,
                            started_at,
                            datetime.now(self.settings.timezone),
                            supplements=sorted(
                                supplements.values(), key=lambda item: item.created_at
                            ),
                            consumed_supplements=sorted(
                                consumed.values(), key=lambda item: item.created_at
                            ),
                            error="supplement_restart",
                        )
                now_monotonic = asyncio.get_running_loop().time()
                if (
                    progress_interval > 0
                    and progress_updates < max_progress_updates
                    and now_monotonic >= next_progress_at
                ):
                    progress = session.latest_progress
                    if progress and progress != last_progress:
                        progress_text = self.agent_runtime.render_auto_message(
                            "progress",
                            {
                                "progress": progress,
                                "request_count": len(batch) + len(consumed),
                                "contact_name": batch[0].contact.display_name,
                                "contact_alias": batch[0].contact.alias,
                                "self_name": self.settings.self_name,
                            },
                        )
                        if progress_text:
                            try:
                                await self._send_supervisor_text(
                                    batch[0].contact,
                                    batch[0].conversation_id,
                                    progress_text,
                                    f"{session_id}:progress:{progress_updates + 1}",
                                )
                                progress_updates += 1
                                last_progress = progress
                            except Exception as exc:
                                LOG.warning(
                                    "progress update failed session=%s error=%s",
                                    session_id,
                                    exc,
                                )
                    next_progress_at = now_monotonic + progress_interval
            if session.error:
                return AgentResult(
                    session_id,
                    None,
                    session.exit_code,
                    started_at,
                    datetime.now(self.settings.timezone),
                    consumed_supplements=sorted(
                        consumed.values(), key=lambda item: item.created_at
                    ),
                    error=session.error,
                )
            parsed = session.decision()
            decision = parsed if front else normalize_decision(parsed)
            return AgentResult(
                session_id,
                decision,
                session.exit_code if session.exit_code is not None else 0,
                started_at,
                datetime.now(self.settings.timezone),
                consumed_supplements=sorted(
                    consumed.values(), key=lambda item: item.created_at
                ),
            )
        except asyncio.CancelledError:
            await session.abort()
            raise
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return AgentResult(
                session_id,
                None,
                session.exit_code,
                started_at,
                datetime.now(self.settings.timezone),
                supplements=sorted(supplements.values(), key=lambda item: item.created_at),
                consumed_supplements=sorted(
                    consumed.values(), key=lambda item: item.created_at
                ),
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            await session.close()
            shutil.rmtree(session_dir, ignore_errors=True)

    async def _workspace_snapshot(self) -> dict[str, str]:
        async def signature(root: Path) -> tuple[str, str]:
            head_code, head = await self._run_git(root, "rev-parse", "HEAD")
            status_code, status = await self._run_git(
                root, "status", "--porcelain=v1", "--untracked-files=normal"
            )
            if head_code != 0 or status_code != 0:
                return str(root), "unavailable"
            return str(root), sha256_text(f"{head}\0{status}")

        pairs = await asyncio.gather(*(signature(root) for root in self.main_repo_roots))
        return dict(pairs)

    async def _workspace_drift(self, before: Mapping[str, str]) -> list[str]:
        after = await self._workspace_snapshot()
        return sorted(
            Path(path).name
            for path, signature in before.items()
            if after.get(path) != signature
        )

    @staticmethod
    def _workspace_drift_warnings(repositories: Sequence[str] | None) -> list[str]:
        if not repositories:
            return []
        return [f"主检出目录状态发生变化：{', '.join(repositories)}"]

    async def _discover_session_changes(
        self,
        result: AgentResult,
        declared_changes: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        declared_paths = {
            str(Path(str(item.get("worktree"))).resolve())
            for item in declared_changes
            if item.get("worktree")
        }
        discovered: list[dict[str, Any]] = []
        for candidate in self.settings.worktree_root.glob(f"{result.session_id}-*"):
            candidate = candidate.resolve()
            if str(candidate) in declared_paths or not candidate.is_dir():
                continue
            root_code, root_text = await self._run_git(candidate, "rev-parse", "--show-toplevel")
            if root_code != 0:
                continue
            root = Path(root_text).resolve()
            dirty_code, dirty = await self._run_git(root, "status", "--porcelain")
            head_code, head = await self._run_git(root, "rev-parse", "HEAD")
            branch_code, branch = await self._run_git(root, "branch", "--show-current")
            reflog_code, reflog = await self._run_git(
                root, "reflog", "--date=unix", "--format=%H%x09%gs%x09%gD"
            )
            session_commits: list[str] = []
            base_sha = head
            if reflog_code == 0:
                earliest_second = int(result.started_at.timestamp()) - 1
                records: list[tuple[str, str, int]] = []
                for line in reflog.splitlines():
                    parts = line.split("\t", 2)
                    timestamp_match = re.search(r"@\{(\d+)\}$", parts[2]) if len(parts) == 3 else None
                    if timestamp_match is None:
                        continue
                    records.append((parts[0], parts[1], int(timestamp_match.group(1))))
                changed_at = [
                    index
                    for index, (_, action, timestamp) in enumerate(records)
                    if timestamp >= earliest_second
                    and action.lower().startswith(
                        ("commit", "merge", "rebase", "cherry-pick", "revert")
                    )
                ]
                if changed_at:
                    oldest_change = max(changed_at)
                    if oldest_change + 1 < len(records):
                        base_sha = records[oldest_change + 1][0]
                    else:
                        first_changed_sha = records[oldest_change][0]
                        base_code, base_text = await self._run_git(
                            root, "rev-parse", f"{first_changed_sha}^"
                        )
                        if base_code == 0:
                            base_sha = base_text
                    if head_code == 0 and base_sha != head:
                        commits_code, commits_text = await self._run_git(
                            root, "rev-list", "--reverse", f"{base_sha}..{head}"
                        )
                        if commits_code == 0:
                            session_commits = [
                                line for line in commits_text.splitlines() if line
                            ]
            if not dirty and not session_commits:
                continue
            dirty_files = self._status_files(dirty) if dirty_code == 0 else []
            discovered.append(
                {
                    "repo": root.name,
                    "worktree": str(root),
                    "branch": branch if branch_code == 0 else "",
                    "base_sha": base_sha if head_code == 0 else "",
                    "head_sha": head if head_code == 0 else "",
                    "commits": session_commits,
                    "files": dirty_files,
                    "pushed_to": [],
                }
            )
        return discovered

    @staticmethod
    def _status_files(status_text: str) -> list[str]:
        files: list[str] = []
        for line in status_text.splitlines():
            path = line[3:].strip() if len(line) > 3 else ""
            if " -> " in path:
                path = path.rsplit(" -> ", 1)[-1]
            if path:
                files.append(path.strip('"'))
        return sorted(set(files))

    @staticmethod
    def _domain_allowed(host: str, domains: Sequence[str]) -> bool:
        host = host.lower().rstrip(".")
        try:
            address = ipaddress.ip_address(host)
            return address.is_private or address.is_loopback
        except ValueError:
            pass
        return any(host == domain or host.endswith(f".{domain}") for domain in domains)

    def _external_call_warnings(self, calls: Sequence[str]) -> list[str]:
        security = self.settings.raw.get("security", {})
        execution_domains = [str(x).lower() for x in security.get("allowed_execution_domains", [])]
        reference_domains = [str(x).lower() for x in security.get("allowed_reference_domains", [])]
        warnings: list[str] = []
        for call in calls:
            urls = re.findall(r"https?://[^\s<>\]\[\"']+", str(call), re.IGNORECASE)
            for raw_url in urls:
                host = (urlparse(raw_url).hostname or "").lower()
                if self._domain_allowed(host, execution_domains):
                    continue
                read_only = bool(
                    re.search(r"(?:\bGET\b|read|fetch|docs?|查询|读取|查看)", str(call), re.IGNORECASE)
                )
                if read_only and self._domain_allowed(host, reference_domains):
                    continue
                warnings.append(f"未授权外部调用：{host or '未知主机'}")
        return warnings

    async def _run_git(self, worktree: Path, *arguments: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(worktree),
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        output = stdout.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            output = stderr.decode("utf-8", errors="replace").strip()
        return process.returncode or 0, output

    async def _verify_changes(
        self, raw_changes: Sequence[Mapping[str, Any]]
    ) -> list[ChangeEvidence]:
        verified: list[ChangeEvidence] = []
        for raw in raw_changes:
            worktree_text = str(raw.get("worktree") or "")
            item = ChangeEvidence(
                repo=str(raw.get("repo") or ""),
                worktree=worktree_text,
                branch=str(raw.get("branch") or ""),
                base_sha=str(raw.get("base_sha") or ""),
                head_sha=str(raw.get("head_sha") or ""),
                commits=[str(x) for x in raw.get("commits", [])],
                files=[str(x) for x in raw.get("files", [])],
                pushed_to=[str(x) for x in raw.get("pushed_to", [])],
            )
            if not worktree_text:
                item.warning = "缺少 worktree 路径"
                verified.append(item)
                continue
            worktree = Path(worktree_text).resolve()
            if not _is_beneath(worktree, self.settings.worktree_root):
                # Main-checkout mutations are handled separately by workspace_drift.
                # Existing tags and release inputs are not changes made by this session.
                continue
            root_code, root_text = await self._run_git(worktree, "rev-parse", "--show-toplevel")
            if root_code != 0:
                item.warning = "无法核验 git worktree"
                verified.append(item)
                continue
            root = Path(root_text).resolve()
            if not _is_beneath(root, self.settings.worktree_root):
                item.warning = "git 根目录逃逸出规定的独立 worktree 根目录"
                verified.append(item)
                continue
            branch_code, branch = await self._run_git(root, "branch", "--show-current")
            head_code, head = await self._run_git(root, "rev-parse", "HEAD")
            dirty_code, dirty = await self._run_git(root, "status", "--porcelain")
            if branch_code == 0:
                item.branch = branch
            if head_code == 0:
                declared_head = item.head_sha
                item.head_sha = head
            else:
                declared_head = item.head_sha
            item.worktree = str(root)
            item.repo = item.repo or root.name
            base_valid = False
            if item.base_sha:
                base_code, _ = await self._run_git(
                    root, "cat-file", "-e", f"{item.base_sha}^{{commit}}"
                )
                base_valid = base_code == 0
            if base_valid and head:
                files_code, files_text = await self._run_git(
                    root, "diff", "--name-only", item.base_sha, head
                )
                commits_code, commits_text = await self._run_git(
                    root, "log", "--format=%H", f"{item.base_sha}..{head}"
                )
                if files_code == 0:
                    item.files = [line for line in files_text.splitlines() if line]
                if commits_code == 0:
                    item.commits = [line for line in commits_text.splitlines() if line]
            if dirty_code == 0 and dirty:
                item.files = sorted(set([*item.files, *self._status_files(dirty)]))
            warnings: list[str] = []
            if dirty_code != 0 or dirty:
                warnings.append("worktree 仍有未提交改动")
            if not item.branch:
                warnings.append("无法核验当前分支")
            if not declared_head:
                warnings.append("缺少声明的 head_sha")
            elif head and not head.startswith(declared_head):
                warnings.append("声明的 head_sha 与实际 HEAD 不一致")
            if not item.base_sha:
                warnings.append("缺少 base_sha，无法建立变更边界")
            elif not base_valid:
                warnings.append("base_sha 不是可核验提交")
            if not item.commits:
                warnings.append("没有可核验的提交回滚点")
            if item.commits and not item.files:
                warnings.append("提交范围内没有可列出的改动文件")
            if item.pushed_to:
                remote_code, remote_refs = await self._run_git(
                    root, "branch", "-r", "--contains", head
                )
                actual_refs = {line.strip().lstrip("*").strip() for line in remote_refs.splitlines()}
                for target in item.pushed_to:
                    normalized = target.strip()
                    target_found = any(
                        ref == normalized
                        or ref.endswith(f"/{normalized}")
                        or normalized.endswith(f"/{ref}")
                        for ref in actual_refs
                    )
                    if remote_code != 0 or not target_found:
                        warnings.append(f"未核验到远端分支包含该提交：{normalized}")
            item.warning = "；".join(warnings)
            item.verified = not warnings and head_code == 0
            verified.append(item)
        return verified

    async def _freshness_state(
        self, batch: Sequence[IncomingEvent]
    ) -> tuple[str, list[IncomingEvent]]:
        history = await self.dws.history(batch[0].contact)
        latest_manual = self._register_manual_messages(history)
        earliest = min(item.created_at for item in batch)
        if self._human_owns_conversation(latest_manual, earliest):
            return "manual", []
        known = {item.message_id for item in batch}
        supplements = [
            item
            for item in self._history_events(
                batch[0].contact, batch[0].conversation_id, history, earliest
            )
            if item.message_id not in known
        ]
        return ("supplement", supplements) if supplements else ("fresh", [])

    async def _send_supervisor_text(
        self,
        contact: Contact,
        conversation_id: str,
        content: str,
        session_id: str,
        *,
        retry_network: bool = False,
    ) -> None:
        if self.mode == "shadow":
            LOG.info("shadow supervisor message contact=%s chars=%d", contact.alias, len(content))
            return
        send_uuid = str(uuid.uuid4())
        sent_at = datetime.now(self.settings.timezone)
        self.store.record_outgoing(
            send_uuid,
            None,
            conversation_id,
            contact.user_id,
            sent_at,
            content,
            session_id,
        )
        try:
            message_id, _ = await self.dws.send(
                contact, content, send_uuid, retry_network=retry_network
            )
        except Exception:
            with suppress(Exception):
                self.store.remove_outgoing(send_uuid)
            raise
        if message_id:
            try:
                self.store.record_outgoing(
                    send_uuid,
                    message_id,
                    conversation_id,
                    contact.user_id,
                    sent_at,
                    content,
                    session_id,
                )
            except Exception:
                LOG.exception("outgoing message-id update failed session=%s", session_id)

    async def _send_after_freshness_check(
        self,
        batch: Sequence[IncomingEvent],
        decision: Mapping[str, Any],
        changes: Sequence[ChangeEvidence],
    ) -> str:
        state, _ = await self._freshness_state(batch)
        if state == "manual":
            return "human_cooldown"
        if state == "supplement":
            return "supplement"
        attachment_path: Path | None = None
        attachment_requested = bool(changes) and decision.get("delivery") == "attachment"
        if attachment_requested:
            if any(not item.verified for item in changes):
                raise ValueError("refusing to attach unverified code changes")
            primary = changes[0]
            suffix = (primary.head_sha or "changes")[:12]
            archive_name = f"{primary.repo or 'code'}-{suffix}.zip"
            attachment_path = self.settings.state_dir / "deliveries" / archive_name
            build_change_archive(changes, attachment_path)
            state, _ = await self._freshness_state(batch)
            if state != "fresh":
                attachment_path.unlink(missing_ok=True)
                return "human_cooldown" if state == "manual" else "supplement"
        content = sanitize_text(
            render_reply(decision, changes),
            int(self.settings.raw.get("max_reply_chars", 16000)),
        )
        self._set_runtime(batch, "sending")
        if self.mode == "shadow":
            LOG.info(
                "shadow reply contact=%s action=%s chars=%d",
                batch[0].contact.alias,
                decision.get("action"),
                len(content),
            )
            if attachment_path:
                LOG.info(
                    "shadow attachment contact=%s file=%s",
                    batch[0].contact.alias,
                    attachment_path.name,
                )
                attachment_path.unlink(missing_ok=True)
            return "shadow"
        session_id = str(decision.get("session_id") or uuid.uuid4())
        try:
            if attachment_path:
                file_uuid = str(uuid.uuid4())
                file_sent_at = datetime.now(self.settings.timezone)
                self.store.record_outgoing(
                    file_uuid,
                    None,
                    batch[0].conversation_id,
                    batch[0].contact.user_id,
                    file_sent_at,
                    attachment_path.name,
                    session_id,
                )
                try:
                    file_message_id, _ = await self.dws.send_file(
                        batch[0].contact, attachment_path, file_uuid
                    )
                except Exception:
                    with suppress(Exception):
                        self.store.remove_outgoing(file_uuid)
                    raise
                if file_message_id:
                    try:
                        self.store.record_outgoing(
                            file_uuid,
                            file_message_id,
                            batch[0].conversation_id,
                            batch[0].contact.user_id,
                            file_sent_at,
                            attachment_path.name,
                            session_id,
                        )
                    except Exception:
                        LOG.exception(
                            "outgoing file message-id update failed session=%s",
                            session_id,
                        )
                # A final report is another outbound action, so check again
                # after the upload in case the other person added a request.
                state, _ = await self._freshness_state(batch)
                if state != "fresh":
                    return "human_cooldown" if state == "manual" else "supplement"
            await self._send_supervisor_text(
                batch[0].contact,
                batch[0].conversation_id,
                content,
                session_id,
                retry_network=True,
            )
        finally:
            if attachment_path:
                attachment_path.unlink(missing_ok=True)
        LOG.info("reply sent contact=%s action=%s", batch[0].contact.alias, decision.get("action"))
        return "sent"


def _validate_configuration(settings: Settings) -> list[str]:
    errors: list[str] = []
    aliases = [item.alias for item in settings.contacts]
    user_ids = [item.user_id for item in settings.contacts]
    open_ids = [item.open_dingtalk_id for item in settings.contacts]
    if not settings.contacts:
        errors.append("contacts must not be empty")
    if len(set(aliases)) != len(aliases):
        errors.append("duplicate contact alias")
    if len(set(user_ids)) != len(user_ids) or len(set(open_ids)) != len(open_ids):
        errors.append("duplicate DingTalk identity")
    if settings.self_open_id in open_ids:
        errors.append("self identity appears in listener whitelist")
    if settings.quiet_window <= 0 or settings.cooldown <= 0:
        errors.append("quiet window and cooldown must be positive")
    try:
        AgentRuntime.from_config(
            settings.raw, settings.config_path, settings.workspace_root
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"agent workflow is invalid: {exc}")
    if not settings.workspace_root.exists():
        errors.append(f"workspace_root does not exist: {settings.workspace_root}")
    return errors


async def _doctor(settings: Settings) -> int:
    checks: list[tuple[str, bool, str]] = []
    errors = _validate_configuration(settings)
    checks.append(("config", not errors, "; ".join(errors) if errors else "ok"))
    binaries = {
        "dws": str(settings.raw.get("dws", {}).get("binary", "dws")),
        "git": "git",
    }
    if not errors:
        runtime = AgentRuntime.from_config(
            settings.raw, settings.config_path, settings.workspace_root
        )
        binaries.update(runtime.required_binaries())
    for binary_name, binary in binaries.items():
        resolved = shutil.which(binary)
        checks.append((binary_name, bool(resolved), resolved or "not found"))
    if not errors and shutil.which(str(settings.raw.get("dws", {}).get("binary", "dws"))):
        contact = settings.contacts[0]
        command = [
            str(settings.raw.get("dws", {}).get("binary", "dws")),
            "event",
            "consume",
            "user_im_message_receive_o2o",
            "--user",
            contact.user_id,
            "--format",
            "ndjson",
            "--dry-run",
        ]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        checks.append(
            (
                "dws event dry-run",
                process.returncode == 0,
                (stdout or stderr).decode("utf-8", errors="replace").splitlines()[0][:200],
            )
        )
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
    return 0 if all(item[1] for item in checks) else 1


def _acquire_single_instance(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "agent.lock"
    handle = lock_path.open("a+")
    try:
        if os.name == "nt":
            if lock_path.stat().st_size == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        handle.close()
        raise RuntimeError("another dws-chat-agent instance is already running")
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


async def _run_service(
    settings: Settings, mode: str | None, *, open_dashboard: bool = False
) -> int:
    lock_handle = _acquire_single_instance(settings.state_dir)
    service = AgentService(settings, mode, open_dashboard=open_dashboard)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, service.request_stop)
    try:
        await service.run()
    finally:
        service.close()
        lock_handle.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DingTalk chat agent")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="consume DWS events")
    run_parser.add_argument("--mode", choices=("shadow", "live"))
    run_parser.add_argument(
        "--open-dashboard", action="store_true", help="open the local dashboard"
    )
    subparsers.add_parser("doctor", help="validate local runtime without sending")
    gate_parser = subparsers.add_parser("probe-gate", help="run the zero-token gate")
    gate_parser.add_argument("text")
    return parser


async def async_main(arguments: argparse.Namespace) -> int:
    try:
        settings = load_settings(arguments.config, arguments.env_file)
    except (OSError, ValueError, KeyError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.state_dir, arguments.verbose)
    if arguments.command == "doctor":
        return await _doctor(settings)
    if arguments.command == "probe-gate":
        security = settings.raw.get("security", {})
        gate = SecurityGate(
            int(security.get("max_message_chars", 12000)),
            security.get("allowed_execution_domains", []),
            security.get("allowed_reference_domains", []),
        )
        result: GateResult = gate.inspect([arguments.text])
        print(json.dumps(result.__dict__, ensure_ascii=False))
        return 0
    if arguments.command == "run":
        errors = _validate_configuration(settings)
        if errors:
            for error in errors:
                print(f"configuration error: {error}", file=sys.stderr)
            return 2
        return await _run_service(
            settings, arguments.mode, open_dashboard=arguments.open_dashboard
        )
    return 2


def main() -> int:
    return asyncio.run(async_main(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
