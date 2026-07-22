from __future__ import annotations

import asyncio
import json
import logging
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
        except TimeoutError:
            process.terminate()
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5)
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

    async def _send_json_command(
        self, arguments: Sequence[str], timeout: float = 45
    ) -> Any:
        for attempt in range(3):
            try:
                return await self._json_command(arguments, timeout=timeout)
            except RuntimeError as exc:
                if not str(exc).endswith(": NETWORK_ERROR") or attempt == 2:
                    raise
                LOG.warning("DWS send retry attempt=%d/3 error=NETWORK_ERROR", attempt + 2)
                await asyncio.sleep(2**attempt)
        raise AssertionError("unreachable")

    async def history(self, contact: Contact) -> list[HistoryMessage]:
        query_time = (datetime.now(self.settings.timezone) + timedelta(seconds=5)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        payload = await self._json_command(
            [
                "chat", "message", "list", "--user", contact.user_id,
                "--time", query_time, "--direction", "older", "--limit",
                str(int(self.settings.raw.get("history_limit", 80))),
            ]
        )
        messages = payload.get("result", {}).get("messages", []) if isinstance(payload, Mapping) else []
        result: list[HistoryMessage] = []
        for raw in messages:
            if not isinstance(raw, Mapping):
                continue
            message_id = str(raw.get("openMessageId") or raw.get("messageId") or "")
            conversation_id = str(raw.get("openConversationId") or f"dm:{contact.user_id}")
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

    async def send(
        self, contact: Contact, content: str, send_uuid: str, *,
        dry_run: bool = False, retry_network: bool = False,
    ) -> tuple[str | None, Any]:
        if contact.user_id:
            target = ["--user", contact.user_id]
        elif contact.open_dingtalk_id:
            target = ["--open-dingtalk-id", contact.open_dingtalk_id]
        else:
            raise ValueError("DingTalk recipient has no user or open DingTalk id")
        arguments = [
            "chat", "message", "send", *target, "--text", content,
            f"--ai-tag={'true' if self.ai_tag else 'false'}", "--uuid", send_uuid, "--yes",
        ]
        if dry_run:
            arguments.append("--dry-run")
        payload = await self._send_json_command(arguments) if retry_network else await self._json_command(arguments)
        return _find_message_id(payload), payload

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
