from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse


@dataclass(frozen=True)
class Contact:
    alias: str
    display_name: str
    user_id: str
    open_dingtalk_id: str


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


@dataclass
class ChangeEvidence:
    repo: str
    worktree: str
    branch: str
    base_sha: str
    head_sha: str
    commits: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    pushed_to: list[str] = field(default_factory=list)
    verified: bool = False
    warning: str = ""


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
        or f"dm:{contact.user_id}"
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
_URL = re.compile(r"https?://[^\s<>\]\[\"']+", re.IGNORECASE)


class SecurityGate:
    """Deterministic, zero-token gate. Uncertain business requests pass to the agent."""

    def __init__(
        self,
        max_chars: int,
        allowed_execution_domains: Sequence[str],
        allowed_reference_domains: Sequence[str],
    ) -> None:
        self.max_chars = max_chars
        self.allowed_execution_domains = tuple(x.lower().lstrip(".") for x in allowed_execution_domains)
        self.allowed_reference_domains = tuple(x.lower().lstrip(".") for x in allowed_reference_domains)
        self._hard_patterns: list[tuple[str, re.Pattern[str]]] = [
            (
                "prompt_injection",
                re.compile(
                    r"(?:忽略|绕过|覆盖|无视).{0,30}(?:系统|上文|之前|安全|规则|指令|prompt|提示词)",
                    re.IGNORECASE | re.DOTALL,
                ),
            ),
            (
                "credential_exfiltration",
                re.compile(
                    r"(?:读取|窃取|导出|上传|发给|贴出|泄露|回传).{0,50}(?:\.ssh|私钥|助记词|密码|口令|token|cookie|credential|keychain|环境变量|密钥|凭据)",
                    re.IGNORECASE | re.DOTALL,
                ),
            ),
            (
                "destructive_command",
                re.compile(
                    r"(?:rm\s+-rf\s+(?:/|~|\$HOME)|git\s+(?:push\s+--force|reset\s+--hard)|DROP\s+(?:DATABASE|SCHEMA)|TRUNCATE\s+TABLE|mkfs\b|dd\s+if=|:\(\)\s*\{\s*:\|:&\s*\};:|curl\b[^\n|]{0,300}\|\s*(?:sh|bash))",
                    re.IGNORECASE,
                ),
            ),
            (
                "offensive_intrusion",
                re.compile(
                    r"(?:帮我|替我|直接|执行|写个|生成).{0,30}(?:入侵|提权|绕过鉴权|爆破密码|盗取数据|植入后门|反弹\s*shell|勒索|木马)",
                    re.IGNORECASE | re.DOTALL,
                ),
            ),
            (
                "metadata_exfiltration",
                re.compile(r"169\.254\.169\.254|metadata\.google\.internal", re.IGNORECASE),
            ),
        ]

    @staticmethod
    def _host_allowed(host: str, domains: Sequence[str]) -> bool:
        host = host.lower().rstrip(".")
        if not host:
            return False
        try:
            address = ipaddress.ip_address(host)
            return address.is_private or address.is_loopback
        except ValueError:
            pass
        return any(host == domain or host.endswith(f".{domain}") for domain in domains)

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
        for reason, pattern in self._hard_patterns:
            if pattern.search(combined):
                return GateResult(
                    "refuse",
                    reason,
                    "这个请求涉及越权、敏感信息或破坏性操作，我不能代为执行。如果是正常内部需求，请说明目标环境、授权范围和可回滚方案。",
                )

        execution_words = re.search(
            r"(?:执行|调用|请求|curl|wget|POST|PUT|PATCH|DELETE|上传|回传|发到|发送到|提交到)",
            combined,
            re.IGNORECASE,
        )
        sensitive_words = re.search(
            r"(?:token|cookie|密码|口令|凭据|密钥|客户数据|用户数据|内部数据)",
            combined,
            re.IGNORECASE,
        )
        if execution_words:
            for raw_url in _URL.findall(combined):
                host = (urlparse(raw_url).hostname or "").lower()
                if not self._host_allowed(host, self.allowed_execution_domains):
                    if sensitive_words or not self._host_allowed(host, self.allowed_reference_domains):
                        return GateResult(
                            "refuse",
                            "untrusted_external_execution",
                            "这个请求要向未授权的外部地址执行调用或传输数据，我不能代为执行。请提供内部域名、测试环境和明确授权。",
                        )
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
                session_id TEXT NOT NULL
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
            "events": "request_preview TEXT NOT NULL DEFAULT ''",
            "runs": "request_preview TEXT NOT NULL DEFAULT ''",
        }
        for table, column in migrations.items():
            names = {
                row["name"]
                for row in self.connection.execute(f"PRAGMA table_info({table})")
            }
            if "request_preview" not in names:
                self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
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
                event.contact.user_id,
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
    ) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO outgoing(
                send_uuid, message_id, conversation_id, contact_user_id,
                sent_at, content_sha256, session_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                send_uuid,
                message_id,
                conversation_id,
                contact_user_id,
                sent_at.isoformat(),
                sha256_text(canonical_message_text(content)),
                session_id,
            ),
        )
        self.connection.commit()

    def remove_outgoing(self, send_uuid: str) -> None:
        self.connection.execute("DELETE FROM outgoing WHERE send_uuid = ?", (send_uuid,))
        self.connection.commit()

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
            SELECT send_uuid, message_id, sent_at, content_sha256 FROM outgoing
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
            item for item in candidates if item[2] and item[1] <= tolerance_seconds
        ]
        selected = min(content_candidates, key=lambda item: item[1], default=None)
        if selected is None:
            legacy_candidates = [
                item for item in candidates if not item[0]["message_id"] and item[1] <= 3
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
        changes: Sequence[Mapping[str, Any]],
        validation: Sequence[str],
        external_calls: Sequence[str],
        warnings: Sequence[str],
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
                contact.user_id,
                contact.display_name,
                started_at.isoformat(),
                finished_at.isoformat(),
                action,
                status,
                sanitize_text(handled, 1000),
                sanitize_text(reason, 1000),
                json.dumps(list(changes), ensure_ascii=False),
                json.dumps([sanitize_text(x, 1000) for x in validation], ensure_ascii=False),
                json.dumps([sanitize_text(x, 1000) for x in external_calls], ensure_ascii=False),
                json.dumps([sanitize_text(x, 1000) for x in warnings], ensure_ascii=False),
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


def normalize_decision(value: Mapping[str, Any]) -> dict[str, Any]:
    action = str(value.get("action") or "handoff")
    if action not in {"reply", "no_reply", "refuse", "handoff"}:
        action = "handoff"
    delivery = str(value.get("delivery") or "")
    if delivery not in {"none", "feature", "attachment", "dev", "test"}:
        delivery = "test" if value.get("changes") else "none"
    decision: dict[str, Any] = {
        "action": action,
        "delivery": delivery,
        "reply": sanitize_text(value.get("reply"), 6000),
        "handled": sanitize_text(value.get("handled"), 1000),
        "reason": sanitize_text(value.get("reason"), 1000),
        "changes": [],
        "validation": [sanitize_text(x, 1000) for x in value.get("validation", []) if str(x).strip()][
            :50
        ],
        "external_calls": [
            sanitize_text(x, 1000) for x in value.get("external_calls", []) if str(x).strip()
        ][:50],
        "warnings": [sanitize_text(x, 1000) for x in value.get("warnings", []) if str(x).strip()][
            :50
        ],
    }
    for raw in value.get("changes", []):
        if not isinstance(raw, Mapping):
            continue
        decision["changes"].append(
            {
                "repo": sanitize_text(raw.get("repo"), 300),
                "worktree": sanitize_text(raw.get("worktree"), 1000),
                "branch": sanitize_text(raw.get("branch"), 300),
                "base_sha": sanitize_text(raw.get("base_sha"), 100),
                "head_sha": sanitize_text(raw.get("head_sha"), 100),
                "commits": [sanitize_text(x, 100) for x in raw.get("commits", []) if str(x)][:100],
                "files": [sanitize_text(x, 1000) for x in raw.get("files", []) if str(x)][:500],
                "pushed_to": [
                    sanitize_text(x, 300) for x in raw.get("pushed_to", []) if str(x)
                ][:50],
            }
        )
    if decision["action"] != "no_reply" and not decision["reply"]:
        decision["action"] = "handoff"
        decision["reply"] = "这个问题需要我本人确认一下，我先不代为做确定性答复。"
        decision["warnings"].append("模型未提供可发送正文，已转人工")
    return decision


def evidence_to_mapping(evidence: ChangeEvidence) -> dict[str, Any]:
    return {
        "repo": evidence.repo,
        "worktree": evidence.worktree,
        "branch": evidence.branch,
        "base_sha": evidence.base_sha,
        "head_sha": evidence.head_sha,
        "commits": evidence.commits,
        "files": evidence.files,
        "pushed_to": evidence.pushed_to,
        "verified": evidence.verified,
        "warning": evidence.warning,
    }


def render_reply(decision: Mapping[str, Any], changes: Sequence[ChangeEvidence]) -> str:
    return sanitize_text(decision.get("reply"), 6000)
