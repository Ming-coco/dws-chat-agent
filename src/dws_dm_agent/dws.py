from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import Settings
from .core import Contact, HistoryMessage, parse_local_datetime, sha256_text


LOG = logging.getLogger("dws-chat-agent")


def _find_message_id(value: Any) -> str | None:
    queue = [value]
    while queue:
        current = queue.pop(0)
        if isinstance(current, Mapping):
            for key in ("openMessageId", "messageId", "message_id", "msgId"):
                if current.get(key):
                    return str(current[key])
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current)
    return None


def _find_biz_id(value: Any) -> str | None:
    queue = [value]
    while queue:
        current = queue.pop(0)
        if isinstance(current, Mapping):
            for key in ("bizId", "biz_id"):
                if current.get(key):
                    return str(current[key])
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current)
    return None


def _find_dws_error_code(*streams: bytes) -> str:
    for raw in streams:
        text = raw.decode("utf-8", errors="replace").strip()
        for candidate in (text, *text.splitlines()):
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            queue = [value]
            while queue:
                current = queue.pop(0)
                if isinstance(current, Mapping):
                    for key in (
                        "server_error_code",
                        "serverErrorCode",
                        "error_code",
                        "errorCode",
                    ):
                        if current.get(key):
                            return str(current[key])
                    queue.extend(current.values())
                elif isinstance(current, list):
                    queue.extend(current)
    return ""


class DwsClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        raw = settings.raw.get("dws", {})
        self.binary = str(raw.get("binary", "dws"))
        self.profile = raw.get("profile")
        self.ai_tag = bool(raw.get("ai_tag", False))
        self.streaming_cards = bool(raw.get("streaming_cards", False))

    def _global_flags(self) -> list[str]:
        return ["--profile", str(self.profile)] if self.profile else []

    async def _json_command(
        self,
        arguments: Sequence[str],
        timeout: float = 45,
        *,
        expect_json: bool = True,
    ) -> Any:
        command = [self.binary, *arguments, *self._global_flags(), "--format", "json"]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except (TimeoutError, asyncio.CancelledError) as exc:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RuntimeError(f"DWS command timed out: {' '.join(arguments[:3])}")
        if process.returncode != 0:
            operation = " ".join(arguments[:3])
            code = _find_dws_error_code(stdout, stderr)
            error_code = f": {code}" if code else ""
            raise RuntimeError(
                f"DWS command failed ({process.returncode}): {operation}{error_code}"
            )
        if not expect_json:
            return stdout.decode("utf-8", errors="replace")
        try:
            return json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("DWS returned invalid JSON") from exc

    async def _retry_network_json_command(
        self, arguments: Sequence[str], timeout: float = 45, *, operation: str
    ) -> Any:
        for attempt in range(3):
            try:
                return await self._json_command(arguments, timeout=timeout)
            except RuntimeError as exc:
                if not str(exc).endswith(": NETWORK_ERROR") or attempt == 2:
                    raise
                LOG.warning(
                    "DWS %s retry attempt=%d/3 error=NETWORK_ERROR",
                    operation,
                    attempt + 2,
                )
                await asyncio.sleep(2**attempt)
        raise AssertionError("unreachable")

    async def _send_json_command(
        self, arguments: Sequence[str], timeout: float = 45
    ) -> Any:
        return await self._retry_network_json_command(
            arguments, timeout=timeout, operation="send"
        )

    async def history(self, contact: Contact) -> list[HistoryMessage]:
        query_time = (datetime.now(self.settings.timezone) + timedelta(seconds=5)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        limit = str(int(self.settings.raw.get("history_limit", 80)))
        payload = await self._fetch_history(contact, query_time, limit)
        messages = payload.get("result", {}).get("messages", []) if isinstance(payload, Mapping) else []
        result: list[HistoryMessage] = []
        for raw in messages:
            if not isinstance(raw, Mapping):
                continue
            message_id = str(raw.get("openMessageId") or raw.get("messageId") or "")
            conversation_id = str(raw.get("openConversationId") or f"dm:{contact.identity}")
            result.append(
                HistoryMessage(
                    message_id=message_id or f"history:{sha256_text(json.dumps(raw, sort_keys=True, ensure_ascii=False))[:24]}",
                    conversation_id=conversation_id,
                    sender=str(raw.get("sender") or ""),
                    sender_open_dingtalk_id=str(raw.get("senderOpenDingTalkId") or ""),
                    content=str(raw.get("content") or "").strip(),
                    created_at=parse_local_datetime(raw.get("createTime"), self.settings.timezone),
                )
            )
        return sorted(result, key=lambda item: item.created_at)

    async def _fetch_history(
        self, contact: Contact, query_time: str, limit: str
    ) -> Any:
        """Fetch a contact's message history, preferring userId.

        Cross-organization contacts often expose a userId that DingTalk's message
        API rejects (peerUid is required). When that userId path fails with a
        business error and an open DingTalk id is available, retry once with
        --open-dingtalk-id. Network errors are not downgraded; they retry in the
        caller and would fail identically on either identity.
        """
        if contact.user_id:
            try:
                return await self._retry_network_json_command(
                    [
                        "chat", "message", "list", "--user", contact.user_id,
                        "--time", query_time, "--direction", "older", "--limit", limit,
                    ],
                    operation="history",
                )
            except RuntimeError as exc:
                if not contact.open_dingtalk_id or str(exc).endswith(": NETWORK_ERROR"):
                    raise
                LOG.warning(
                    "history user-id lookup failed contact=%s; falling back to open DingTalk id error=%s",
                    contact.alias,
                    str(exc),
                )
        return await self._retry_network_json_command(
            [
                "chat", "message", "list", "--open-dingtalk-id", contact.open_dingtalk_id,
                "--time", query_time, "--direction", "older", "--limit", limit,
            ],
            operation="history",
        )

    async def send(
        self, contact: Contact, content: str, send_uuid: str, *,
        dry_run: bool = False, retry_network: bool = False,
    ) -> tuple[str | None, Any]:
        content = re.sub(r"\$(?=\d+\.\d)", "USD ", content)
        if not contact.user_id and not contact.open_dingtalk_id:
            raise ValueError("DingTalk recipient has no user or open DingTalk id")
        command = self._send_json_command if retry_network else self._json_command
        if contact.user_id:
            try:
                payload = await command(
                    self._send_arguments(
                        "--user", contact.user_id, content, send_uuid, dry_run=dry_run
                    )
                )
                return _find_message_id(payload), payload
            except RuntimeError as exc:
                # Cross-organization contacts often have a userId DingTalk cannot
                # resolve (cannot resolve userId ... to openDingTalkId). Retry with
                # the open DingTalk id; the shared send_uuid keeps the retry idempotent.
                if not contact.open_dingtalk_id or str(exc).endswith(": NETWORK_ERROR"):
                    raise
                LOG.warning(
                    "send user-id lookup failed contact=%s; falling back to open DingTalk id error=%s",
                    contact.alias,
                    str(exc),
                )
        payload = await command(
            self._send_arguments(
                "--open-dingtalk-id", contact.open_dingtalk_id,
                content, send_uuid, dry_run=dry_run,
            )
        )
        return _find_message_id(payload), payload

    def _send_arguments(
        self,
        target_flag: str,
        target_value: str,
        content: str,
        send_uuid: str,
        *,
        dry_run: bool,
    ) -> list[str]:
        arguments = [
            "chat", "message", "send", target_flag, target_value,
            "--text", content,
            f"--ai-tag={'true' if self.ai_tag else 'false'}",
            "--uuid", send_uuid, "--yes",
        ]
        if dry_run:
            arguments.append("--dry-run")
        return arguments

    async def send_card(
        self, contact: Contact
    ) -> tuple[str, str | None, Any]:
        if not contact.open_dingtalk_id:
            raise ValueError("streaming cards require an open DingTalk id")
        payload = await self._send_json_command(
            [
                "chat",
                "message",
                "send-card",
                "--receiver",
                contact.open_dingtalk_id,
            ]
        )
        biz_id = _find_biz_id(payload)
        if not biz_id:
            raise RuntimeError("DWS streaming card response did not include bizId")
        return biz_id, _find_message_id(payload), payload

    async def update_card(
        self, biz_id: str, content: str, flow_status: int
    ) -> Any:
        if flow_status not in {1, 2, 3, 4, 5}:
            raise ValueError("invalid streaming card flow status")
        return await self._send_json_command(
            [
                "chat",
                "message",
                "update-card",
                "--biz-id",
                biz_id,
                "--content",
                content,
                "--flow-status",
                str(flow_status),
            ]
        )

    async def add_emoji(
        self, conversation_id: str, message_id: str, emoji: str = "专注"
    ) -> Any:
        return await self._json_command(
            [
                "chat",
                "message",
                "add-emoji",
                "--conversation-id",
                conversation_id,
                "--msg-id",
                message_id,
                "--emoji",
                emoji,
            ]
        )

    async def remove_emoji(
        self, conversation_id: str, message_id: str, emoji: str = "专注"
    ) -> Any:
        return await self._json_command(
            [
                "chat",
                "message",
                "remove-emoji",
                "--conversation-id",
                conversation_id,
                "--msg-id",
                message_id,
                "--emoji",
                emoji,
            ]
        )

    async def send_file(
        self, contact: Contact, file_path: Path, send_uuid: str,
    ) -> tuple[str | None, Any]:
        target = ["--open-dingtalk-id", contact.open_dingtalk_id] if contact.open_dingtalk_id else ["--user", contact.user_id]
        payload = await self._send_json_command(
            [
                "chat", "message", "send", *target, "--msg-type", "file",
                "--file-path", str(file_path),
                f"--ai-tag={'true' if self.ai_tag else 'false'}", "--uuid", send_uuid, "--yes",
            ],
            timeout=180,
        )
        return _find_message_id(payload), payload

    async def download_media(
        self,
        resource_id: str,
        message_id: str,
        conversation_id: str,
        output: Path,
    ) -> Path:
        await self._json_command(
            [
                "chat",
                "message",
                "download-media",
                "--type",
                "mediaId",
                "--resource-id",
                resource_id,
                "--message-id",
                message_id,
                "--open-conversation-id",
                conversation_id,
                "--output",
                str(output),
            ],
            timeout=90,
            expect_json=False,
        )
        if not output.is_file():
            raise RuntimeError("media_download_failed: DWS did not create the output file")
        return output
