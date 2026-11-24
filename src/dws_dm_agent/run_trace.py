"""单次运行的调用链记录。

一次处理从收到消息到投递完成，中间会经历若干个阶段和上百次工具调用。
出问题时最需要知道的是「这 18 分钟到底在干什么、最后被什么拦下的」，
而原来的审计库只有开始和结束时间。

这里记录三样东西：

  1. 阶段：前台判断、执行、校验、投递各自的起止与耗时
  2. 步骤：每一次工具调用和关键事件，含参数、耗时、结果摘要
  3. 拦截：被 reply_guard 拦下的原文全文

写入按行追加到 JSONL，跑完再落一条汇总。用 JSONL 而不是塞进 SQLite，
是因为步骤数量不确定，几十到几百条，而且不需要按字段查询——查询走汇总。

保留期由配置决定，默认 7 天；超期文件在服务启动时清理。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# 步骤类型。审计和界面都按这个分类着色/折叠。
STEP_INPUT = "input"        # 对方发来的消息
STEP_TOOL = "tool"          # 工具调用
STEP_THOUGHT = "thought"    # 模型思考过程
STEP_REPLY = "reply"        # 模型输出的文本（中间陈述或最终正文）
STEP_STAGE = "stage"        # 阶段边界
STEP_GUARD = "guard"        # 校验器介入
STEP_SEND = "send"          # 投递
STEP_MARK = "mark"          # 终态标记（为什么停在这里）

# 默认保留天数，配置里可以覆盖。
DEFAULT_RETENTION_DAYS = 7


@dataclass
class Step:
    """一条调用链记录。"""

    kind: str
    name: str
    started_ms: int
    duration_ms: int = 0
    ok: bool = True
    # 工具参数、模型输出、拦截原因等，全量保留：
    # 只留摘要的话，下次还得靠猜。
    content: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "started": self.started_ms,
            "duration": self.duration_ms,
            "ok": self.ok,
            "content": self.content,
        }
        if self.extra:
            data["extra"] = self.extra
        return data


class RunTrace:
    """单次运行的调用链。线程安全到「同一个 run 只在一个任务里写」这个程度。"""

    def __init__(
        self,
        root: Path,
        trace_id: str,
        contact: str,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self.root = root
        # trace_id 是这条消息的全局唯一 id，贯穿整个处理过程。
        # 不用 session_id 关联：一次处理会经过前台和若干次执行，
        # 各自是独立的会话，只有 trace_id 能把它们串起来。
        self.trace_id = trace_id
        self.contact = contact
        # 对方的原始输入。没有它整条链没有起点，
        # 事后看不出「为什么要做这些事」。
        self.input = ""
        self.retention_days = max(1, int(retention_days))
        self.started_at = datetime.now()
        self._start = time.monotonic()
        # 与 _monotonic_ms() 同一把尺子的基准点：调用方传进来的可能是
        # 绝对值（开机以来的毫秒数），这里换算成相对本次处理的偏移。
        self._base_ms = int(self._start * 1000)
        self._steps: list[Step] = []
        # 最终结果：sent / skipped / error / human_cooldown 等。
        # 由调用方在处理结束时写入，落盘时带上，否则记录里看不出这次到底成没成。
        self.outcome: str = ""
        self.outcome_reason: str = ""
        self._stage_open: dict[str, int] = {}
        self._closed = False

    # ---- 记录 ----

    def _elapsed_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)

    def stage_begin(self, name: str) -> None:
        self._stage_open[name] = self._elapsed_ms()

    def stage_end(self, name: str, *, ok: bool = True) -> None:
        begin = self._stage_open.pop(name, None)
        if begin is None:
            return
        self._steps.append(
            Step(
                kind=STEP_STAGE,
                name=name,
                started_ms=begin,
                duration_ms=self._elapsed_ms() - begin,
                ok=ok,
            )
        )

    def tool(
        self,
        name: str,
        started_ms: int,
        duration_ms: int,
        *,
        content: str = "",
        ok: bool = True,
    ) -> None:
        self._steps.append(
            Step(
                kind=STEP_TOOL,
                name=name,
                started_ms=self._offset(started_ms),
                duration_ms=max(0, duration_ms),
                ok=ok,
                content=content,
            )
        )

    def _offset(self, started_ms: int) -> int:
        """把绝对时间换成相对本次处理开始的偏移。

        调用方用的是 time.monotonic() 的毫秒值（开机以来的时长），
        直接展示会变成几万分钟。比基准点还早的（理论上不该出现）
        一律归零，避免出现负数时间轴。
        """
        try:
            value = int(started_ms)
        except (TypeError, ValueError):
            return 0
        if value <= 0:
            return 0
        # 已经很小说明调用方传的就是偏移，原样返回
        if value < 86_400_000:
            return value
        return max(0, value - self._base_ms)

    def set_input(self, content: str) -> None:
        """记录对方发来的内容。批次里可能有多条，用换行拼接。"""
        self.input = content or ""

    def thought(self, text: str, started_ms: int, duration_ms: int = 0) -> None:
        """模型的一段思考。text 是 thinking_* 事件累积的全文。"""
        body = (text or "").strip()
        if not body:
            return
        self._steps.append(
            Step(
                kind=STEP_THOUGHT,
                name="思考",
                started_ms=self._offset(started_ms),
                duration_ms=max(0, duration_ms),
                content=body,
            )
        )

    def reply(self, text: str, *, final: bool = False) -> None:
        """模型输出的一段文本。final 表示这是最终正文。"""
        body = (text or "").strip()
        if not body:
            return
        self._steps.append(
            Step(
                kind=STEP_REPLY,
                name="最终回复" if final else "中间输出",
                started_ms=self._elapsed_ms(),
                content=body,
            )
        )

    def guard(
        self, name: str, *, blocked: bool, content: str, extra: dict[str, Any] | None = None
    ) -> None:
        """校验器介入。content 放被拦下的原文全文。"""
        self._steps.append(
            Step(
                kind=STEP_GUARD,
                name=name,
                started_ms=self._elapsed_ms(),
                ok=not blocked,
                content=content,
                extra=extra or {},
            )
        )

    def set_outcome(self, status: str, reason: str = "") -> None:
        """记录本次处理的最终结果，由服务在处理结束时调用。"""
        self.outcome = status
        self.outcome_reason = reason or self.outcome_reason

    def mark(self, name: str, *, ok: bool, content: str = "") -> None:
        """记录一个终态标记：为什么这次处理停在这里。

        人工接管、主动沉默、重试耗尽都走这里，
        让轨迹能自解释，而不是看起来像崩了。
        """
        self._steps.append(
            Step(
                kind=STEP_MARK,
                name=name,
                started_ms=self._elapsed_ms(),
                ok=ok,
                content=content,
            )
        )

    def finish_with(
        self, status: str, reason: str = "", content: str = ""
    ) -> None:
        """终态：记一条 mark 再落盘。"""
        self.set_outcome(status, reason)
        self.mark(reason or status, ok=status in {"sent"}, content=content)

    def send(self, *, content: str, status: str) -> None:
        self._steps.append(
            Step(
                kind=STEP_SEND,
                name=status,
                started_ms=self._elapsed_ms(),
                ok=status == "sent",
                content=content,
            )
        )

    # ---- 落盘 ----

    def finish(self, *, status: str = "", reason: str = "") -> Path | None:
        """写出汇总文件。重复调用无副作用。"""
        if self._closed:
            return None
        self._closed = True

        # 没关的阶段补上，避免时间轴上出现空洞
        for name in list(self._stage_open):
            self.stage_end(name)

        payload = {
            "traceId": self.trace_id,
            "input": self.input,
            "contact": self.contact,
            "startedAt": self.started_at.isoformat(timespec="seconds"),
            "totalMs": self._elapsed_ms(),
            "status": status or self.outcome,
            "reason": reason or self.outcome_reason,
            "retentionDays": self.retention_days,
            "steps": [step.to_dict() for step in self._steps],
        }

        self.root.mkdir(parents=True, exist_ok=True)
        stamp = self.started_at.strftime("%Y%m%d-%H%M%S")
        # 用落盘时的 session_id：创建时还不知道真实会话，那时写进文件名
        # 的话界面按 session 匹配不上。
        safe_id = "".join(
            char if char.isalnum() or char in "-_" else "_" for char in self.trace_id
        ) or "unknown"
        path = self.root / f"{safe_id}.json"
        # 写临时文件再替换：界面随时可能在读，不能让它读到半个文件
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(temp, path)
        return path

    def summary(self) -> dict[str, Any]:
        """给首页列表用的摘要，不含步骤明细。"""
        tool_count = sum(1 for step in self._steps if step.kind == STEP_TOOL)
        slowest = max(
            (step for step in self._steps if step.kind == STEP_TOOL),
            key=lambda step: step.duration_ms,
            default=None,
        )
        return {
            "traceId": self.trace_id,
            "contact": self.contact,
            "startedAt": self.started_at.isoformat(timespec="seconds"),
            "totalMs": self._elapsed_ms(),
            "toolCount": tool_count,
            "slowestTool": (
                {"name": slowest.name, "duration": slowest.duration_ms}
                if slowest
                else None
            ),
            "blocked": any(
                step.kind == STEP_GUARD and not step.ok for step in self._steps
            ),
        }


def purge_expired(root: Path, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """删掉超过保留期的调用链文件，返回删除数量。

    服务启动时调一次即可：这些文件只在排查时用，不需要常驻。
    """
    if not root.is_dir():
        return 0
    cutoff = datetime.now() - timedelta(days=max(1, int(retention_days)))
    removed = 0
    for path in root.glob("*.json"):
        try:
            # 用文件名里的时间戳判断，比 mtime 可靠：
            # 拷贝或同步会改 mtime，但改不了文件名。
            stamp = path.stem.split("-", 2)
            if len(stamp) < 2:
                continue
            created = datetime.strptime(stamp[0] + stamp[1], "%Y%m%d%H%M%S")
        except (ValueError, IndexError):
            continue
        if created < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    for temp in root.glob("*.tmp"):
        try:
            temp.unlink()
        except OSError:
            pass
    return removed


def load_trace(path: Path) -> dict[str, Any] | None:
    """读一份调用链。界面展开时调用。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_traces(root: Path, limit: int = 50) -> list[dict[str, Any]]:
    """列出最近的调用链摘要，按时间倒序。"""
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), reverse=True)[:limit]:
        data = load_trace(path)
        if not data:
            continue
        steps = data.get("steps") or []
        tools = [step for step in steps if step.get("kind") == STEP_TOOL]
        slowest = max(tools, key=lambda step: step.get("duration", 0), default=None)
        items.append(
            {
                "file": path.name,
                "traceId": data.get("traceId", ""),
                "contact": data.get("contact", ""),
                "startedAt": data.get("startedAt", ""),
                "totalMs": data.get("totalMs", 0),
                "status": data.get("status", ""),
                "reason": data.get("reason", ""),
                "toolCount": len(tools),
                "slowestTool": (
                    {"name": slowest.get("name"), "duration": slowest.get("duration", 0)}
                    if slowest
                    else None
                ),
                "blocked": any(
                    step.get("kind") == STEP_GUARD and not step.get("ok", True)
                    for step in steps
                ),
            }
        )
    return items
