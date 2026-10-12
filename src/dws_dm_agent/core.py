from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


# 单次 agent 运行的图片预算（用户可见上限）：最多 MAX_IMAGES 张、每张不超过
# MAX_IMAGE_BYTES。下载侧（service）与事件流读取上限（runners.runtime）都必须
# 从这两个常量推导，保证"允许下载的图片一定能被事件流完整读回"。
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 20 * 1024 * 1024


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
        self.connection.execute("PRAGMA journal_mode=WAL")
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

    # Kept for callers that still read audit databases created by older releases.
    codex_run_counts_since = agent_run_counts_since
