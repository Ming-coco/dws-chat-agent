#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as fixed_timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .runners.runtime import AgentRuntime, AgentSession
from .core import (
    MAX_IMAGES,
    MAX_IMAGE_BYTES,
    AuditStore,
    Contact,
    GateResult,
    HistoryMessage,
    IncomingEvent,
    SecurityGate,
    TaskScope,
    canonical_message_text,
    human_owns_conversation,
    parse_dws_event,
    parse_local_datetime,
    sanitize_text,
    sha256_text,
)
from .config import (
    DEFAULT_CONFIG,
    DEFAULT_ENV,
    WEB_DIR,
    Settings,
    _load_timezone,
    _read_env_file,
    _resolve_env,
    load_settings,
)
from .dashboard import AgentConfigStore, DashboardServer
from .dws import DwsClient, _find_dws_error_code
from . import sandbox


LOG = logging.getLogger("dws-chat-agent")
_IMAGE_MEDIA_ID = re.compile(r"\[图片消息\]\(mediaId=([^)\s]+)\)")
# 图片预算统一定义在 core（MAX_IMAGES/MAX_IMAGE_BYTES）；此处仅保留历史别名，
# 事件流读取上限等下游限制必须与 core 常量联动，不允许各自硬编码。
_MAX_IMAGES = MAX_IMAGES
_MAX_IMAGE_BYTES = MAX_IMAGE_BYTES
_STREAM_CARD_BIND_TIMEOUT_SECONDS = 6.0
# 活跃事项快照注入 prompt 的上限；超出部分用 task_lookup 查询，不悄悄丢失。
_TASK_SNAPSHOT_LIMIT = 8
# 事项无实质更新后的归档天数（task_memory.inactive_days 可覆盖）。
_TASK_INACTIVE_DAYS_DEFAULT = 2
# 只在群聊时注入的额外约束：群里是公开场合，与单聊的措辞尺度不同。
_GROUP_REMINDER = (
    "这是群聊，不是私聊。你的回复会显示给群里所有人，是公开可见的发言："
    "不要提及其他联系人、其他项目、内部编号或只有私下才该说的话；"
    "不要写成只对一个人说话的口吻；需要指向具体某人时用发言人的名字。"
    "群里的追问可能来自不同的人，以最新一条被 @ 的消息为准判断当前对象。"
)


def _event_consume_command(
    settings: Settings, contact: Contact, *, dry_run: bool = False
) -> list[str]:
    command = [
        str(settings.raw.get("dws", {}).get("binary", "dws")),
        "event",
        "consume",
        "user_im_message_receive_o2o",
    ]
    if contact.user_id:
        command.extend(["--user", contact.user_id])
    command.extend(["--format", "ndjson", "--ephemeral"])
    profile = settings.raw.get("dws", {}).get("profile")
    if profile:
        command.extend(["--profile", str(profile)])
    if dry_run:
        command.append("--dry-run")
    return command


def _mention_consume_command(settings: Settings, *, dry_run: bool = False) -> list[str]:
    """订阅「被 @ 的消息」。

    这个 EventKey 不接受目标参数，一条订阅就覆盖所有群；因此必须在解析时
    按 conversation_id 过滤，只保留登记过的群。@所有人 也会落进这个事件，
    所以「只登记需要的群」是唯一的防线。
    """
    command = [
        str(settings.raw.get("dws", {}).get("binary", "dws")),
        "event",
        "consume",
        "user_im_message_receive_at",
        "--flatten",
        "--format",
        "ndjson",
        "--ephemeral",
    ]
    profile = settings.raw.get("dws", {}).get("profile")
    if profile:
        command.extend(["--profile", str(profile)])
    if dry_run:
        command.append("--dry-run")
    return command


def _groups(settings: Settings) -> tuple[Contact, ...]:
    """群聊目标；同样对 SimpleNamespace 容错。"""
    return tuple(getattr(settings, "groups", ()) or ())


def _group_for_conversation(settings: Settings, conversation_id: str) -> Contact | None:
    finder = getattr(settings, "group_for_conversation", None)
    if callable(finder):
        return finder(conversation_id)
    for group in _groups(settings):
        if group.conversation_id == conversation_id:
            return group
    return None


def _targets(settings: Settings) -> tuple[Contact, ...]:
    """单聊联系人与群聊目标的并集。

    单元测试会用 SimpleNamespace 伪造 settings，所以这里必须容错，
    不能直接访问 settings.targets。
    """
    values = getattr(settings, "targets", None)
    if values is not None:
        return tuple(values)
    contacts = tuple(getattr(settings, "contacts", ()) or ())
    groups = tuple(getattr(settings, "groups", ()) or ())
    return (*contacts, *groups)


def _event_conversation_id(line: str) -> str:
    """从 @ 事件里取出会话 id，用来判断是否属于登记的群。"""
    try:
        outer = json.loads(line)
    except json.JSONDecodeError:
        return ""
    if not isinstance(outer, Mapping):
        return ""
    payload = outer.get("data")
    if isinstance(payload, str):
        # dws 某些路径把 data 再序列化成字符串，这里跟着解开一层。
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = None
    sources = [payload, outer] if isinstance(payload, Mapping) else [outer]
    for source in sources:
        for key in ("conversation_id", "conversationId", "openConversationId"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _image_resources(
    batch: Sequence[IncomingEvent],
) -> list[tuple[IncomingEvent, str]]:
    resources: list[tuple[IncomingEvent, str]] = []
    seen: set[tuple[str, str]] = set()
    for event in batch:
        for media_id in _IMAGE_MEDIA_ID.findall(event.content):
            key = (event.message_id, media_id)
            if key not in seen:
                seen.add(key)
                resources.append((event, media_id))
    return resources


def _image_suffix(path: Path) -> str:
    with path.open("rb") as stream:
        header = stream.read(12)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    raise RuntimeError("media_download_failed: unsupported image format")


def _apply_image_context(
    prompt: str, protocol: str, local_images: Sequence[Path]
) -> tuple[str, tuple[Path, ...]]:
    """按协议把下载好的本地图片接入 prompt，返回 (prompt, local_images)。

    - pi-rpc：图片由 runtime 编码为 ImageContent 随首条 prompt 原生发送，
      prompt 只说明附件已附带、可直接看图；看不到图片时如实说明，请对方
      用文字描述，不再提供单独的看图工具兜底；local_images 原样传给
      open_session。
    - codex-app-server：runtime 以 localImage 路径输入，prompt 不加说明。
    - 其余协议（claude/custom）：无法原生输入图片，把路径嵌入 prompt，
      由 agent 用 read 工具读取，返回空 local_images。
    """
    if not local_images or protocol == "codex-app-server":
        return prompt, tuple(local_images)
    image_paths = "\n".join(f"- {img.resolve()}" for img in local_images)
    if protocol == "pi-rpc":
        return (
            prompt
            + "\n\n## 图片附件\n\n对方发送的图片已作为图片附件随本条消息直接发给你，"
            "请直接查看图片内容并结合上下文理解对方意图，不要为看图调用工具。"
            "若你确实看不到图片内容，如实说明并请对方用文字描述现象。\n",
            tuple(local_images),
        )
    return (
        prompt
        + "\n\n## 图片附件\n\n以下图片文件已下载到本地：\n"
        + image_paths
        + "\n请先用 read 工具逐个读取，确认你是否能直接看到图片内容；"
        "如果 read 得到的不是有效图片内容，如实说明并请对方用文字描述现象。\n",
        (),
    )


@dataclass
class AgentResult:
    session_id: str
    reply: str | None
    exit_code: int | None
    started_at: datetime
    finished_at: datetime
    escalated: bool = False
    manual_takeover: bool = False
    supplements: list[IncomingEvent] | None = None
    consumed_supplements: list[IncomingEvent] | None = None
    error: str = ""
    # 模型显式调用 no_reply 后的成功终态：不发消息、不进 fallback。
    silent: bool = False

    @property
    def outcome(self) -> str:
        """运行的明确结果：interrupted/escalate/silent/reply/error。"""
        if self.manual_takeover:
            return "interrupted"
        if self.escalated:
            return "escalate"
        if self.silent:
            return "silent"
        if self.reply is not None:
            return "reply"
        return "error"


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
            max_chars=int(security.get("max_message_chars", 12000))
        )
        self.dws = DwsClient(settings)
        self.queues: dict[str, asyncio.Queue[IncomingEvent]] = {}
        self.workers: dict[str, asyncio.Task[None]] = {}
        self.listeners: list[asyncio.Task[None]] = []
        self.runtime_heartbeat: asyncio.Task[None] | None = None
        self.history_reconciler: asyncio.Task[None] | None = None
        self.outbox_dispatcher: asyncio.Task[None] | None = None
        self.outbox_wakeup = asyncio.Event()
        self.listener_processes: set[asyncio.subprocess.Process] = set()
        # 未登记会话（含 @所有人）只记一次，避免日志被刷屏。
        self._ignored_conversations: set[str] = set()
        self.stop_event = asyncio.Event()
        self.capacity = int(settings.raw.get("max_parallel_conversations", 2))
        self.global_slots = asyncio.Semaphore(self.capacity)
        self.runtime_path = settings.state_dir / "runtime.json"
        self.runtime_states: dict[str, dict[str, Any]] = {}
        self.streaming_card_send_uuids: dict[str, str] = {}
        # Ponytail ceiling: this lock only covers idle-check/config-swap/enqueue;
        # move config application onto the event loop if those operations grow.
        self.configuration_lock = threading.RLock()
        self.dashboard = DashboardServer(
            settings.state_dir / "audit.sqlite3",
            self.runtime_path,
            WEB_DIR / "dashboard.html",
            {item.identity: item.display_name for item in settings.targets},
            settings.timezone,
            config_store=AgentConfigStore(
                settings.config_path,
                settings.workspace_root,
                on_apply=self._apply_agent_configuration,
                is_idle=self._configuration_is_idle,
                mutation_lock=self.configuration_lock,
                contacts=[
                    {
                        "alias": item.alias,
                        "display_name": item.display_name,
                        "user_id": item.user_id,
                        "open_dingtalk_id": item.open_dingtalk_id,
                        "projects": list(item.projects),
                    }
                    for item in settings.contacts
                ],
                groups=[
                    {
                        "alias": item.alias,
                        "display_name": item.display_name,
                        "open_conversation_id": item.conversation_id,
                        "projects": list(item.projects),
                        "allowed_senders": list(item.allowed_senders),
                    }
                    for item in settings.groups
                ],
            ),
            contact_search=self._search_dingtalk_contacts,
            group_lister=self._list_dingtalk_groups,
            project_lister=self._list_projects,
            sandbox_status=self._sandbox_status,
            sandbox_refresh=self._refresh_sandboxes,
        )

    def _list_projects(self) -> list[str]:
        """workspace_root 下的一层目录，作为联系人项目下拉的候选。"""
        root = self.settings.workspace_root
        if not root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in root.iterdir()
            if entry.is_dir()
            and not entry.name.startswith(".")
            and sandbox.normalize_project_name(entry.name) is not None
            and not sandbox.is_link(entry)
        )

    def _sandbox_status(self) -> dict[str, Any]:
        return self._rebuild_sandboxes(apply=False).as_dict()

    def _refresh_sandboxes(self) -> dict[str, Any]:
        """按 config.json 的当前内容重建沙盒。

        联系人与项目列表要重启才进入运行态，所以这里刻意读盘而不是用内存快照：
        按钮的作用是「先校验并铺好目录」，重启后 agent 才能真正用上。
        """
        try:
            disk_settings = load_settings(
                self.settings.config_path, self.settings.env_path
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            payload = self._rebuild_sandboxes(apply=False).as_dict()
            payload["healthy"] = False
            payload.setdefault("errors", []).append(f"读取 config.json 失败：{exc}")
            return payload
        report = sandbox.rebuild(
            disk_settings.workspace_root,
            disk_settings.sandbox_root,
            ((target.alias, target.projects) for target in _targets(disk_settings)),
            apply=True,
        )
        payload = report.as_dict()
        running = tuple(
            (item.alias, tuple(item.projects)) for item in _targets(self.settings)
        )
        saved = tuple(
            (item.alias, tuple(item.projects)) for item in _targets(disk_settings)
        )
        if running != saved:
            payload.setdefault("notes", []).append(
                "项目分配已变更，重启服务后 agent 才会改用新的工作目录"
            )
        return payload

    def close(self) -> None:
        self.dashboard.stop()
        self.runtime_path.unlink(missing_ok=True)
        self.store.close()

    async def run(self) -> None:
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings.worktree_root.mkdir(parents=True, exist_ok=True)
        LOG.info("starting mode=%s contacts=%d", self.mode, len(self.settings.contacts))
        self._rebuild_sandboxes(apply=True)
        days = self._task_memory_inactive_days()
        if days > 0:
            archived = self.store.archive_inactive_tasks(
                datetime.now(self.settings.timezone), days
            )
            if archived:
                LOG.info("startup archived inactive tasks count=%d", archived)
        self._write_runtime()
        try:
            self.dashboard.start()
            LOG.info("dashboard ready url=%s", self.dashboard.url)
            if self.open_dashboard:
                await asyncio.to_thread(webbrowser.open, self.dashboard.url)
        except OSError as exc:
            LOG.error("dashboard failed error=%s", exc)
        await self._recover_pending_events()
        await self._reconcile_missing_events()
        self.listeners = [
            asyncio.create_task(self._listen_contact(contact), name=f"listen:{contact.alias}")
            for contact in self.settings.contacts
        ]
        if _groups(self.settings):
            self.listeners.append(
                asyncio.create_task(self._listen_mentions(), name="listen:mentions")
            )
        self.history_reconciler = asyncio.create_task(
            self._reconcile_history_loop(), name="history-reconciler"
        )
        self.outbox_dispatcher = asyncio.create_task(
            self._dispatch_outbox(), name="outbox-dispatcher"
        )
        self.runtime_heartbeat = asyncio.create_task(
            self._keep_runtime_alive(), name="runtime-heartbeat"
        )
        await self.stop_event.wait()
        LOG.info("stopping listeners gracefully")
        for task in self.listeners:
            task.cancel()
        for task in self.workers.values():
            task.cancel()
        self.history_reconciler.cancel()
        self.outbox_dispatcher.cancel()
        self.runtime_heartbeat.cancel()
        await asyncio.gather(
            *self.listeners,
            *self.workers.values(),
            self.history_reconciler,
            self.outbox_dispatcher,
            self.runtime_heartbeat,
            return_exceptions=True,
        )

    def request_stop(self) -> None:
        self.stop_event.set()

    async def _recover_pending_events(self) -> None:
        async def recover(contact: Contact) -> None:
            pending = self.store.pending_event_ids(contact.identity)
            if not pending:
                return
            try:
                history = await self.dws.history(contact)
            except Exception as exc:
                LOG.warning("queued event recovery failed contact=%s error=%s", contact.alias, exc)
                return
            for message in history:
                if message.message_id not in pending:
                    continue
                if not self._history_sender_allowed(
                    contact, message.sender_open_dingtalk_id, message.sender
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
                        sender_open_dingtalk_id=message.sender_open_dingtalk_id,
                        sender_name=message.sender,
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

        await asyncio.gather(*(recover(contact) for contact in _targets(self.settings)))

    async def _reconcile_missing_events(
        self, contacts: Sequence[Contact] | None = None
    ) -> None:
        lookback = float(
            self.settings.raw.get("event_reconcile_lookback_seconds", 7200)
        )
        not_before = datetime.now(self.settings.timezone) - timedelta(seconds=lookback)

        async def reconcile(contact: Contact) -> None:
            try:
                history = await self.dws.history(contact)
            except Exception as exc:
                LOG.warning(
                    "event history reconciliation failed contact=%s error=%s",
                    contact.alias,
                    exc,
                )
                return
            latest_self = max(
                (
                    message.created_at
                    for message in history
                    if message.sender_open_dingtalk_id == self.settings.self_open_id
                ),
                default=not_before,
            )
            recover_after = max(not_before, latest_self)
            recovered = 0
            for message in history:
                if (
                    message.sender_open_dingtalk_id != contact.open_dingtalk_id
                    or message.created_at <= recover_after
                ):
                    continue
                event = IncomingEvent(
                    event_id=f"reconcile:{message.message_id}",
                    message_id=message.message_id,
                    conversation_id=message.conversation_id,
                    contact=contact,
                    content=message.content,
                    created_at=message.created_at,
                )
                if self.store.claim_event(event):
                    self._enqueue(event)
                    recovered += 1
            if recovered:
                LOG.warning(
                    "recovered events missed by listener contact=%s count=%d",
                    contact.alias,
                    recovered,
                )

        # 群聊刻意不参与历史补齐：群历史里所有消息都在，无法区分「@ 我的」
        # 与「别人聊天」，按历史补会把闲聊当成提问。群聊只依赖 @ 事件通道。
        targets = contacts if contacts is not None else self.settings.contacts
        await asyncio.gather(*(reconcile(contact) for contact in targets))

    async def _reconcile_history_loop(self) -> None:
        interval = float(
            self.settings.raw.get("event_reconcile_interval_seconds", 60)
        )
        while not self.stop_event.is_set():
            await asyncio.sleep(interval)
            await self._reconcile_missing_events()

    async def _keep_runtime_alive(self) -> None:
        while True:
            await asyncio.sleep(5)
            self._write_runtime()

    def _configuration_is_idle(self) -> bool:
        with self.configuration_lock:
            return not self.runtime_states and all(
                queue.empty() for queue in self.queues.values()
            )

    def _search_dingtalk_contacts(self, query: str) -> list[dict[str, Any]]:
        command = [
            str(self.settings.raw.get("dws", {}).get("binary", "dws")),
            "contact",
            "user",
            "search",
            "--query",
            query,
            "--format",
            "json",
            "--timeout",
            "15",
        ]
        profile = self.settings.raw.get("dws", {}).get("profile")
        if profile:
            command.extend(["--profile", str(profile)])
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                # dws 始终输出 UTF-8；不显式指定会被 locale（中文 Windows 为 GBK）
                # 解码，遇到中文姓名直接 UnicodeDecodeError 并让 stdout 变成 None。
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("搜索超时") from exc
        if completed.returncode:
            code = _find_dws_error_code(completed.stderr) or f"exit {completed.returncode}"
            raise RuntimeError(code)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("DWS 返回了无效 JSON") from exc
        raw_results = payload.get("result", []) if isinstance(payload, Mapping) else []
        if not isinstance(raw_results, list):
            raise RuntimeError("DWS 返回了无效搜索结果")
        results = []
        for item in raw_results[:30]:
            if not isinstance(item, Mapping):
                continue
            results.append(
                {
                    "name": sanitize_text(item.get("name"), 120),
                    "nick": sanitize_text(item.get("nick"), 120),
                    "flowerName": sanitize_text(item.get("flowerName"), 120),
                    "title": sanitize_text(item.get("title"), 120),
                    "userId": str(item.get("userId") or "")[:512],
                    "openDingTalkId": str(item.get("openDingTalkId") or "")[:512],
                }
            )
        return results

    def _list_dingtalk_groups(self) -> list[dict[str, Any]]:
        """列出当前账号所在的群，供设置页挑选登记（避免手抄 cid）。"""
        command = [
            str(self.settings.raw.get("dws", {}).get("binary", "dws")),
            "chat",
            "group",
            "list-all",
            "--format",
            "json",
            "--timeout",
            "20",
        ]
        profile = self.settings.raw.get("dws", {}).get("profile")
        if profile:
            command.extend(["--profile", str(profile)])
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("获取群列表超时") from exc
        if completed.returncode:
            code = _find_dws_error_code(completed.stderr) or f"exit {completed.returncode}"
            raise RuntimeError(code)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("DWS 返回了无效 JSON") from exc
        result = payload.get("result", {}) if isinstance(payload, Mapping) else {}
        groups = result.get("groups", []) if isinstance(result, Mapping) else []
        if not isinstance(groups, list):
            raise RuntimeError("DWS 返回了无效群列表")
        registered = {group.conversation_id for group in _groups(self.settings)}
        items = []
        for item in groups:
            if not isinstance(item, Mapping):
                continue
            conversation_id = str(item.get("openConversationId") or "").strip()
            title = sanitize_text(item.get("title"), 120)
            if not conversation_id or not title:
                continue
            try:
                member_count = int(item.get("memberCount") or 0)
            except (TypeError, ValueError):
                member_count = 0
            items.append(
                {
                    "title": title,
                    "openConversationId": conversation_id,
                    "memberCount": member_count,
                    "registered": conversation_id in registered,
                }
            )
        items.sort(key=lambda entry: (-entry["memberCount"], entry["title"]))
        return items[:200]

    def _apply_agent_configuration(self, raw: Mapping[str, Any]) -> None:
        env = _read_env_file(self.settings.env_path)
        env.update(os.environ)
        contacts = raw.get("contacts", [])
        if isinstance(contacts, list) and any(
            str(item.get("open_dingtalk_id") or "") == self.settings.self_open_id
            for item in contacts
            if isinstance(item, Mapping)
        ):
            raise ValueError("self identity cannot be added to the contact whitelist")
        candidate = dict(self.settings.raw)
        candidate["agents"] = _resolve_env(raw.get("agents", {}), env)
        candidate["workflows"] = _resolve_env(raw.get("workflows", {}), env)
        candidate["dws"] = _resolve_env(raw.get("dws", {}), env)
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
        previous_dws = self.settings.raw.get("dws")
        previous_ai_tag = self.dws.ai_tag
        previous_runtime = self.agent_runtime
        try:
            self.settings.raw["agents"] = candidate["agents"]
            self.settings.raw["workflows"] = candidate["workflows"]
            self.settings.raw["dws"] = candidate["dws"]
            self.dws.ai_tag = bool(candidate["dws"].get("ai_tag", False))
            self.agent_runtime = runtime
            self._write_runtime()
        except Exception:
            self.settings.raw["agents"] = previous_agents
            self.settings.raw["workflows"] = previous_workflows
            self.settings.raw["dws"] = previous_dws
            self.dws.ai_tag = previous_ai_tag
            self.agent_runtime = previous_runtime
            raise

    def _write_runtime(self) -> None:
        payload = {
            "pid": os.getpid(),
            "mode": self.mode,
            "contacts": len(_targets(self.settings)),
            "groups": len(_groups(self.settings)),
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
        if not contact.user_id:
            await self._poll_open_id_contact(contact)
            return
        delay = 1.0
        while not self.stop_event.is_set():
            command = _event_consume_command(self.settings, contact)
            process: asyncio.subprocess.Process | None = None
            stderr_task: asyncio.Task[None] | None = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    # 同 _listen_mentions：consume 把 stdin EOF 当退出信号。
                    stdin=asyncio.subprocess.PIPE,
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

    async def _listen_mentions(self) -> None:
        """消费「被 @ 的消息」，按会话 id 路由到登记过的群。

        一个订阅覆盖所有群，所以这里必须是唯一的群入口：未登记的群、未授权
        的发言人、自己发的消息都在这里被丢掉，不进入队列也不落库。
        """
        if not _groups(self.settings):
            return
        delay = 1.0
        while not self.stop_event.is_set():
            command = _mention_consume_command(self.settings)
            process: asyncio.subprocess.Process | None = None
            stderr_task: asyncio.Task[None] | None = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    # dws consume 把 stdin EOF 当作退出信号。这里显式持有一个
                    # 不关闭的管道，避免服务被以 stdin 已关闭的方式拉起时
                    # 订阅一就绪就退出。
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self.listener_processes.add(process)
                stderr_task = asyncio.create_task(
                    self._drain_listener_stderr(process, None)
                )
                assert process.stdout is not None
                async for raw_line in process.stdout:
                    event = self._parse_mention(raw_line)
                    if event is None:
                        continue
                    if not self.store.claim_event(event) and not self.store.event_is_pending(
                        event.message_id
                    ):
                        continue
                    self._enqueue(event)
                return_code = await process.wait()
                if not self.stop_event.is_set():
                    LOG.warning("mention listener exited code=%s", return_code)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.error("mention listener error=%s", exc)
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
                        LOG.error("mention listener did not stop after SIGTERM")
            if not self.stop_event.is_set():
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    def _parse_mention(self, raw_line: bytes) -> IncomingEvent | None:
        """把一条 @ 事件归到登记的群；不认识的会话直接丢弃。"""
        line = raw_line.decode("utf-8", errors="replace")
        conversation_id = _event_conversation_id(line)
        if not conversation_id:
            return None
        group = _group_for_conversation(self.settings, conversation_id)
        if group is None:
            # @所有人 和未登记群都会到这里；只记一次，避免刷屏。
            self._note_unregistered_conversation(conversation_id)
            return None
        return parse_dws_event(
            line, group, self.settings.timezone, self.settings.self_open_id
        )

    def _note_unregistered_conversation(self, conversation_id: str) -> None:
        if conversation_id in self._ignored_conversations:
            return
        self._ignored_conversations.add(conversation_id)
        LOG.info(
            "ignoring mentions from unregistered conversation=%s", conversation_id
        )

    async def _poll_open_id_contact(self, contact: Contact) -> None:
        # ponytail: open-id-only contacts poll until DWS event consume accepts
        # openDingTalkId; replace this with a native subscription when available.
        interval = float(
            self.settings.raw.get("open_id_poll_interval_seconds", 5)
        )
        LOG.info(
            "listener ready contact=%s transport=history-poll interval=%ss",
            contact.alias,
            interval,
        )
        while not self.stop_event.is_set():
            await asyncio.sleep(interval)
            await self._reconcile_missing_events((contact,))

    async def _drain_listener_stderr(
        self, process: asyncio.subprocess.Process, contact: Contact | None
    ) -> None:
        assert process.stderr is not None
        label = contact.alias if contact is not None else "mentions"
        async for raw_line in process.stderr:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if "ready" in line.lower():
                LOG.info("listener ready target=%s", label)
            elif line:
                LOG.debug(
                    "listener status target=%s detail_hash=%s detail_length=%d",
                    label,
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
                        await self._set_processing_reactions(pending, active=True)
                        try:
                            await self._handle_batch(pending)
                        finally:
                            await self._set_processing_reactions(
                                pending, active=False
                            )
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

    async def _set_processing_reactions(
        self, batch: Sequence[IncomingEvent], *, active: bool
    ) -> None:
        dws = getattr(self, "dws", None)
        operation = getattr(
            dws, "add_emoji" if active else "remove_emoji", None
        )
        if not callable(operation):
            return
        for item in batch:
            try:
                await operation(item.conversation_id, item.message_id, "专注")
            except Exception as exc:
                LOG.warning(
                    "processing reaction failed active=%s message=%s error=%s",
                    active,
                    sha256_text(item.message_id)[:12],
                    sanitize_text(str(exc), 300),
                )

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

    def _history_sender_allowed(
        self, contact: Contact, sender_open_id: str, sender_name: str
    ) -> bool:
        """历史消息算不算该目标发来的。

        单聊只有联络人本人；群聊是群成员，但必须排除自己（自己和 agent 的
        消息走 manual/outgoing 逻辑，不能再当成新的提问）。
        """
        if sender_open_id and sender_open_id == getattr(
            self.settings, "self_open_id", ""
        ):
            return False
        if contact.is_group:
            return contact.accepts_sender(sender_open_id, sender_name)
        return sender_open_id == contact.open_dingtalk_id

    def _history_events(
        self,
        contact: Contact,
        conversation_id: str,
        history: Sequence[HistoryMessage],
        not_before: datetime,
    ) -> list[IncomingEvent]:
        # 群聊刻意返回空：群里所有消息都在历史里，历史不带「是否 @ 我」，
        # 按历史补就等于把群里的闲聊当成新提问。群聊只认 @ 事件通道。
        if contact.is_group:
            return []
        result: list[IncomingEvent] = []
        for message in history:
            if not self._history_sender_allowed(
                contact, message.sender_open_dingtalk_id, message.sender
            ):
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
                    sender_open_dingtalk_id=message.sender_open_dingtalk_id,
                    sender_name=message.sender,
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
            if message.sender_open_dingtalk_id == self.settings.self_open_id:
                role = "agent" if self.store.is_agent_outgoing(message) else "operator"
            elif self._history_sender_allowed(
                contact, message.sender_open_dingtalk_id, message.sender
            ):
                # 群聊里发言人是某个群成员，展示真实姓名而不是群名。
                role = "contact"
            else:
                continue
            execution = ""
            if role == "agent":
                execution = self.store.outgoing_session_id(message.message_id).partition(":")[0]
            messages.append(
                {
                    "role": role,
                    "sender": (
                        (message.sender or contact.display_name)
                        if role == "contact"
                        else getattr(self.settings, "self_name", "")
                    ),
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
            try:
                history = await self.dws.history(contact)
            except Exception as exc:
                LOG.warning(
                    "history unavailable; continuing without remote context "
                    "contact=%s error=%s",
                    contact.alias,
                    sanitize_text(str(exc), 300),
                )
                return self._deduplicate(batch), [], self.store.latest_manual(
                    batch[0].conversation_id
                )
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
        max_replans = int(self.settings.raw.get("max_replans", 5))
        for attempt in range(max_replans):
            batch, history, latest_manual = await self._stabilize(batch)
            recent_context, _ = self._recent_context(history, batch)
            self._set_runtime(batch, "screening", attempt=attempt + 1)
            earliest = min(item.created_at for item in batch)
            if self._human_owns_conversation(latest_manual, earliest):
                await self._close_active_streaming_card(
                    batch[0].conversation_id, "已停止自动处理，已转人工。"
                )
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
                await self._close_active_streaming_card(
                    batch[0].conversation_id, "处理已结束。"
                )
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
                refusal_session = f"code-{uuid.uuid4().hex[:12]}"
                outcome = await self._send_after_freshness_check(
                    batch,
                    gate.reply,
                    session_id=refusal_session,
                )
                if outcome == "supplement":
                    batch = await self._merge_latest(batch)
                    continue
                self._record_without_agent(
                    batch,
                    status="refused" if outcome in {"sent", "shadow"} else outcome,
                    action="refuse",
                    handled="拒绝高风险请求",
                    reason=gate.reason,
                    session_id=refusal_session,
                )
                if outcome != "delivery_pending":
                    self.store.update_event_status(
                        (item.message_id for item in batch), "processed", gate.reason
                    )
                return

            if self._agent_rate_limited(batch[0].contact):
                await self._close_active_streaming_card(
                    batch[0].conversation_id, "处理已结束。"
                )
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

            result: AgentResult | None = None
            if (
                self.agent_runtime.has_stage("front")
                and not _image_resources(batch)
            ):
                self._set_runtime(batch, "routing", attempt=attempt + 1)
                front_result = await self._run_agent(
                    batch,
                    prior_attempt,
                    front=True,
                    recent_context=recent_context,
                )
                if front_result.consumed_supplements:
                    batch = self._deduplicate(
                        [*batch, *front_result.consumed_supplements]
                    )
                if front_result.manual_takeover:
                    # 人工接管后不再新建对外卡片通知；接管事实由人工消息本身
                    # 传达，但已存在的处理中卡片必须安全收口，不能悬在会话里。
                    await self._close_active_streaming_card(
                        batch[0].conversation_id, "已停止自动处理，已转人工。"
                    )
                    self._record_agent_result(
                        batch, front_result, status="human_cooldown"
                    )
                    self.store.update_event_status(
                        (item.message_id for item in batch),
                        "skipped",
                        "manual_takeover",
                    )
                    return
                if front_result.supplements:
                    batch = self._deduplicate([*batch, *front_result.supplements])
                    prior_attempt = "前台收到补充消息，已按完整上下文重新处理。"
                    await asyncio.sleep(self.settings.quiet_window)
                    continue
                if front_result.silent:
                    # 前台判定无需回复：成功终态，不进 worker、不发送、不通知。
                    await self._finish_silent(batch, front_result)
                    return
                if front_result.reply is not None and not front_result.escalated:
                    result = front_result
                else:
                    prior_attempt = (
                        "前台已升级，由执行 Agent 处理完整请求。"
                        if front_result.escalated
                        else f"前台失败：{front_result.error}"
                    )

            if result is None:
                self._set_runtime(batch, "worker", attempt=attempt + 1)
                result = await self._run_worker_with_fallback(
                    batch,
                    prior_attempt,
                    recent_context=recent_context,
                )
                if result.consumed_supplements:
                    batch = self._deduplicate(
                        [*batch, *result.consumed_supplements]
                    )

            if result.manual_takeover:
                await self._close_active_streaming_card(
                    batch[0].conversation_id, "已停止自动处理，已转人工。"
                )
                self._record_agent_result(batch, result, status="human_cooldown")
                self.store.update_event_status(
                    (item.message_id for item in batch), "skipped", "manual_takeover"
                )
                return
            if result.supplements:
                self._record_agent_result(batch, result, status="stale_supplement")
                batch = self._deduplicate([*batch, *result.supplements])
                prior_attempt = "对方补充了消息，按新的完整上下文继续。"
                await asyncio.sleep(self.settings.quiet_window)
                continue
            if result.silent:
                # 模型显式选择沉默：成功终态，不触发兜底回复、不写发送队列。
                await self._finish_silent(batch, result)
                return
            if result.reply is None:
                # 失败要通知联系人：卡片在此时点才创建（见 _ensure_streaming_card）。
                stream_card_id = await self._ensure_streaming_card(
                    batch, result.session_id
                )
                await self._safe_update_streaming_card(
                    stream_card_id, "处理失败，请稍后重试或转人工处理。", 5
                )
                self._record_agent_result(batch, result, status="error")
                self.store.update_event_status(
                    (item.message_id for item in batch), "error", result.error[:200]
                )
                return

            self._set_runtime(batch, "freshness", attempt=attempt + 1)
            # 回复路径不在决策点建卡：最终新鲜度检查在投递派发时进行，
            # 卡片只能在检查通过后创建（见 _dispatch_due_delivery），否则
            # 补充消息或人工接管取消投递时会把处理中卡片留在会话里。
            try:
                outcome = await self._send_after_freshness_check(
                    batch,
                    result.reply,
                    session_id=result.session_id,
                )
            except Exception as exc:
                await self._close_active_streaming_card(
                    batch[0].conversation_id, "结果发送失败，请稍后重试。"
                )
                result.error = f"send_failed: {sanitize_text(str(exc), 500)}"
                self._record_agent_result(batch, result, status="error")
                self.store.update_event_status(
                    (item.message_id for item in batch), "error", result.error
                )
                return
            self._record_agent_result(batch, result, status=outcome)
            if outcome != "delivery_pending":
                self.store.update_event_status(
                    (item.message_id for item in batch), "processed", outcome
                )
            return

        stream_card_id = await self._ensure_streaming_card(
            batch, f"code-{uuid.uuid4().hex[:12]}"
        )
        await self._safe_update_streaming_card(
            stream_card_id, "补充消息过多，本轮已停止自动处理。", 5
        )
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
        contact_count, global_count = self.store.agent_run_counts_since(
            since, contact.identity
        )
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

    async def _ensure_streaming_card(
        self, batch: Sequence[IncomingEvent], session_id: str
    ) -> str:
        """失败/过多补充等"需要通知联系人"的终态点创建流式卡片。

        回复路径不再走这里：回复的卡片由 _dispatch_due_delivery 在最终
        新鲜度检查通过后创建，否则补充消息或人工接管取消投递时会留下
        处理中卡片（silent 更是要求决策前零对外输出）。ack 文案只作为
        卡片初始内容，随即被最终内容覆盖。运行内的进度/思考不对外展示
        （begin_response 展示控制是后续独立工作）。创建失败返回空串，
        交付回退纯文本。
        """
        if not getattr(getattr(self, "dws", None), "streaming_cards", False):
            return ""
        content = self.agent_runtime.render_auto_message(
            "ack",
            {
                "request_count": len(batch),
                "contact_name": batch[0].contact.display_name,
                "contact_alias": batch[0].contact.alias,
                "self_name": self.settings.self_name,
            },
        ) or "正在处理。"
        try:
            return await self._start_streaming_card(
                batch[0].contact,
                batch[0].conversation_id,
                content,
                session_id,
            )
        except Exception as exc:
            LOG.warning(
                "streaming card start failed contact=%s error=%s",
                batch[0].contact.alias,
                sanitize_text(str(exc), 300),
            )
            return ""

    async def _close_active_streaming_card(
        self, conversation_id: str, content: str
    ) -> None:
        """把会话中仍处于处理态的卡片安全收口成终止态；无卡时是空操作。

        卡片正常只在投递派发（最终新鲜度检查之后）或失败通知点创建并随即
        收口；仍会残留处理中卡片的路径是投递重试被补充/人工接管取消。这些
        终态（silent、人工接管、无 agent 处理的跳过路径）必须把残留卡片
        关闭，不能让联系人看到永远"处理中"的卡片。
        """
        active = self.store.active_streaming_card(conversation_id)
        if not active:
            return
        biz_id, send_uuid = active
        # 进程重启或直写数据库时内存映射里没有这张卡；先补上映射，保证
        # DingTalk 卡片关闭后 store 里的卡片状态也收敛到终止态。
        card_send_uuids = getattr(self, "streaming_card_send_uuids", None)
        if card_send_uuids is None:
            card_send_uuids = self.streaming_card_send_uuids = {}
        card_send_uuids.setdefault(biz_id, send_uuid)
        await self._safe_update_streaming_card(biz_id, content, 5)

    def _record_without_agent(
        self,
        batch: Sequence[IncomingEvent],
        *,
        status: str,
        action: str,
        handled: str,
        reason: str,
        session_id: str = "",
    ) -> None:
        now = datetime.now(self.settings.timezone)
        self.store.record_run(
            session_id=session_id or f"code-{uuid.uuid4().hex[:12]}",
            conversation_id=batch[0].conversation_id,
            contact=batch[0].contact,
            started_at=now,
            finished_at=now,
            action=action,
            status=status,
            handled=handled,
            reason=reason,
            codex_exit_code=None,
            request_preview=self._request_preview(batch),
        )

    async def _finish_silent(
        self,
        batch: Sequence[IncomingEvent],
        result: AgentResult,
    ) -> None:
        """落地 silent 终态：消费输入、审计留痕，不发消息、不进 outbox。

        回复路径的卡片在投递派发时才创建，silent 决策前正常不存在卡片；
        若会话仍残留处理中卡片（上次投递重试被补充取消），这里把它安全
        收口，除此之外对联系人零输出。
        """
        await self._close_active_streaming_card(
            batch[0].conversation_id, "处理已结束。"
        )
        self._record_agent_result(batch, result, status="silent")
        self.store.update_event_status(
            (item.message_id for item in batch), "processed", "silent"
        )

    def _record_agent_result(
        self,
        batch: Sequence[IncomingEvent],
        result: AgentResult,
        *,
        status: str,
    ) -> None:
        self.store.record_run(
            session_id=result.session_id,
            conversation_id=batch[0].conversation_id,
            contact=batch[0].contact,
            started_at=result.started_at,
            finished_at=result.finished_at,
            action=(
                "reply"
                if result.reply
                else ("no_reply" if result.silent else "handoff")
            ),
            status=status,
            handled=sanitize_text(result.reply, 1000),
            reason=result.error,
            codex_exit_code=result.exit_code,
            request_preview=self._request_preview(batch),
        )

    def _task_memory_inactive_days(self) -> int:
        value = int(
            self.settings.raw.get("task_memory", {}).get(
                "inactive_days", _TASK_INACTIVE_DAYS_DEFAULT
            )
        )
        # <=0 视为关闭自动归档（事项继续留在活跃快照里）。
        return value

    def _rebuild_sandboxes(self, *, apply: bool) -> sandbox.SandboxReport:
        """按联系人/群的项目列表对齐沙盒目录；启动和手动刷新共用。"""
        report = sandbox.rebuild(
            self.settings.workspace_root,
            self.settings.sandbox_root,
            ((target.alias, target.projects) for target in _targets(self.settings)),
            apply=apply,
        )
        if report.errors:
            for error in report.errors:
                LOG.warning("sandbox issue: %s", error)
        if apply:
            for note in report.notes:
                LOG.warning("sandbox note: %s", note)
            LOG.info(
                "sandbox ready root=%s contacts=%d errors=%d",
                report.root,
                len(report.contacts),
                len(report.errors),
            )
        return report

    def _authoritative_contact(self, contact: Contact) -> Contact:
        """批量事件里的联系人可能来自历史恢复，这里以配置为准补全项目列表。"""
        finder = getattr(self.settings, "contact_for", None)
        if finder is None:
            return contact
        return finder(contact.identity) or contact

    def _contact_projects(self, contact: Contact) -> tuple[str, ...]:
        return tuple(getattr(self._authoritative_contact(contact), "projects", ()) or ())

    def _contact_workspace(self, contact: Contact) -> Path:
        """该联系人 agent 的工作目录：沙盒里挂载的主项目。"""
        resolver = getattr(self.settings, "workspace_for", None)
        if resolver is None:
            return Path(str(getattr(self.settings, "workspace_root", ".")))
        return resolver(self._authoritative_contact(contact))

    def _active_tasks_snapshot(
        self, contact: Contact, conversation_id: str
    ) -> str:
        """构建活跃事项快照；加载前先做本地幂等归档。"""
        days = self._task_memory_inactive_days()
        if days > 0:
            archived = self.store.archive_inactive_tasks(
                datetime.now(self.settings.timezone), days
            )
            if archived:
                LOG.info("archived inactive tasks count=%d days=%d", archived, days)
        scope = TaskScope(
            self_id=self.settings.self_open_id,
            contact_id=contact.identity,
            conversation_id=conversation_id,
        )
        tasks = self.store.active_tasks(scope, limit=_TASK_SNAPSHOT_LIMIT)
        return json.dumps(tasks, ensure_ascii=False, indent=2)

    def _task_context_for(
        self,
        batch: Sequence[IncomingEvent],
        consumed: Iterable[IncomingEvent],
        run_id: str,
    ) -> dict[str, Any]:
        """构建事项工具上下文：message_times 是本运行已覆盖消息的
        id -> 时间戳映射。

        consumed 是已 steer 进当前会话的补充消息，并入映射后模型可以
        引用它们作证据。映射是证据 ID 的唯一校验集与时间戳来源：
        未覆盖的 ID 会被拒绝，引用旧消息的时间就取旧消息的时间。
        """
        covered = [*batch, *consumed]
        return {
            "db_path": str(self.store.path),
            "self_id": self.settings.self_open_id,
            "contact_id": batch[0].contact.identity,
            "conversation_id": batch[0].conversation_id,
            "run_id": run_id,
            "message_times": {
                item.message_id: item.created_at.isoformat()
                for item in covered
            },
        }

    def _refresh_task_context(
        self,
        session: "AgentSession",
        batch: Sequence[IncomingEvent],
        consumed: Iterable[IncomingEvent],
    ) -> None:
        """steer 成功后原子重写上下文文件，事项更新改用最新实际证据。

        pi 进程的环境变量无法在启动后修改，所以 prepare() 把上下文落到
        session 目录的 task-context.json，task_tools.py 每次调用优先读它。
        """
        path = getattr(
            getattr(session, "prepared", None), "task_context_path", None
        )
        if path is None:
            return
        context = self._task_context_for(batch, consumed, session.session_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(context, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, path)

    def _prompt_variables(
        self,
        stage: str,
        session_id: str,
        batch: Sequence[IncomingEvent],
        prior_attempt: str,
        recent_context: Sequence[Mapping[str, str]],
    ) -> dict[str, Any]:
        contact = batch[0].contact
        messages = [
            {
                "time": item.created_at.isoformat(),
                "sender": item.sender_label,
                "role": "contact",
                "conversation_id": item.conversation_id,
                "message_id": item.message_id,
                "text": item.content,
            }
            for item in batch
        ]
        return {
            "self_name": self.settings.self_name,
            "agent_name": self.agent_runtime.profile_name(stage),
            "worker_name": self.agent_runtime.profile_name("worker"),
            "contact_name": contact.display_name,
            "contact_alias": contact.alias,
            "conversation_kind": "群聊" if contact.is_group else "单聊",
            "group_reminder": _GROUP_REMINDER if contact.is_group else "",
            "contact_user_id": contact.user_id,
            "contact_open_dingtalk_id": contact.open_dingtalk_id,
            "session_id": session_id,
            "prior_attempt": prior_attempt or "无",
            "project_root": self._contact_workspace(contact),
            "projects_json": json.dumps(
                list(self._contact_projects(contact)), ensure_ascii=False
            ),
            "recent_context_json": json.dumps(
                list(recent_context), ensure_ascii=False, indent=2
            ),
            "current_messages_json": json.dumps(
                messages, ensure_ascii=False, indent=2
            ),
            "active_tasks_json": self._active_tasks_snapshot(
                contact, batch[0].conversation_id
            ),
            "now": datetime.now(self.settings.timezone).strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def _run_worker_with_fallback(
        self,
        batch: Sequence[IncomingEvent],
        prior_attempt: str,
        *,
        recent_context: Sequence[Mapping[str, str]] = (),
    ) -> AgentResult:
        """按主 worker + fallbacks 链执行；无回复视为该 agent 失败，切换链上下一个。"""
        result = await self._run_agent(
            batch,
            prior_attempt,
            recent_context=recent_context,
        )
        fallbacks = self.agent_runtime._stage("worker").profile.fallbacks
        for index, agent_name in enumerate(fallbacks, start=1):
            if (
                result.reply is not None
                or result.silent
                or result.manual_takeover
                or result.supplements
            ):
                # silent 与正常回复同为成功终态，不能触发 fallback 兜底发送。
                return result
            LOG.warning(
                "worker fallback engaged agent=%s attempt=%d/%d error=%s",
                agent_name,
                index,
                len(fallbacks),
                result.error[:200],
            )
            result = await self._run_agent(
                batch,
                prior_attempt,
                recent_context=recent_context,
                agent_override=agent_name,
            )
        return result

    async def _run_agent(
        self,
        batch: Sequence[IncomingEvent],
        prior_attempt: str,
        *,
        front: bool = False,
        recent_context: Sequence[Mapping[str, str]] = (),
        agent_override: str | None = None,
    ) -> AgentResult:
        stage = "front" if front else "worker"
        profile = re.sub(
            r"[^a-z0-9]+",
            "-",
            (
                agent_override
                or self.agent_runtime.profile_name(stage)
            ).lower(),
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
            ),
        )
        try:
            stage_profile = self.agent_runtime.resolve_stage(stage, agent_override)
            local_images = (
                () if front else await self._download_images(batch, session_dir)
            )
            has_images = bool(local_images)
            # pi-rpc 原生附带图片（返回的 local_images 保持非空，由 runtime
            # 编码为 ImageContent）；其余非 codex 协议只嵌路径进 prompt。
            prompt, local_images = _apply_image_context(
                prompt, stage_profile.profile.protocol, local_images
            )
            # 有图片时使用多模态模型
            model_override: str | None = None
            if has_images and stage_profile.profile.protocol == "pi-rpc":
                multimodal_model = stage_profile.profile.options.get("multimodal_model")
                if multimodal_model:
                    model_override = str(multimodal_model)
            # 事项工具上下文只注入 pi-rpc：作用域隔离由后端强制，重放按
            # (run_id, tool_call_id) 幂等；其他协议不提供事项工具。
            task_context = (
                self._task_context_for(batch, (), session_id)
                if stage_profile.profile.protocol == "pi-rpc"
                else None
            )
            workspace = self._contact_workspace(batch[0].contact)
            session = self.agent_runtime.open_session(
                stage, session_id, prompt, session_dir, local_images,
                model_override=model_override,
                agent_override=agent_override,
                task_context=task_context,
                workspace=workspace,
            )
            if task_context is not None:
                # 写入租约先于会话启动登记：task_tools.py 直连审计库的
                # 事务在拿到 SQLite 写锁后仍会校验租约，接管/取消后排队
                # 等锁的过期子进程写入会被拒绝，而不是继续落地变更。
                self.store.register_task_run(
                    session_id, datetime.now(self.settings.timezone)
                )
        except (OSError, RuntimeError, ValueError) as exc:
            shutil.rmtree(session_dir, ignore_errors=True)
            return AgentResult(
                session_id,
                None,
                None,
                started_at,
                datetime.now(self.settings.timezone),
                error=f"{type(exc).__name__}: {exc}",
            )
        supplements: dict[str, IncomingEvent] = {}
        consumed: dict[str, IncomingEvent] = {}

        def revoke_run_lease(reason: str) -> None:
            # 幂等：首次吊销的原因与时间保留，后续调用是 no-op。
            if task_context is not None:
                self.store.revoke_task_run(
                    session_id, reason, datetime.now(self.settings.timezone)
                )

        # 失败保留 session 现场（prompt.md/result.txt/stderr.log 供排障），仅成功路径清理
        session_dir_keep = True
        try:
            await session.start()
            interval = float(self.settings.raw.get("monitor_interval_seconds", 5))
            timeout = session.prepared.timeout_seconds
            now_monotonic = asyncio.get_running_loop().time()
            deadline = now_monotonic + timeout if timeout > 0 else None
            next_history_at = now_monotonic + interval
            known_ids = {item.message_id for item in batch}
            earliest = min(item.created_at for item in batch)
            # 运行内不对外展示进度/思考：卡片要等非 silent 结果决定后才创建。
            while not session.done:
                self._touch_runtime(batch[0].conversation_id)
                remaining = (
                    deadline - asyncio.get_running_loop().time()
                    if deadline is not None
                    else None
                )
                if remaining is not None and remaining <= 0:
                    revoke_run_lease("agent_timeout")
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
                    wait_timeout = max(
                        0.0, next_history_at - asyncio.get_running_loop().time()
                    )
                    if remaining is not None:
                        wait_timeout = min(wait_timeout, remaining)
                    await session.wait_for_update(timeout=wait_timeout)
                except TimeoutError:
                    pass
                if session.done:
                    break
                now_monotonic = asyncio.get_running_loop().time()
                if now_monotonic < next_history_at:
                    continue
                next_history_at = now_monotonic + interval
                try:
                    history = await self.dws.history(batch[0].contact)
                except Exception as exc:
                    LOG.warning(
                        "freshness poll failed session=%s error=%s", session_id, exc
                    )
                    continue
                latest_manual = self._register_manual_messages(history)
                if self._human_owns_conversation(latest_manual, started_at):
                    # 人工接管：先吊销写入租约再终止会话，被接管运行的
                    # 子进程（含排队等锁的）不能再落地事项变更，也不批量
                    # 改写事项状态。
                    revoke_run_lease("manual_takeover")
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
                            "conversation_kind": (
                                "群聊" if batch[0].contact.is_group else "单聊"
                            ),
                        }
                    )
                    steered = (
                        not _image_resources(new_supplements)
                        and self.agent_runtime.supplement_strategy == "steer"
                        and await session.steer(steer_prompt)
                    )
                    if steered:
                        for item in new_supplements:
                            consumed[item.message_id] = item
                            supplements.pop(item.message_id, None)
                        # 补充消息已进入会话：事项工具上下文改用最新实际
                        # 证据（含补充消息的 ID 与时间），而不是启动快照。
                        self._refresh_task_context(
                            session, batch, consumed.values()
                        )
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
            if session.silent:
                # no_reply 是模型的显式控制动作且已成功执行：即使其后出现
                # 提供方噪声错误，也不能让错误路径触发兜底发送。
                session_dir_keep = False
                return AgentResult(
                    session_id,
                    None,
                    session.exit_code if session.exit_code is not None else 0,
                    started_at,
                    datetime.now(self.settings.timezone),
                    silent=True,
                    consumed_supplements=sorted(
                        consumed.values(), key=lambda item: item.created_at
                    ),
                )
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
            session_dir_keep = False
            return AgentResult(
                session_id,
                session.result(),
                session.exit_code if session.exit_code is not None else 0,
                started_at,
                datetime.now(self.settings.timezone),
                escalated=session.escalated,
                consumed_supplements=sorted(
                    consumed.values(), key=lambda item: item.created_at
                ),
            )
        except asyncio.CancelledError:
            # 取消（含服务关停）：先吊销租约再终止会话，防止孤儿子进程
            # 在会话取消后仍落地事项写入。
            revoke_run_lease("run_cancelled")
            await session.abort()
            raise
        except (OSError, RuntimeError, ValueError) as exc:
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
            # 会话收尾兜底吊销租约（幂等）：成功/失败/静默路径同样不允许
            # 迟到的子进程写入；此前已按具体原因吊销时这里是 no-op。
            revoke_run_lease("run_finished")
            await session.close()
            if session_dir_keep:
                LOG.warning(
                    "agent run failed, kept session dir for debugging session=%s dir=%s",
                    session_id,
                    session_dir,
                )
            else:
                shutil.rmtree(session_dir, ignore_errors=True)

    async def _download_images(
        self, batch: Sequence[IncomingEvent], session_dir: Path
    ) -> tuple[Path, ...]:
        resources = _image_resources(batch)
        if len(resources) > _MAX_IMAGES:
            raise RuntimeError(
                f"media_download_failed: at most {_MAX_IMAGES} images are supported"
            )
        images: list[Path] = []
        for index, (event, media_id) in enumerate(resources, 1):
            downloaded = session_dir / f"image-{index}.download"
            await self.dws.download_media(
                media_id, event.message_id, event.conversation_id, downloaded
            )
            if downloaded.stat().st_size > _MAX_IMAGE_BYTES:
                raise RuntimeError(
                    f"media_download_failed: image exceeds {_MAX_IMAGE_BYTES} bytes"
                )
            image = downloaded.with_suffix(_image_suffix(downloaded))
            downloaded.replace(image)
            images.append(image)
        return tuple(images)

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
            LOG.info(
                "shadow supervisor message contact=%s chars=%d",
                contact.alias,
                len(content),
            )
            return
        send_uuid = str(uuid.uuid4())
        sent_at = datetime.now(self.settings.timezone)
        self.store.record_outgoing(
            send_uuid,
            None,
            conversation_id,
            contact.identity,
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
                    contact.identity,
                    sent_at,
                    content,
                    session_id,
                )
            except Exception:
                LOG.exception(
                    "outgoing message-id update failed session=%s", session_id
                )

    async def _start_streaming_card(
        self,
        contact: Contact,
        conversation_id: str,
        content: str,
        session_id: str,
    ) -> str:
        if self.mode == "shadow":
            LOG.info(
                "shadow streaming card contact=%s chars=%d",
                contact.alias,
                len(content),
            )
            return ""
        active_card = self.store.active_streaming_card(conversation_id)
        if active_card:
            biz_id, send_uuid = active_card
            card_send_uuids = getattr(self, "streaming_card_send_uuids", None)
            if card_send_uuids is None:
                card_send_uuids = self.streaming_card_send_uuids = {}
            card_send_uuids[biz_id] = send_uuid
            await self._update_streaming_card(biz_id, content, 2)
            return biz_id
        try:
            async with asyncio.timeout(_STREAM_CARD_BIND_TIMEOUT_SECONDS):
                baseline = {
                    message.message_id
                    for message in await self.dws.history(contact)
                    if message.conversation_id == conversation_id
                    and message.sender_open_dingtalk_id
                    == self.settings.self_open_id
                }
        except Exception as exc:
            LOG.warning(
                "streaming card baseline failed contact=%s error=%s",
                contact.alias,
                sanitize_text(str(exc), 300),
            )
            return ""
        sent_at = datetime.now(self.settings.timezone)
        send_uuid = str(uuid.uuid4())
        marked_content = f"{content}\n\n处理编号：{send_uuid[:8]}"
        biz_id, message_id, _ = await self.dws.send_card(contact)
        self.store.record_outgoing(
            send_uuid,
            message_id,
            conversation_id,
            contact.identity,
            sent_at,
            marked_content,
            session_id,
            card_biz_id=biz_id,
            card_flow_status=2,
        )
        card_send_uuids = getattr(self, "streaming_card_send_uuids", None)
        if card_send_uuids is None:
            card_send_uuids = self.streaming_card_send_uuids = {}
        card_send_uuids[biz_id] = send_uuid
        try:
            await self._update_streaming_card(biz_id, marked_content, 2)
        except Exception:
            await self._safe_update_streaming_card(
                biz_id, "卡片初始化失败，请等待普通文本回复。", 5
            )
            raise
        if message_id:
            await self._safe_update_streaming_card(biz_id, content, 2)
            return biz_id
        new_messages: dict[str, HistoryMessage] = {}
        try:
            async with asyncio.timeout(_STREAM_CARD_BIND_TIMEOUT_SECONDS):
                for delay in (0.25, 0.5, 1.0, 2.0):
                    await asyncio.sleep(delay)
                    history = await self.dws.history(contact)
                    new_messages = {
                        message.message_id: message
                        for message in history
                        if message.message_id not in baseline
                        and message.conversation_id == conversation_id
                        and message.sender_open_dingtalk_id
                        == self.settings.self_open_id
                    }
                    marked_candidates = [
                        message
                        for message in new_messages.values()
                        if canonical_message_text(message.content)
                        == canonical_message_text(marked_content)
                    ]
                    if (
                        len(marked_candidates) == 1
                        and self.store.bind_outgoing_id(
                            send_uuid, marked_candidates[0].message_id
                        )
                    ):
                        self.store.remove_manual(marked_candidates[0].message_id)
                        await self._safe_update_streaming_card(
                            biz_id, content, 2
                        )
                        return biz_id
        except Exception as exc:
            LOG.warning(
                "streaming card message-id reconciliation failed session=%s error=%s",
                session_id,
                sanitize_text(str(exc), 300),
            )
        await self._safe_update_streaming_card(
            biz_id, "卡片识别失败，请等待普通文本回复。", 5
        )
        LOG.warning(
            "streaming card message-id ambiguous session=%s new_self_messages=%d",
            session_id,
            len(new_messages),
        )
        return ""

    async def _start_delivery_card(
        self,
        contact: Contact,
        conversation_id: str,
        content: str,
        session_id: str,
    ) -> str:
        """投递时点创建（或复用）承载最终回复的流式卡片。

        只在 _dispatch_due_delivery 的最终新鲜度检查通过后调用；初始内容
        直接用最终回复（含消息编号标记以便回绑 message_id）。创建失败返回
        空串，投递回退纯文本。
        """
        try:
            return await self._start_streaming_card(
                contact, conversation_id, content, session_id
            )
        except Exception as exc:
            LOG.warning(
                "streaming card start failed contact=%s error=%s",
                contact.alias,
                sanitize_text(str(exc), 300),
            )
            return ""

    async def _update_streaming_card(
        self, biz_id: str, content: str, flow_status: int
    ) -> None:
        card_send_uuids = getattr(self, "streaming_card_send_uuids", {})
        try:
            await self.dws.update_card(biz_id, content, flow_status)
            send_uuid = card_send_uuids.get(biz_id)
            if send_uuid:
                self.store.update_streaming_card(send_uuid, content, flow_status)
        finally:
            if flow_status in {3, 5}:
                card_send_uuids.pop(biz_id, None)

    async def _safe_update_streaming_card(
        self, biz_id: str, content: str, flow_status: int
    ) -> None:
        if not biz_id or getattr(self, "mode", "live") == "shadow":
            return
        try:
            await self._update_streaming_card(biz_id, content, flow_status)
        except Exception as exc:
            LOG.warning(
                "streaming card update failed status=%d error=%s",
                flow_status,
                sanitize_text(str(exc), 300),
            )

    async def _send_after_freshness_check(
        self,
        batch: Sequence[IncomingEvent],
        reply: str,
        *,
        session_id: str = "",
    ) -> str:
        """把回复写入发送队列；真正的最终新鲜度检查在投递派发时进行。

        这里不创建流式卡片：卡片由 _dispatch_due_delivery 在人工接管与
        补充消息检查都通过之后创建，保证任何取消路径都不留处理中卡片。
        """
        content = sanitize_text(
            reply,
            int(self.settings.raw.get("max_reply_chars", 16000)),
        )
        if not content:
            raise ValueError("refusing to send an empty reply")
        self._set_runtime(batch, "sending")
        if self.mode == "shadow":
            LOG.info(
                "shadow reply contact=%s chars=%d",
                batch[0].contact.alias,
                len(content),
            )
            return "shadow"
        outgoing_session = session_id or str(uuid.uuid4())
        self.store.enqueue_delivery(
            str(uuid.uuid4()),
            min(batch, key=lambda item: item.created_at),
            content,
            outgoing_session,
            event_ids=[item.message_id for item in batch],
        )
        self.outbox_wakeup.set()
        LOG.info("reply queued contact=%s", batch[0].contact.alias)
        return "delivery_pending"

    async def _dispatch_outbox(self) -> None:
        LOG.info("outbox dispatcher ready")
        while not self.stop_event.is_set():
            if await self._dispatch_due_delivery():
                continue
            self.outbox_wakeup.clear()
            try:
                await asyncio.wait_for(self.outbox_wakeup.wait(), timeout=2)
            except TimeoutError:
                pass

    async def _dispatch_due_delivery(self) -> bool:
        now = datetime.now(self.settings.timezone)
        row = self.store.claim_due_delivery(now)
        if row is None:
            return False
        send_uuid = str(row["send_uuid"])
        contact = next(
            (
                item
                for item in _targets(self.settings)
                if item.identity == row["contact_user_id"]
            ),
            None,
        )
        if contact is None:
            await self._safe_update_streaming_card(
                str(row["card_biz_id"] or ""), "处理已结束。", 5
            )
            self.store.mark_delivery_cancelled(
                send_uuid, "error", "delivery_contact_not_configured"
            )
            return True
        try:
            history = await self.dws.history(contact)
            latest_manual = self._register_manual_messages(history)
            source_received_at = datetime.fromisoformat(row["source_received_at"])
            if self._human_owns_conversation(latest_manual, source_received_at):
                await self._safe_update_streaming_card(
                    str(row["card_biz_id"] or ""),
                    "已停止自动处理，已转人工。",
                    5,
                )
                self.store.mark_delivery_cancelled(
                    send_uuid, "human_cooldown", "manual_takeover"
                )
                return True
            known = set(json.loads(row["event_ids_json"] or "[]"))
            supplements = [
                item
                for item in self._history_events(
                    contact,
                    str(row["conversation_id"]),
                    history,
                    source_received_at,
                )
                if item.message_id not in known
            ]
            if supplements:
                self.store.mark_delivery_cancelled(
                    send_uuid, "stale_supplement", "new_message_before_delivery"
                )
                # 已创建的卡片（如上次网络重试后留下的）保持处理态，交给
                # 下一轮处理复用或在其终态收口；这里不新建也不重复关闭。
                for item in supplements:
                    if self.store.claim_event(item) or self.store.event_is_pending(
                        item.message_id
                    ):
                        self._enqueue(item)
                return True

            content = str(row["payload_text"])
            message_id = ""
            card_flow_status: int | None = None
            card_biz_id = str(row["card_biz_id"] or "")
            if not card_biz_id and getattr(self.dws, "streaming_cards", False):
                # 卡片是投递的一部分：人工接管与补充消息的最终检查都已
                # 通过，此刻才创建；创建后绑定投递行，网络重试复用同卡。
                card_biz_id = await self._start_delivery_card(
                    contact,
                    str(row["conversation_id"]),
                    content,
                    str(row["session_id"]),
                )
                if card_biz_id:
                    self.store.bind_delivery_card(send_uuid, card_biz_id)
            if card_biz_id:
                try:
                    await self._update_streaming_card(card_biz_id, content, 3)
                    card_flow_status = 3
                except Exception as exc:
                    LOG.warning(
                        "streaming card final update failed; falling back to text "
                        "session=%s error=%s",
                        row["session_id"],
                        sanitize_text(str(exc), 300),
                    )
                    message_id, _ = await self.dws.send(
                        contact, content, send_uuid, retry_network=True
                    )
                    card_flow_status = 5
            else:
                message_id, _ = await self.dws.send(
                    contact, content, send_uuid, retry_network=True
                )
            self.store.mark_delivery_sent(
                send_uuid,
                message_id,
                card_flow_status=card_flow_status,
            )
            LOG.info("reply sent contact=%s", contact.alias)
        except Exception as exc:
            delay = min(2 ** int(row["attempt_count"]), 60)
            self.store.mark_delivery_retry(
                send_uuid,
                f"{type(exc).__name__}: {exc}",
                now + timedelta(seconds=delay),
            )
            LOG.warning(
                "delivery deferred contact=%s error=%s",
                contact.alias,
                sanitize_text(str(exc), 300),
            )
        return True


def _validate_configuration(settings: Settings) -> list[str]:
    errors: list[str] = []
    aliases = [item.alias for item in _targets(settings)]
    user_ids = [item.user_id for item in settings.contacts if item.user_id]
    open_ids = [item.open_dingtalk_id for item in settings.contacts]
    conversation_ids = [item.conversation_id for item in _groups(settings)]
    # 群聊也可以单独使用：只登记群、不登记单聊是合法配置。
    if not _targets(settings):
        errors.append("contacts and groups must not both be empty")
    # 沙盒目录按 alias 命名，群和联系人必须共享同一个命名空间。
    if len(set(aliases)) != len(aliases):
        errors.append("duplicate contact/group alias")
    if len(set(user_ids)) != len(user_ids) or len(set(open_ids)) != len(open_ids):
        errors.append("duplicate DingTalk identity")
    if len(set(conversation_ids)) != len(conversation_ids):
        errors.append("duplicate group conversation id")
    if any(not item.conversation_id for item in _groups(settings)):
        errors.append("group entry is missing open_conversation_id")
    # 群和联系人共用 alias 命名空间（沙盒目录按 alias 建），跨两张表也要查重。
    contact_aliases = {item.alias for item in _targets(settings) if not item.is_group}
    if contact_aliases & {item.alias for item in _groups(settings)}:
        errors.append("group alias collides with a contact alias")
    if settings.self_open_id in open_ids:
        errors.append("self identity appears in listener whitelist")
    if settings.quiet_window <= 0 or settings.cooldown < 0:
        errors.append("quiet window must be positive and cooldown must be non-negative")
    try:
        AgentRuntime.from_config(
            settings.raw, settings.config_path, settings.workspace_root
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"agent workflow is invalid: {exc}")
    if not settings.workspace_root.exists():
        errors.append(f"workspace_root does not exist: {settings.workspace_root}")
    errors.extend(_validate_sandbox(settings))
    return errors


def _validate_sandbox(settings: Settings) -> list[str]:
    """校验联系人的项目配置；只检查不落盘，避免 doctor 改动文件系统。"""
    errors: list[str] = []
    if settings.sandbox_root == settings.workspace_root:
        errors.append("sandbox_root must not be the same directory as workspace_root")
    if settings.workspace_root.exists() and settings.sandbox_root.is_relative_to(
        settings.workspace_root
    ):
        errors.append(
            f"sandbox_root must live outside workspace_root: {settings.sandbox_root}"
        )
    # 沙盒是 agent 的 cwd，放在 state 里会把它送到 audit.sqlite3（含历史对话）旁边。
    if settings.sandbox_root.is_relative_to(settings.state_dir):
        errors.append(
            f"sandbox_root must live outside state_dir: {settings.sandbox_root}"
        )
    for contact in _targets(settings):
        for name in contact.projects:
            target = settings.workspace_root / name
            if not target.is_dir():
                errors.append(
                    f"{contact.alias}: project directory does not exist: {target}"
                )
    if not settings.workspace_root.exists():
        return errors
    report = sandbox.rebuild(
        settings.workspace_root,
        settings.sandbox_root,
        ((contact.alias, contact.projects) for contact in _targets(settings)),
        apply=False,
    )
    for error in report.errors:
        message = f"sandbox: {error}"
        if message not in errors:
            errors.append(message)
    return errors


async def _doctor(settings: Settings) -> int:
    checks: list[tuple[str, bool, str]] = []
    errors = _validate_configuration(settings)
    checks.append(("config", not errors, "; ".join(errors) if errors else "ok"))
    binaries = {
        "dws": str(settings.raw.get("dws", {}).get("binary", "dws")),
    }
    if not errors:
        runtime = AgentRuntime.from_config(
            settings.raw, settings.config_path, settings.workspace_root
        )
        binaries.update(runtime.required_binaries())
    for binary_name, binary in binaries.items():
        resolved = shutil.which(binary)
        checks.append((binary_name, bool(resolved), resolved or "not found"))
    if settings.workspace_root.exists():
        report = sandbox.rebuild(
            settings.workspace_root,
            settings.sandbox_root,
            ((contact.alias, contact.projects) for contact in settings.contacts),
            apply=False,
        )
        links = sum(len(item.links) for item in report.contacts)
        pending = sum(
            1
            for item in report.contacts
            for link in item.links
            if link.state == sandbox.LINK_ABSENT
        )
        detail = (
            f"root={report.root} contacts={len(report.contacts)} links={links}"
        )
        if pending:
            detail += f" pending={pending}(将在启动时创建)"
        if report.errors:
            detail += f" issues={len(report.errors)}"
        checks.append(("sandbox", report.healthy, detail))
    if not errors and shutil.which(str(settings.raw.get("dws", {}).get("binary", "dws"))):
        contact = settings.contacts[0]
        command = _event_consume_command(settings, contact, dry_run=True)
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
            int(security.get("max_message_chars", 12000))
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
