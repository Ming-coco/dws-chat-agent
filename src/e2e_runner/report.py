"""报告输出：控制台摘要 + JSON + Markdown。

控制台给人看，JSON 给机器读（agent 调用走这个），Markdown 方便贴到群里或工单。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .detect import FAIL, PASS, WARN
from .navigate import PageResult, RunResult

_ICON = {PASS: "✅", WARN: "⚠️ ", FAIL: "❌"}


def to_dict(result: RunResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "startedAt": result.started_at,
        "durationMs": result.duration_ms,
        "overall": result.overall,
        "error": result.error,
        "menuSource": result.menu_source,
        "menuTopCount": result.menu_top_count,
        "summary": {
            "total": len(result.pages),
            "passed": result.passed,
            "warned": result.warned,
            "failed": result.failed,
        },
        "pages": [
            {
                "name": page.name,
                "path": page.path,
                "level": page.verdict.level,
                "reason": page.verdict.reason,
                "layer": page.verdict.layer,
                "details": page.verdict.details,
                "durationMs": page.duration_ms,
                "consoleErrors": page.signal.console_errors,
                "pageErrors": page.signal.page_errors,
                "badRequests": [
                    {"status": item.status, "method": item.method, "url": item.url}
                    for item in page.signal.bad_requests
                ],
                "tableRows": page.signal.table_rows,
                "screenshot": page.signal.screenshot,
            }
            for page in result.pages
        ],
    }


def write_json(result: RunResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(to_dict(result), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def console_summary(result: RunResult) -> str:
    """控制台摘要。给人扫一眼就能知道结论。"""
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append(f"{result.name}  页面体检")
    lines.append(f"开始 {result.started_at}   耗时 {result.duration_ms / 1000:.1f}s")
    lines.append("=" * 64)

    if result.error:
        lines.append(f"❌ 运行失败: {result.error}")
        return "\n".join(lines)

    lines.append(
        f"结论 {_ICON.get(result.overall, '')} {result.overall}   "
        f"共 {len(result.pages)} 页   "
        f"通过 {result.passed} / 警告 {result.warned} / 失败 {result.failed}"
    )
    lines.append("")

    for page in result.pages:
        icon = _ICON.get(page.verdict.level, "")
        layer = f"[{page.verdict.layer}]" if page.verdict.layer else ""
        lines.append(f"{icon} {page.name:<28} {page.verdict.reason} {layer}")
        for detail in page.verdict.details[:2]:
            lines.append(f"      {detail}")

    return "\n".join(lines)


def write_markdown(result: RunResult, path: Path) -> None:
    """Markdown 摘要，方便贴到群里。"""
    data = to_dict(result)
    lines: list[str] = []
    lines.append(f"## {result.name} 页面体检")
    lines.append("")
    lines.append(f"- **时间**：{result.started_at}")
    lines.append(f"- **耗时**：{result.duration_ms / 1000:.1f}s")
    lines.append(f"- **结论**：{_ICON.get(result.overall, '')} {result.overall}")
    lines.append("")

    summary = data["summary"]
    lines.append("| 项 | 数量 |")
    lines.append("|---|---|")
    lines.append(f"| 访问页面 | {summary['total']} |")
    lines.append(f"| 通过 | {summary['passed']} |")
    lines.append(f"| 警告 | {summary['warned']} |")
    lines.append(f"| 失败 | {summary['failed']} |")
    lines.append("")

    problems = [page for page in result.pages if page.verdict.level != PASS]
    if problems:
        lines.append("### 问题明细")
        lines.append("")
        lines.append("| 页面 | 级别 | 归因 | 说明 |")
        lines.append("|---|---|---|---|")
        for page in problems:
            lines.append(
                f"| {page.name} | {page.verdict.level} | "
                f"{page.verdict.layer or '-'} | {page.verdict.reason} |"
            )
        lines.append("")

    lines.append("### 全部页面")
    lines.append("")
    lines.append("| 页面 | 状态 | 接口 | 表格行 | 耗时 |")
    lines.append("|---|---|---|---|---|")
    for page in result.pages:
        lines.append(
            f"| {page.name} | {_ICON.get(page.verdict.level, '')} {page.verdict.level} | "
            f"{len(page.signal.requests)} | {page.signal.table_rows} | "
            f"{page.duration_ms / 1000:.1f}s |"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
