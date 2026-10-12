from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple, Sequence


# 单次 agent 运行的图片预算（用户可见上限）：最多 MAX_IMAGES 张、每张不超过
# MAX_IMAGE_BYTES。下载侧（service）与事件流读取上限（runners.runtime）都必须
# 从这两个常量推导，保证"允许下载的图片一定能被事件流完整读回"。
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 20 * 1024 * 1024

# 事项记忆的业务状态。归档（archived_at）独立于状态，不新增额外业务状态。
TASK_STATUSES = ("open", "waiting", "completed", "cancelled")


class TaskScope(NamedTuple):
    """事项的作用域：本地登录身份 + 联系人 + 会话，三者组合隔离。"""

    self_id: str
    contact_id: str
    conversation_id: str


@dataclass(frozen=True)
class Contact:
    alias: str
    display_name: str
    user_id: str
    open_dingtalk_id: str

    @property
    def identity(self) -> str:
        """Stable local key when DingTalk does not expose a userId."""
        return self.user_id or self.open_dingtalk_id


@dataclass(frozen=True)
class IncomingEvent:
    event_id: str
    message_id: str
    conversation_id: str
    contact: Contact
    content: str
    created_at: datetime


@dataclass(frozen=True)
class HistoryMessage:
    message_id: str
    conversation_id: str
    sender: str
    sender_open_dingtalk_id: str
    content: str
    created_at: datetime


@dataclass(frozen=True)
class GateResult:
    action: str
    reason: str
    reply: str = ""


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def canonical_message_text(value: str) -> str:
    """Match DWS history after it normalizes Markdown/newline whitespace."""
    return " ".join(value.split())


_SECRET_VALUE = re.compile(
    r"(?i)((?:authorization|token|password|passwd|secret|api[-_]?key|cookie|密码|口令|密钥|凭据)\s*[:=：]\s*)([^\s,;，；]+)"
)
_SECRET_QUERY = re.compile(
    r"(?i)([?&](?:access_token|token|secret|api[-_]?key|password)=)([^&#\s]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----",
    re.DOTALL,
)


def sanitize_text(value: Any, max_chars: int = 6000) -> str:
    text = str(value or "")
    text = "".join(char for char in text if char in "\n\t" or ord(char) >= 32)
    text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _JWT.sub("[REDACTED JWT]", text)
    text = _SECRET_VALUE.sub(r"\1[REDACTED]", text)
    text = _SECRET_QUERY.sub(r"\1[REDACTED]", text)
    if len(text) > max_chars:
        text = text[: max_chars - 16].rstrip() + "…[已截断]"
    return text.strip()


def human_owns_conversation(
    latest_manual: datetime | None,
    earliest_incoming: datetime,
    cooldown_seconds: float,
) -> bool:
    """Only a real self-authored message starts cooldown; read state is irrelevant."""
    if cooldown_seconds <= 0:
        return False
    if latest_manual is None:
        return False
    if latest_manual >= earliest_incoming - timedelta(seconds=2):
        return True
    return (earliest_incoming - latest_manual).total_seconds() < cooldown_seconds


def parse_local_datetime(value: Any, timezone: tzinfo) -> datetime:
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        return datetime.fromtimestamp(seconds, timezone)
    text = str(value or "").strip()
    if text.isdigit():
        return parse_local_datetime(int(text), timezone)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.astimezone(timezone) if parsed.tzinfo else parsed.replace(tzinfo=timezone)
        except ValueError:
            continue
    return datetime.now(timezone)


def _json_object(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _deep_value(root: Any, names: set[str]) -> Any:
    queue = [root]
    seen: set[int] = set()
    while queue:
        current = _json_object(queue.pop(0))
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            for key, value in current.items():
                if key in names and value not in (None, ""):
                    return value
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current)
    return None


def _extract_content(value: Any) -> str:
    value = _json_object(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("text", "content", "title"):
            if key in value:
                text = _extract_content(value[key])
                if text:
                    return text
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return ""
    return str(value).strip()


def parse_dws_event(line: str, contact: Contact, timezone: tzinfo) -> IncomingEvent | None:
    try:
        outer = json.loads(line)
    except json.JSONDecodeError:
        return None
    payload = _json_object(outer.get("data")) if isinstance(outer, Mapping) else outer
    if not isinstance(payload, Mapping):
        payload = outer
    sender_id = str(
        _deep_value(payload, {"sender_open_dingtalk_id", "senderOpenDingTalkId", "senderId"}) or ""
    )
    if sender_id and sender_id != contact.open_dingtalk_id:
        return None
    content = _extract_content(_deep_value(payload, {"content", "text", "messageContent"}))
    if not content:
        return None
    message_id = str(
        _deep_value(payload, {"message_id", "messageId", "openMessageId", "msgId"}) or ""
    )
    conversation_id = str(
        _deep_value(payload, {"conversation_id", "conversationId", "openConversationId"})
        or f"dm:{contact.identity}"
    )
    event_id = str(_deep_value(outer, {"event_id", "eventId", "id"}) or "")
    if not message_id:
        message_id = f"msg:{sha256_text(conversation_id + content)[:24]}"
    if not event_id:
        event_id = f"event:{sha256_text(message_id + content)[:24]}"
    created_raw = _deep_value(payload, {"create_time", "createTime", "timestamp", "time"})
    return IncomingEvent(
        event_id=event_id,
        message_id=message_id,
        conversation_id=conversation_id,
        contact=contact,
        content=content,
        created_at=parse_local_datetime(created_raw, timezone),
    )


_ACK_TEXT = re.compile(
    r"^(?:好(?:的|嘞|滴)?|收到|知道了|明白|了解|行|可以|没问题|ok|okay|嗯+|哦+|谢(?:谢|啦)|辛苦(?:了|啦)?|哈哈+|嗯嗯|1|666)[。！!~～,.， ]*$",
    re.IGNORECASE,
)
_ONLY_REACTION = re.compile(r"^(?:\[[^\]\n]{1,16}\]|[\W_]){1,8}$", re.UNICODE)
class SecurityGate:
    """Zero-token noise and size gate. Semantic decisions belong to the agent."""

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars

    def inspect(self, messages: Sequence[str]) -> GateResult:
        cleaned = [value.strip() for value in messages if value and value.strip()]
        if not cleaned:
            return GateResult("drop", "empty")
        combined = "\n".join(cleaned)
        if len(combined) > self.max_chars:
            return GateResult(
                "refuse",
                "message_too_large",
                "这段内容过长，我先不自动处理。请拆成具体问题，或等我本人接入。",
            )
        if all(_ACK_TEXT.fullmatch(value) or _ONLY_REACTION.fullmatch(value) for value in cleaned):
            return GateResult("drop", "ack_or_reaction")
        return GateResult("pass", "eligible")


class AuditStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        # 确认/切换 WAL 需要写锁，而且 busy handler 对 journal_mode 不排队：
        # 另一进程持写事务时该 PRAGMA 会立刻 SQLITE_BUSY（task 工具进程与
        # 服务并发初始化时的真实竞态）。先读当前模式，已是 WAL 就完全跳过
        # 写 PRAGMA；确需切换时短暂重试。
        mode = str(
            self.connection.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()
        if mode != "wal":
            for _ in range(20):
                try:
                    self.connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError:
                    time.sleep(0.05)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL UNIQUE,
                conversation_id TEXT NOT NULL,
                contact_user_id TEXT NOT NULL,
                received_at TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                content_length INTEGER NOT NULL,
                request_preview TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS outgoing (
                send_uuid TEXT PRIMARY KEY,
                message_id TEXT UNIQUE,
                conversation_id TEXT NOT NULL,
                contact_user_id TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                session_id TEXT NOT NULL,
                card_biz_id TEXT,
                card_flow_status INTEGER,
                payload_text TEXT NOT NULL DEFAULT '',
                delivery_status TEXT NOT NULL DEFAULT 'sent',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                event_ids_json TEXT NOT NULL DEFAULT '[]',
                source_received_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS outgoing_conversation_time
                ON outgoing(conversation_id, sent_at);
            CREATE TABLE IF NOT EXISTS manual_messages (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                sent_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS manual_conversation_time
                ON manual_messages(conversation_id, sent_at);
            CREATE TABLE IF NOT EXISTS runs (
                session_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                contact_user_id TEXT NOT NULL,
                contact_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                handled TEXT NOT NULL,
                reason TEXT NOT NULL,
                changes_json TEXT NOT NULL,
                validation_json TEXT NOT NULL,
                external_calls_json TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                request_preview TEXT NOT NULL DEFAULT '',
                codex_exit_code INTEGER
            );
            CREATE INDEX IF NOT EXISTS runs_finished_at ON runs(finished_at);
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                scope_self TEXT NOT NULL,
                scope_contact TEXT NOT NULL,
                scope_conversation TEXT NOT NULL,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                waiting_on TEXT NOT NULL DEFAULT '',
                evidence_summary TEXT NOT NULL DEFAULT '',
                evidence_message_ids TEXT NOT NULL DEFAULT '[]',
                last_evidence_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT NOT NULL DEFAULT '',
                archive_reason TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                run_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS tasks_scope
                ON tasks(scope_self, scope_contact, scope_conversation, status);
            CREATE TABLE IF NOT EXISTS task_changes (
                run_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, tool_call_id)
            );
            CREATE TABLE IF NOT EXISTS task_run_leases (
                run_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                revoked_at TEXT NOT NULL DEFAULT '',
                revoke_reason TEXT NOT NULL DEFAULT ''
            );
            """
        )
        migrations = {
            "events": {"request_preview": "request_preview TEXT NOT NULL DEFAULT ''"},
            "runs": {"request_preview": "request_preview TEXT NOT NULL DEFAULT ''"},
            "outgoing": {
                "card_biz_id": "card_biz_id TEXT",
                "card_flow_status": "card_flow_status INTEGER",
                "payload_text": "payload_text TEXT NOT NULL DEFAULT ''",
                "delivery_status": "delivery_status TEXT NOT NULL DEFAULT 'sent'",
                "attempt_count": "attempt_count INTEGER NOT NULL DEFAULT 0",
                "next_attempt_at": "next_attempt_at TEXT NOT NULL DEFAULT ''",
                "last_error": "last_error TEXT NOT NULL DEFAULT ''",
                "event_ids_json": "event_ids_json TEXT NOT NULL DEFAULT '[]'",
                "source_received_at": "source_received_at TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, columns in migrations.items():
            names = {
                row["name"]
                for row in self.connection.execute(f"PRAGMA table_info({table})")
            }
            for name, definition in columns.items():
                if name not in names:
                    self.connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {definition}"
                    )
        self.connection.execute(
            """
            UPDATE outgoing
            SET delivery_status = 'retry_wait', next_attempt_at = ''
            WHERE delivery_status = 'sending'
            """
        )
        self.connection.commit()

    def claim_event(self, event: IncomingEvent) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO events(
                event_id, message_id, conversation_id, contact_user_id,
                received_at, content_sha256, content_length, request_preview
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.message_id,
                event.conversation_id,
                event.contact.identity,
                event.created_at.isoformat(),
                sha256_text(event.content),
                len(event.content),
                sanitize_text(event.content, 500),
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def update_event_status(self, message_ids: Iterable[str], status: str, reason: str = "") -> None:
        self.connection.executemany(
            "UPDATE events SET status = ?, reason = ? WHERE message_id = ?",
            [(status, reason, message_id) for message_id in message_ids],
        )
        self.connection.commit()

    def event_is_pending(self, message_id: str) -> bool:
        row = self.connection.execute(
            "SELECT status FROM events WHERE message_id = ?", (message_id,)
        ).fetchone()
        return bool(row and row["status"] == "queued")

    def pending_event_ids(self, contact_user_id: str) -> set[str]:
        return {
            row["message_id"]
            for row in self.connection.execute(
                "SELECT message_id FROM events WHERE contact_user_id = ? AND status = 'queued'",
                (contact_user_id,),
            )
        }

    def record_outgoing(
        self,
        send_uuid: str,
        message_id: str | None,
        conversation_id: str,
        contact_user_id: str,
        sent_at: datetime,
        content: str,
        session_id: str,
        *,
        card_biz_id: str | None = None,
        card_flow_status: int | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO outgoing(
                send_uuid, message_id, conversation_id, contact_user_id,
                sent_at, content_sha256, session_id, card_biz_id, card_flow_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?,
                COALESCE(?, (SELECT card_biz_id FROM outgoing WHERE send_uuid = ?)),
                COALESCE(?, (SELECT card_flow_status FROM outgoing WHERE send_uuid = ?))
            )
            """,
            (
                send_uuid,
                message_id,
                conversation_id,
                contact_user_id,
                sent_at.isoformat(),
                sha256_text(canonical_message_text(content)),
                session_id,
                card_biz_id,
                send_uuid,
                card_flow_status,
                send_uuid,
            ),
        )
        self.connection.commit()

    def enqueue_delivery(
        self,
        send_uuid: str,
        source_event: IncomingEvent,
        content: str,
        session_id: str,
        *,
        event_ids: Sequence[str],
        card_biz_id: str = "",
    ) -> None:
        now = datetime.now(source_event.created_at.tzinfo)
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute(
            """
            INSERT INTO outgoing(
                send_uuid, message_id, conversation_id, contact_user_id,
                sent_at, content_sha256, session_id, card_biz_id,
                card_flow_status, payload_text, delivery_status,
                attempt_count, next_attempt_at, last_error, event_ids_json,
                source_received_at
            ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, 'pending',
                      0, '', '', ?, ?)
            """,
            (
                send_uuid,
                source_event.conversation_id,
                source_event.contact.identity,
                now.isoformat(),
                sha256_text(canonical_message_text(content)),
                session_id,
                card_biz_id or None,
                content,
                json.dumps(list(event_ids), ensure_ascii=False),
                source_event.created_at.isoformat(),
            ),
        )
        self.connection.executemany(
            "UPDATE events SET status = 'delivery_pending', reason = '' "
            "WHERE message_id = ?",
            [(message_id,) for message_id in event_ids],
        )
        self.connection.commit()

    def bind_delivery_card(self, send_uuid: str, card_biz_id: str) -> None:
        """把投递行绑定到投递时点创建的卡片；网络重试复用同一张卡。"""
        if not card_biz_id:
            return
        self.connection.execute(
            """
            UPDATE outgoing SET card_biz_id = ?
            WHERE send_uuid = ? AND card_biz_id IS NULL
            """,
            (card_biz_id, send_uuid),
        )
        self.connection.commit()

    def claim_due_delivery(self, now: datetime) -> sqlite3.Row | None:
        self.connection.execute("BEGIN IMMEDIATE")
        row = self.connection.execute(
            """
            SELECT * FROM outgoing
            WHERE delivery_status IN ('pending', 'retry_wait')
              AND (next_attempt_at = '' OR next_attempt_at <= ?)
            ORDER BY sent_at ASC
            LIMIT 1
            """,
            (now.isoformat(),),
        ).fetchone()
        if row is None:
            self.connection.commit()
            return None
        self.connection.execute(
            "UPDATE outgoing SET delivery_status = 'sending' WHERE send_uuid = ?",
            (row["send_uuid"],),
        )
        self.connection.commit()
        return row

    def mark_delivery_retry(
        self, send_uuid: str, error: str, next_attempt_at: datetime
    ) -> None:
        self.connection.execute(
            """
            UPDATE outgoing
            SET delivery_status = 'retry_wait',
                attempt_count = attempt_count + 1,
                next_attempt_at = ?,
                last_error = ?
            WHERE send_uuid = ?
            """,
            (
                next_attempt_at.isoformat(),
                sanitize_text(error, 500),
                send_uuid,
            ),
        )
        self.connection.commit()

    def mark_delivery_sent(
        self,
        send_uuid: str,
        message_id: str = "",
        *,
        card_flow_status: int | None = None,
    ) -> None:
        row = self.connection.execute(
            """
            SELECT event_ids_json, session_id, card_biz_id, content_sha256
            FROM outgoing WHERE send_uuid = ?
            """,
            (send_uuid,),
        ).fetchone()
        if row is None:
            return
        event_ids = json.loads(row["event_ids_json"] or "[]")
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute(
            """
            UPDATE outgoing
            SET delivery_status = 'sent', message_id = COALESCE(NULLIF(?, ''), message_id),
                payload_text = '', next_attempt_at = '', last_error = '',
                card_flow_status = COALESCE(?, card_flow_status)
            WHERE send_uuid = ?
            """,
            (message_id, card_flow_status, send_uuid),
        )
        if row["card_biz_id"] and card_flow_status is not None:
            self.connection.execute(
                """
                UPDATE outgoing
                SET content_sha256 = ?, card_flow_status = ?
                WHERE card_biz_id = ?
                """,
                (
                    row["content_sha256"],
                    card_flow_status,
                    row["card_biz_id"],
                ),
            )
        self.connection.executemany(
            "UPDATE events SET status = 'processed', reason = 'sent' "
            "WHERE message_id = ?",
            [(event_id,) for event_id in event_ids],
        )
        self.connection.execute(
            "UPDATE runs SET status = 'sent', reason = '' WHERE session_id = ?",
            (row["session_id"],),
        )
        self.connection.commit()

    def mark_delivery_cancelled(
        self, send_uuid: str, run_status: str, reason: str
    ) -> None:
        row = self.connection.execute(
            "SELECT event_ids_json, session_id FROM outgoing WHERE send_uuid = ?",
            (send_uuid,),
        ).fetchone()
        if row is None:
            return
        event_ids = json.loads(row["event_ids_json"] or "[]")
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute(
            """
            UPDATE outgoing
            SET delivery_status = 'cancelled', payload_text = '',
                next_attempt_at = '', last_error = ?
            WHERE send_uuid = ?
            """,
            (sanitize_text(reason, 500), send_uuid),
        )
        self.connection.executemany(
            "UPDATE events SET status = 'skipped', reason = ? WHERE message_id = ?",
            [(reason, event_id) for event_id in event_ids],
        )
        self.connection.execute(
            "UPDATE runs SET status = ?, reason = ? WHERE session_id = ?",
            (run_status, reason, row["session_id"]),
        )
        self.connection.commit()

    def remove_outgoing(self, send_uuid: str) -> None:
        self.connection.execute("DELETE FROM outgoing WHERE send_uuid = ?", (send_uuid,))
        self.connection.commit()

    def update_outgoing_content(self, send_uuid: str, content: str) -> None:
        self.connection.execute(
            """
            UPDATE outgoing SET content_sha256 = ? WHERE send_uuid = ?
            """,
            (
                sha256_text(canonical_message_text(content)),
                send_uuid,
            ),
        )
        self.connection.commit()

    def active_streaming_card(self, conversation_id: str) -> tuple[str, str] | None:
        row = self.connection.execute(
            """
            SELECT card_biz_id, send_uuid FROM outgoing
            WHERE conversation_id = ? AND card_biz_id IS NOT NULL
              AND (card_flow_status IS NULL OR card_flow_status NOT IN (3, 5))
            ORDER BY sent_at DESC LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        return str(row["card_biz_id"]), str(row["send_uuid"])

    def update_streaming_card(
        self, send_uuid: str, content: str, flow_status: int
    ) -> None:
        self.connection.execute(
            """
            UPDATE outgoing
            SET content_sha256 = ?, card_flow_status = ?
            WHERE send_uuid = ?
            """,
            (
                sha256_text(canonical_message_text(content)),
                flow_status,
                send_uuid,
            ),
        )
        self.connection.commit()

    def bind_outgoing_id(self, send_uuid: str, message_id: str) -> bool:
        if not message_id:
            return False
        try:
            cursor = self.connection.execute(
                """
                UPDATE outgoing SET message_id = ?
                WHERE send_uuid = ? AND message_id IS NULL
                """,
                (message_id, send_uuid),
            )
            self.connection.commit()
        except sqlite3.IntegrityError:
            return False
        return cursor.rowcount == 1

    def outgoing_session_id(self, message_id: str) -> str:
        if not message_id:
            return ""
        row = self.connection.execute(
            "SELECT session_id FROM outgoing WHERE message_id = ?", (message_id,)
        ).fetchone()
        return str(row["session_id"]) if row else ""

    def is_agent_outgoing(self, message: HistoryMessage, tolerance_seconds: int = 180) -> bool:
        if message.message_id:
            row = self.connection.execute(
                "SELECT 1 FROM outgoing WHERE message_id = ?", (message.message_id,)
            ).fetchone()
            if row:
                return True
        canonical_hash = sha256_text(canonical_message_text(message.content))
        raw_hash = sha256_text(message.content)
        rows = self.connection.execute(
            """
            SELECT send_uuid, message_id, sent_at, content_sha256, session_id
            FROM outgoing
            WHERE conversation_id = ?
            ORDER BY sent_at DESC LIMIT 20
            """,
            (message.conversation_id,),
        ).fetchall()
        candidates: list[tuple[sqlite3.Row, float, bool]] = []
        for row in rows:
            try:
                sent_at = datetime.fromisoformat(row["sent_at"])
            except ValueError:
                continue
            delta = abs((message.created_at - sent_at).total_seconds())
            content_matches = row["content_sha256"] in {canonical_hash, raw_hash}
            if row["message_id"] and row["message_id"] != message.message_id:
                continue
            candidates.append((row, delta, content_matches))
        # Prefer a normalized content match. Only fall back to the closest
        # unbound timestamp when an older DWS response omitted message_id and
        # the historical content representation differs (notably file cards).
        content_candidates = [
            item
            for item in candidates
            if item[2]
            and item[1] <= tolerance_seconds
        ]
        selected = min(content_candidates, key=lambda item: item[1], default=None)
        if selected is not None and str(selected[0]["session_id"]).startswith("ack-"):
            # Card creation omits openMessageId. Recognize exact recent content,
            # but never bind it to an arbitrary self-authored message.
            return True
        if selected is None:
            legacy_candidates = [
                item
                for item in candidates
                if not item[0]["message_id"]
                and not str(item[0]["session_id"]).startswith("ack-")
                and item[1] <= 3
            ]
            selected = min(legacy_candidates, key=lambda item: item[1], default=None)
        if selected is None:
            return False
        row = selected[0]
        if message.message_id and not row["message_id"]:
            try:
                self.connection.execute(
                    "UPDATE outgoing SET message_id = ? WHERE send_uuid = ?",
                    (message.message_id, row["send_uuid"]),
                )
                self.connection.commit()
            except sqlite3.IntegrityError:
                pass
        return True

    def record_manual(self, message: HistoryMessage) -> None:
        if not message.message_id:
            return
        self.connection.execute(
            """
            INSERT OR IGNORE INTO manual_messages(message_id, conversation_id, sent_at)
            VALUES (?, ?, ?)
            """,
            (message.message_id, message.conversation_id, message.created_at.isoformat()),
        )
        self.connection.commit()

    def remove_manual(self, message_id: str) -> None:
        if not message_id:
            return
        self.connection.execute(
            "DELETE FROM manual_messages WHERE message_id = ?", (message_id,)
        )
        self.connection.commit()

    def latest_manual(self, conversation_id: str) -> datetime | None:
        row = self.connection.execute(
            """
            SELECT sent_at FROM manual_messages
            WHERE conversation_id = ? ORDER BY sent_at DESC LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        try:
            return datetime.fromisoformat(row["sent_at"])
        except ValueError:
            return None

    def record_run(
        self,
        *,
        session_id: str,
        conversation_id: str,
        contact: Contact,
        started_at: datetime,
        finished_at: datetime,
        action: str,
        status: str,
        handled: str,
        reason: str,
        codex_exit_code: int | None,
        request_preview: str = "",
    ) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO runs(
                session_id, conversation_id, contact_user_id, contact_name,
                started_at, finished_at, action, status, handled, reason,
                changes_json, validation_json, external_calls_json, warnings_json,
                request_preview, codex_exit_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                conversation_id,
                contact.identity,
                contact.display_name,
                started_at.isoformat(),
                finished_at.isoformat(),
                action,
                status,
                sanitize_text(handled, 1000),
                sanitize_text(reason, 1000),
                "[]",
                "[]",
                "[]",
                "[]",
                sanitize_text(request_preview, 500),
                codex_exit_code,
            ),
        )
        self.connection.commit()

    def agent_run_counts_since(
        self, since: datetime, contact_user_id: str
    ) -> tuple[int, int]:
        contact_row = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM runs
            WHERE started_at >= ? AND session_id LIKE 'dm-%' AND contact_user_id = ?
            """,
            (since.isoformat(), contact_user_id),
        ).fetchone()
        global_row = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM runs
            WHERE started_at >= ? AND session_id LIKE 'dm-%'
            """,
            (since.isoformat(),),
        ).fetchone()
        return int(contact_row["count"]), int(global_row["count"])

    # ------------------------------------------------------------------
    # 事项记忆（task memory）：工作记忆而非权威任务账本。
    # 归档独立于业务状态；查询不续期；人工接管不批量改状态。
    # ------------------------------------------------------------------

    def _task_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "goal": str(row["goal"]),
            "status": str(row["status"]),
            "waiting_on": str(row["waiting_on"]),
            "evidence_summary": str(row["evidence_summary"]),
            "evidence_message_ids": json.loads(row["evidence_message_ids"] or "[]"),
            "last_evidence_at": str(row["last_evidence_at"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "archived_at": str(row["archived_at"] or ""),
            "archive_reason": str(row["archive_reason"]),
            "version": int(row["version"]),
        }

    def _load_task(self, scope: TaskScope, task_id: str) -> sqlite3.Row | None:
        # 作用域外的 ID 与不存在的 ID 同样返回 None，不泄露其他作用域的事项存在性。
        return self.connection.execute(
            """
            SELECT * FROM tasks
            WHERE id = ? AND scope_self = ? AND scope_contact = ?
              AND scope_conversation = ?
            """,
            (
                task_id,
                scope.self_id,
                scope.contact_id,
                scope.conversation_id,
            ),
        ).fetchone()

    def task_change_response(
        self, run_id: str, tool_call_id: str
    ) -> dict[str, Any] | None:
        """返回同一 (run_id, tool_call_id) 已落地过的响应，用于重复事件幂等。"""
        row = self._task_receipt_row(run_id, tool_call_id)
        if row is None:
            return None
        return self._decode_task_receipt(row)

    def register_task_run(self, run_id: str, now: datetime) -> None:
        """登记一次运行的写入租约；服务在挂载事项工具的会话启动前调用。

        租约是 task_tools.py 直连事务的运行撤销机制：apply_task_record 在
        事务内（拿到 SQLite 写锁之后）校验租约，人工接管/取消已吊销的
        运行即使子进程早已排队等锁也会被拒绝。幂等：重复登记保留原行。
        """
        if not run_id:
            return
        self.connection.execute(
            "INSERT OR IGNORE INTO task_run_leases(run_id, created_at) VALUES (?, ?)",
            (run_id, now.isoformat()),
        )
        self.connection.commit()

    def revoke_task_run(self, run_id: str, reason: str, now: datetime) -> None:
        """吊销运行的写入租约；幂等，首次吊销的时间与原因保留。"""
        if not run_id:
            return
        self.connection.execute(
            """
            UPDATE task_run_leases SET revoked_at = ?, revoke_reason = ?
            WHERE run_id = ? AND revoked_at = ''
            """,
            (now.isoformat(), reason, run_id),
        )
        self.connection.commit()

    def _task_run_revoked_in_tx(self, run_id: str) -> bool:
        """在当前事务内读取租约状态；必须在 BEGIN IMMEDIATE 之后调用。

        读发生在拿到写锁之后，因此一定看到吊销前已提交的最新状态：
        排队等锁的过期子进程醒来后在这里被拒绝，而不是继续落地变更。
        """
        row = self.connection.execute(
            "SELECT revoked_at FROM task_run_leases WHERE run_id = ?", (run_id,)
        ).fetchone()
        return bool(row and row["revoked_at"])

    def _task_receipt_row(
        self, run_id: str, tool_call_id: str
    ) -> sqlite3.Row | None:
        if not run_id or not tool_call_id:
            return None
        return self.connection.execute(
            "SELECT response_json FROM task_changes WHERE run_id = ? AND tool_call_id = ?",
            (run_id, tool_call_id),
        ).fetchone()

    @staticmethod
    def _decode_task_receipt(row: sqlite3.Row) -> dict[str, Any] | None:
        try:
            value = json.loads(row["response_json"])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _store_task_receipt(
        self,
        run_id: str,
        tool_call_id: str,
        response: Mapping[str, Any],
        now: datetime,
    ) -> None:
        """在当前事务内写入幂等收据；与事项变更一起提交或一起回滚。

        OR REPLACE 只在收据存在但不可解析（重放检查放行了重新执行）时
        覆盖旧值；正常路径下该行不存在，等价于普通插入。
        """
        self.connection.execute(
            """
            INSERT OR REPLACE INTO task_changes(
                run_id, tool_call_id, task_id, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                tool_call_id,
                str(response.get("task", {}).get("id") or ""),
                json.dumps(dict(response), ensure_ascii=False),
                now.isoformat(),
            ),
        )

    def _replay_task_response_in_tx(
        self, run_id: str, tool_call_id: str
    ) -> dict[str, Any] | None:
        row = self._task_receipt_row(run_id, tool_call_id)
        if row is None:
            return None
        replayed = self._decode_task_receipt(row)
        if replayed is None:
            # 收据存在但不可解析：视为该事件从未落地，重新执行并覆盖收据。
            return None
        return replayed

    def apply_task_record(
        self,
        *,
        scope: TaskScope,
        record: Mapping[str, Any],
        evidence_at: datetime,
        now: datetime,
        run_id: str = "",
        tool_call_id: str = "",
    ) -> dict[str, Any]:
        """带幂等收据的 task_record：收据与事项变更在同一事务内落地。

        同一 (run_id, tool_call_id) 已有收据时直接返回已落地响应；否则
        执行变更并把响应写入收据，两者一起提交或一起回滚。进程在两步
        之间崩溃不会留下"变更已生效但收据缺失"，重放也就不会重复创建
        或重复更新事项。BEGIN IMMEDIATE 串行化并发调用：后到者在收到
        写锁后必然看到先到者的收据。

        拿到写锁后先校验运行租约：人工接管、取消或会话收尾已吊销本
        运行时，即使 task_tools.py 子进程早已排队等到写锁，也拒绝落地
        变更或重放旧收据（接管绝不批量改写事项状态，只拦新写入）。
        """
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if run_id and self._task_run_revoked_in_tx(run_id):
                self.connection.commit()
                return {"ok": False, "error": "run_revoked"}
            if run_id and tool_call_id:
                replayed = self._replay_task_response_in_tx(run_id, tool_call_id)
                if replayed is not None:
                    self.connection.commit()
                    return replayed
            response = self._record_task_tx(
                scope=scope, record=record, evidence_at=evidence_at, now=now
            )
            if run_id and tool_call_id:
                self._store_task_receipt(run_id, tool_call_id, response, now)
            self.connection.commit()
            return response
        except Exception:
            self.connection.rollback()
            raise

    def apply_task_lookup(
        self,
        *,
        scope: TaskScope,
        status: str = "",
        include_archived: bool = False,
        limit: int = 20,
        now: datetime,
        run_id: str = "",
        tool_call_id: str = "",
    ) -> dict[str, Any]:
        """带幂等收据的 task_lookup：同一工具事件重放返回同一份查询结果。"""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if run_id and tool_call_id:
                replayed = self._replay_task_response_in_tx(run_id, tool_call_id)
                if replayed is not None:
                    self.connection.commit()
                    return replayed
            tasks = self.lookup_tasks(
                scope=scope,
                status=status,
                include_archived=include_archived,
                limit=limit,
            )
            response = {"ok": True, "tasks": tasks}
            if run_id and tool_call_id:
                self._store_task_receipt(run_id, tool_call_id, response, now)
            self.connection.commit()
            return response
        except Exception:
            self.connection.rollback()
            raise

    def record_task(
        self,
        *,
        scope: TaskScope,
        task_id: str = "",
        goal: str | None = None,
        status: str | None = None,
        waiting_on: str | None = None,
        evidence_summary: str | None = None,
        evidence_message_ids: Sequence[str] = (),
        expected_version: int | None = None,
        restore: bool = False,
        run_id: str = "",
        evidence_at: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        """创建或更新事项（无幂等收据；工具入口走 apply_task_record）。

        更新是部分提交：None 表示不改动该字段，业务状态不会回落到 open；
        waiting_on 传空串表示显式清空，其余字段的空串等同于未提交。
        更新走乐观版本锁并落到带 version 条件的单条 UPDATE：
        读与写之间发生的并发更新或归档都会让条件失配，读回最新状态
        返回 version_conflict，不静默覆盖。重复提交相同内容视为无实质
        变化，不刷新 last_evidence_at、不递增 version（不延长保留期）。
        last_evidence_at 只随更新的证据前移：被引用旧消息的时间戳不会
        把已记录的证据时间回拨。
        """
        return self.apply_task_record(
            scope=scope,
            record={
                "task_id": task_id,
                "goal": goal,
                "status": status,
                "waiting_on": waiting_on,
                "evidence_summary": evidence_summary,
                "evidence_message_ids": evidence_message_ids,
                "expected_version": expected_version,
                "restore": restore,
                "run_id": run_id,
            },
            evidence_at=evidence_at,
            now=now,
        )

    def _record_task_tx(
        self,
        *,
        scope: TaskScope,
        record: Mapping[str, Any],
        evidence_at: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        """record_task 的无事务版本：在调用方的事务内执行，不提交不回滚。"""
        task_id = str(record.get("task_id") or "")
        goal = record.get("goal")
        status = record.get("status")
        waiting_on = record.get("waiting_on")
        evidence_summary = record.get("evidence_summary")
        evidence_message_ids = record.get("evidence_message_ids") or ()
        expected_version = record.get("expected_version")
        restore = bool(record.get("restore", False))
        run_id = str(record.get("run_id") or "")
        if status is not None and status not in TASK_STATUSES:
            return {"ok": False, "error": f"invalid_status: {status}"}
        if not task_id:
            if not goal:
                return {"ok": False, "error": "goal_required_for_new_task"}
            task_id = f"task-{uuid.uuid4().hex[:12]}"
            self.connection.execute(
                """
                INSERT INTO tasks(
                    id, scope_self, scope_contact, scope_conversation,
                    goal, status, waiting_on, evidence_summary,
                    evidence_message_ids, last_evidence_at, created_at,
                    updated_at, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    scope.self_id,
                    scope.contact_id,
                    scope.conversation_id,
                    goal,
                    status or "open",
                    waiting_on or "",
                    evidence_summary or "",
                    json.dumps(list(evidence_message_ids), ensure_ascii=False),
                    evidence_at.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    run_id,
                ),
            )
            return {
                "ok": True,
                "task": self._task_row_to_dict(
                    self._load_task(scope, task_id)  # type: ignore[arg-type]
                ),
            }
        row = self._load_task(scope, task_id)
        if row is None:
            return {"ok": False, "error": "task_not_found"}
        if expected_version is None:
            return {
                "ok": False,
                "error": "expected_version_required",
                "task": self._task_row_to_dict(row),
            }
        if int(row["version"]) != int(expected_version):
            return {
                "ok": False,
                "error": "version_conflict",
                "task": self._task_row_to_dict(row),
            }
        if row["archived_at"] and not restore:
            return {
                "ok": False,
                "error": "task_archived_needs_restore",
                "task": self._task_row_to_dict(row),
            }
        # waiting_on 只在 waiting 状态有意义：显式离开 waiting 且未另提交
        # waiting_on 时一并清空，避免完成/取消/转 open 的事项残留等待说明。
        effective_waiting_on = waiting_on
        if status is not None and status != "waiting" and waiting_on is None:
            effective_waiting_on = ""
        changes: dict[str, Any] = {}
        if goal and goal != str(row["goal"]):
            changes["goal"] = goal
        if status is not None and status != str(row["status"]):
            changes["status"] = status
        if effective_waiting_on is not None and effective_waiting_on != str(
            row["waiting_on"]
        ):
            changes["waiting_on"] = effective_waiting_on
        if evidence_summary and evidence_summary != str(row["evidence_summary"]):
            changes["evidence_summary"] = evidence_summary
        if evidence_message_ids and list(evidence_message_ids) != json.loads(
            row["evidence_message_ids"] or "[]"
        ):
            changes["evidence_message_ids"] = json.dumps(
                list(evidence_message_ids), ensure_ascii=False
            )
        if restore and row["archived_at"]:
            changes["archived_at"] = ""
            changes["archive_reason"] = ""
        if not changes:
            return {"ok": True, "task": self._task_row_to_dict(row)}
        # 证据时间来自被引用消息自身的时间戳：引用旧消息不会把任务刷新
        # 成新证据，也不回退已记录的更新证据时间（保留期只前移不回拨）。
        try:
            previous_evidence_at = datetime.fromisoformat(str(row["last_evidence_at"]))
        except ValueError:
            previous_evidence_at = None
        effective_evidence_at = evidence_at
        if previous_evidence_at is not None:
            effective_evidence_at = max(previous_evidence_at, evidence_at)
        changes["last_evidence_at"] = effective_evidence_at.isoformat()
        changes["updated_at"] = now.isoformat()
        changes["run_id"] = run_id
        assignments = ", ".join(f"{key} = ?" for key in changes)
        cursor = self.connection.execute(
            f"""
            UPDATE tasks SET {assignments}, version = version + 1
            WHERE id = ? AND scope_self = ? AND scope_contact = ?
              AND scope_conversation = ? AND version = ?
            """,
            (
                *changes.values(),
                task_id,
                scope.self_id,
                scope.contact_id,
                scope.conversation_id,
                int(expected_version),
            ),
        )
        if cursor.rowcount != 1:
            # 读与写之间有并发变更（更新/归档都递增 version）。本事务内
            # 没有已写入的其他语句，直接读回最新状态交由调用方决定，
            # 不静默覆盖；由外层事务统一回滚/提交。
            latest = self._load_task(scope, task_id)
            if latest is None:
                return {"ok": False, "error": "task_not_found"}
            if latest["archived_at"] and not restore:
                return {
                    "ok": False,
                    "error": "task_archived_needs_restore",
                    "task": self._task_row_to_dict(latest),
                }
            return {
                "ok": False,
                "error": "version_conflict",
                "task": self._task_row_to_dict(latest),
            }
        return {
            "ok": True,
            "task": self._task_row_to_dict(self._load_task(scope, task_id)),  # type: ignore[arg-type]
        }

    def lookup_tasks(
        self,
        *,
        scope: TaskScope,
        status: str = "",
        include_archived: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """查询当前作用域内的事项。查询不续期、不唤醒归档任务。"""
        if status and status not in TASK_STATUSES:
            return []
        conditions = [
            "scope_self = ?",
            "scope_contact = ?",
            "scope_conversation = ?",
        ]
        parameters: list[Any] = [
            scope.self_id,
            scope.contact_id,
            scope.conversation_id,
        ]
        if status:
            conditions.append("status = ?")
            parameters.append(status)
        if not include_archived:
            conditions.append("archived_at = ''")
        rows = self.connection.execute(
            f"""
            SELECT * FROM tasks WHERE {' AND '.join(conditions)}
            ORDER BY last_evidence_at DESC LIMIT ?
            """,
            (*parameters, max(1, min(int(limit), 50))),
        ).fetchall()
        return [self._task_row_to_dict(row) for row in rows]

    def active_tasks(
        self, scope: TaskScope, *, limit: int = 8
    ) -> list[dict[str, Any]]:
        """活跃快照：未归档的 open/waiting 事项。completed/cancelled 立即退出。"""
        rows = self.connection.execute(
            """
            SELECT * FROM tasks
            WHERE scope_self = ? AND scope_contact = ? AND scope_conversation = ?
              AND status IN ('open', 'waiting') AND archived_at = ''
            ORDER BY last_evidence_at DESC LIMIT ?
            """,
            (scope.self_id, scope.contact_id, scope.conversation_id, max(1, int(limit))),
        ).fetchall()
        return [self._task_row_to_dict(row) for row in rows]

    def archive_inactive_tasks(self, now: datetime, inactive_days: int) -> int:
        """把超过 inactive_days 无实质证据的 open/waiting 事项归档。

        归档只退出默认上下文，不改变业务状态；幂等，可重复执行。
        时间比较用解析后的 datetime，避免 ISO 字符串微观格式差异。
        候选扫描与写入之间可能有并发更新：归档 UPDATE 带 last_evidence_at
        和 version 条件并递增 version，刚拿到新证据的事项不会被归档，
        归档本身也会让读到旧版本的在途更新冲突而不是静默覆盖。
        """
        if inactive_days <= 0:
            return 0
        cutoff = now - timedelta(days=inactive_days)
        rows = self.connection.execute(
            """
            SELECT id, last_evidence_at, version FROM tasks
            WHERE status IN ('open', 'waiting') AND archived_at = ''
            """
        ).fetchall()
        archived = 0
        for row in rows:
            try:
                last_evidence_at = datetime.fromisoformat(row["last_evidence_at"])
            except ValueError:
                continue
            if last_evidence_at >= cutoff:
                continue
            cursor = self.connection.execute(
                """
                UPDATE tasks
                SET archived_at = ?, archive_reason = 'inactive_timeout',
                    updated_at = ?, version = version + 1
                WHERE id = ? AND archived_at = '' AND last_evidence_at = ?
                  AND version = ?
                """,
                (
                    now.isoformat(),
                    now.isoformat(),
                    str(row["id"]),
                    str(row["last_evidence_at"]),
                    int(row["version"]),
                ),
            )
            archived += cursor.rowcount
        self.connection.commit()
        return archived

    # Kept for callers that still read audit databases created by older releases.
    codex_run_counts_since = agent_run_counts_since
