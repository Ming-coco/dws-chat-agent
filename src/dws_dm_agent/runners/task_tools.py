"""task_record/task_lookup 的 CLI 后端，由 pi 扩展 pi_dws_tools.ts 按次拉起。

输入：环境变量 DWS_TASK_CONTEXT_FILE（优先，服务在 steer 进补充消息后
会原子重写）或 DWS_TASK_CONTEXT（会话启动时的初始快照），内容为 JSON：
db_path/self_id/contact_id/conversation_id/run_id/message_times（本运行
已覆盖消息的 id -> 时间戳映射）；
stdin 一行 JSON 请求
{"tool": "task_record"|"task_lookup", "tool_call_id": str, "args": {...}}。
输出：stdout 一行 JSON 结果，始终退出码 0，错误以 {"ok": false, "error"} 返回，
让模型读到失败原因而不是让工具调用挂掉。

同一 (run_id, tool_call_id) 的重复事件直接返回已落地的响应，不重复创建事项。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

# 本脚本由 pi_dws_tools.ts 以 `python <script>` 直接拉起，没有包上下文，
# 相对导入会直接 ImportError。把 src/ 加入 sys.path 后改用包内绝对导入，
# 不依赖安装方式或调用方 cwd（escalate_mcp.py 不导入本包，无此问题）。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dws_dm_agent.core import AuditStore, TaskScope, sanitize_text

_MAX_MESSAGE_IDS = 12


def _context_from_env() -> dict[str, Any]:
    # 优先读服务刷新过的上下文文件：steer 进补充消息后，可引用的证据
    # 消息集合应扩展为本运行已覆盖的最新消息，而不是会话启动时的快照。
    # 文件缺失或损坏时回退到环境变量里的初始快照，不让工具挂掉。
    file_path = os.environ.get("DWS_TASK_CONTEXT_FILE", "")
    if file_path:
        try:
            value = json.loads(Path(file_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = None
        if isinstance(value, Mapping):
            return dict(value)
    raw = os.environ.get("DWS_TASK_CONTEXT", "")
    if not raw:
        raise ValueError("DWS_TASK_CONTEXT is not configured")
    value = json.loads(raw)
    if not isinstance(value, Mapping):
        raise ValueError("DWS_TASK_CONTEXT must be a JSON object")
    return dict(value)


def _message_time_map(context: Mapping[str, Any]) -> dict[str, datetime] | str:
    """解析上下文的 message_times（消息 id -> 时间戳）。

    返回错误字符串表示上下文不可用；时间戳必须可解析且带时区，
    后续 max 比较才有意义。
    """
    raw = context.get("message_times")
    if not isinstance(raw, Mapping) or not raw:
        return "task_context_missing_message_times"
    mapping: dict[str, datetime] = {}
    for key, value in raw.items():
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return "task_context_invalid_message_time"
        if not parsed.tzinfo:
            return "task_context_invalid_message_time"
        mapping[str(key)] = parsed
    return mapping


def _clean_str(value: Any, max_chars: int) -> str:
    return sanitize_text(value, max_chars).strip()


def _optional_str(
    args: Mapping[str, Any], key: str, max_chars: int
) -> str | None:
    """读取可选字段：缺省或 null 返回 None（本次不提交该字段）。

    显式传空串按提交处理，用于 waiting_on 这类“传空即清空”的字段；
    goal/status/evidence_summary 的空串在后端按未提交处理。
    """
    value = args.get(key)
    if value is None:
        return None
    return _clean_str(value, max_chars)


def _clean_message_ids(value: Any) -> list[str]:
    """清洗模型提交的证据消息 ID；不做任何自动补齐。

    自动附加本运行全部消息 ID 会让无关确认也构成“新证据”而续期事项，
    因此这里只保留模型显式引用的 ID，再由调用方对照上下文校验。
    """
    ids: list[str] = []
    if isinstance(value, (list, tuple)):
        ids.extend(_clean_str(item, 120) for item in value)
    seen: set[str] = set()
    result: list[str] = []
    for item in ids:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result[:_MAX_MESSAGE_IDS]


def execute_request(request: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    """执行一次 task_record/task_lookup 请求；所有边界校验都在这里完成。"""
    tool = str(request.get("tool") or "")
    if tool not in {"task_record", "task_lookup"}:
        return {"ok": False, "error": f"unknown_tool: {tool or '(empty)'}"}
    scope = TaskScope(
        self_id=str(context.get("self_id") or ""),
        contact_id=str(context.get("contact_id") or ""),
        conversation_id=str(context.get("conversation_id") or ""),
    )
    if not all((scope.self_id, scope.contact_id, scope.conversation_id)):
        return {"ok": False, "error": "task_scope_missing"}
    db_path = str(context.get("db_path") or "")
    if not db_path:
        return {"ok": False, "error": "task_db_missing"}
    run_id = str(context.get("run_id") or "")
    tool_call_id = str(request.get("tool_call_id") or "")
    args = request.get("args")
    args = args if isinstance(args, Mapping) else {}

    store = AuditStore(Path(db_path))
    try:
        now = datetime.now().astimezone()
        evidence_ids: list[str] = []
        evidence_at = now
        if tool == "task_record":
            message_times = _message_time_map(context)
            if isinstance(message_times, str):
                return {"ok": False, "error": message_times}
            evidence_ids = _clean_message_ids(args.get("evidence_message_ids"))
            unknown = [
                item for item in evidence_ids if item not in message_times
            ]
            if unknown:
                # 未知 ID 一律拒绝，绝不带着“当前时间”兜底落库：那会把
                # 无法核实的引用伪装成最新证据，让事项凭空调新。
                return {
                    "ok": False,
                    "error": (
                        "unknown_evidence_message_id: "
                        + ", ".join(unknown[:3])
                    ),
                }
            # 证据时间取被引用消息自身的最新时间戳：引用旧消息，时间就
            # 是旧时间；未引用时才用本运行已覆盖消息的最新时间（新建
            # 事项的触发消息就是当前输入）。
            evidence_at = max(
                (message_times[item] for item in evidence_ids),
                default=max(message_times.values()),
            )
        if tool == "task_lookup":
            include_archived = bool(args.get("include_archived", False))
            # 收据与查询同一事务：同一工具事件重放返回同一份结果。
            response = store.apply_task_lookup(
                scope=scope,
                status=_clean_str(args.get("status"), 32),
                include_archived=include_archived,
                limit=_int_or(args.get("limit"), 20),
                now=now,
                run_id=run_id,
                tool_call_id=tool_call_id,
            )
        else:
            expected_version = args.get("expected_version")
            # 收据与事项变更同一事务落地：崩溃后要么都发生要么都不发生，
            # 重复事件不会重复创建或重复更新事项。
            response = store.apply_task_record(
                scope=scope,
                record={
                    "task_id": _clean_str(args.get("task_id"), 64),
                    "goal": _optional_str(args, "goal", 2000),
                    "status": _optional_str(args, "status", 32),
                    "waiting_on": _optional_str(args, "waiting_on", 500),
                    "evidence_summary": _optional_str(args, "evidence_summary", 2000),
                    "evidence_message_ids": evidence_ids,
                    "expected_version": (
                        int(expected_version)
                        if isinstance(expected_version, int)
                        and not isinstance(expected_version, bool)
                        else None
                    ),
                    "restore": bool(args.get("restore", False)),
                },
                evidence_at=evidence_at,
                now=now,
                run_id=run_id,
                tool_call_id=tool_call_id,
            )
        return response
    finally:
        store.close()


def _int_or(value: Any, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def main() -> int:
    try:
        context = _context_from_env()
        line = sys.stdin.readline()
        request = json.loads(line) if line.strip() else {}
        if not isinstance(request, dict):
            request = {}
        response = execute_request(request, context)
    except Exception as exc:  # noqa: BLE001 - CLI 边界：任何错误都以 JSON 返回
        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
