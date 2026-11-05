"""jira_issue 的 CLI 后端，由 pi 扩展 pi_jira.ts 按次拉起。

输入：stdin 一行 JSON
{"tool": "jira_issue", "args": {"issue": "CQNSHCYDN-1422", "max_comments": 10}}
输出：stdout 一行 JSON。始终退出码 0，错误以 {"ok": false, "error"} 返回，
让模型读到失败原因而不是让工具调用整体挂掉。

凭据只从环境变量读取，不进提示词、不进命令行参数，因此不会出现在模型上下文里：
  JIRA_PAT              必填，Jira Personal Access Token
  JIRA_BASE_URL         可选，默认 https://issue.hsjdata.com
  JIRA_TIMEOUT_SECONDS  可选，默认 30

只做读取：不建单、不改字段、不加评论。写操作必须另行授权。
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

DEFAULT_BASE_URL = "https://issue.hsjdata.com"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_COMMENTS = 10
MAX_MAX_COMMENTS = 50
MAX_DESCRIPTION_CHARS = 6000
MAX_COMMENT_CHARS = 1500

# 从 .env 兜底读取的键。服务只用 .env 解析 config.json 的 ${...}，
# 不会把它灌进子进程环境，所以这里自己读一次。
_ENV_KEYS = ("JIRA_PAT", "JIRA_BASE_URL", "JIRA_TIMEOUT_SECONDS")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Jira 单号：项目键大写字母开头，允许数字和下划线，后面接数字。
# 项目键长度放宽到 1 位起：真实 Jira 一般 2 位以上，但这里宁可多认。
_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]{0,31})-(\d{1,7})\b")
_BROWSE_RE = re.compile(r"/browse/([A-Z][A-Z0-9_]{0,31}-\d{1,7})")

_FIELDS = (
    "summary",
    "status",
    "resolution",
    "resolutiondate",
    "issuetype",
    "priority",
    "assignee",
    "reporter",
    "creator",
    "created",
    "updated",
    "duedate",
    "labels",
    "components",
    "fixVersions",
    "versions",
    "description",
    "comment",
    "parent",
    "subtasks",
    "issuelinks",
    "attachment",
    "project",
    "environment",
)


class JiraError(RuntimeError):
    """可预期的读取失败：凭据缺失、单据不存在、权限不足、超时。"""


def extract_issue_key(value: Any) -> str:
    """从单号或 browse 链接里取出规范单号；取不到返回空串。"""
    text = str(value or "").strip()
    if not text:
        return ""
    match = _BROWSE_RE.search(text)
    if match:
        return match.group(1)
    match = _KEY_RE.search(text)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return ""


def _adf_text(node: Any) -> str:
    """把 Atlassian Document Format 递归摊平成纯文本。

    Jira 9 的描述和评论可能是 ADF（嵌套 JSON），也可能是 wiki 标记字符串；
    两种都要能吃下，否则模型只能看到一堆 JSON。
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_text(item) for item in node)
    if not isinstance(node, Mapping):
        return ""

    kind = str(node.get("type") or "")
    if kind == "text":
        return str(node.get("text") or "")
    if kind == "hardBreak":
        return "\n"
    if kind == "rule":
        return "\n---\n"
    if kind == "mention":
        attrs = node.get("attrs")
        name = attrs.get("text") if isinstance(attrs, Mapping) else ""
        return f"@{name}" if name else "@某人"
    if kind in {"inlineCard", "blockCard", "embedCard"}:
        attrs = node.get("attrs")
        url = attrs.get("url") if isinstance(attrs, Mapping) else ""
        return str(url or "")
    if kind in {"mediaSingle", "media", "mediaGroup", "mediaInline"}:
        return "[附件]"

    inner = _adf_text(node.get("content"))
    if kind in {"paragraph", "heading", "codeBlock", "blockquote", "panel"}:
        return inner + "\n"
    if kind == "listItem":
        return f"- {inner.strip()}\n"
    if kind in {"bulletList", "orderedList"}:
        return inner
    if kind in {"tableRow", "tableCell", "tableHeader"}:
        return inner.strip() + " | "
    if kind == "table":
        return inner + "\n"
    return inner


def _render_rich(value: Any) -> str:
    """描述/评论字段统一转成可读文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return _adf_text(value).strip()
    if isinstance(value, list):
        return _adf_text(value).strip()
    return str(value).strip()


def _truncate(text: str, limit: int, note: str) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}\n…（{note}，已截断 {len(text) - limit} 字）"


def _name_of(value: Any) -> str:
    """Jira 的用户/对象字段常常是 {displayName}/{name}/{value} 其中之一。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("displayName", "name", "value", "key"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def _names_of(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    names = [name for name in (_name_of(item) for item in items) if name]
    return "、".join(names)


def _short_time(value: Any) -> str:
    text = str(value or "").strip()
    # Jira 返回 2026-11-03T18:20:31.000+0800；去掉毫秒和 T，便于阅读。
    if len(text) >= 16 and text[4] == "-" and text[10] == "T":
        return f"{text[:10]} {text[11:16]}"
    return text


def _read_env_file(path: str) -> dict[str, str]:
    """从 .env 里取出 JIRA_* 三项。

    服务和配置变量走的是两条路：服务只用 .env 解析 config.json 的 `${...}`，
    并不把它注入 os.environ，所以子进程看不到 .env。这里按需读一次，让部署
    方只需要在 .env 写一行 JIRA_PAT，且补令牌后无需重启服务。

    格式与服务端 `_read_env_file` 保持一致：忽略空行和 `#` 注释，允许 `export`
    前缀，成对引号会被剥掉。解析不了的行跳过而不是报错——这是兜底路径，
    不该因为一行无关内容让整个工具失败。
    """
    if not path:
        return {}
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not _ENV_KEY_RE.fullmatch(key) or key not in _ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


_ENV_CACHE: dict[str, dict[str, str]] = {}


def _setting(name: str) -> str:
    """环境变量优先，其次 .env；都取不到返回空串。"""
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    env_path = (os.environ.get("DWS_JIRA_ENV_FILE") or "").strip()
    if not env_path:
        return ""
    if env_path not in _ENV_CACHE:
        _ENV_CACHE[env_path] = _read_env_file(env_path)
    return (_ENV_CACHE[env_path].get(name) or "").strip()


def _request_json(url: str, token: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "dws-chat-agent/jira-tools",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001 - 读不出响应体不影响分类
            detail = ""
        if error.code == 404:
            raise JiraError("单据不存在，或当前账号没有查看权限") from error
        if error.code in (401, 403):
            raise JiraError("凭据无效或权限不足（检查 JIRA_PAT 是否过期）") from error
        raise JiraError(f"Jira 返回 HTTP {error.code}：{detail}") from error
    except urllib.error.URLError as error:
        raise JiraError(f"连不上 Jira：{error.reason}") from error
    except TimeoutError as error:
        raise JiraError(f"读取超时（{timeout:.0f} 秒）") from error
    except json.JSONDecodeError as error:
        raise JiraError("Jira 返回的不是合法 JSON") from error


def _render_links(links: Any) -> list[str]:
    lines: list[str] = []
    if not isinstance(links, list):
        return lines
    for link in links:
        if not isinstance(link, Mapping):
            continue
        kind = link.get("type")
        label = ""
        if isinstance(kind, Mapping):
            label = str(kind.get("outward") or kind.get("name") or "")
        other = link.get("outwardIssue") or link.get("inwardIssue")
        if isinstance(other, Mapping):
            key = str(other.get("key") or "")
            summary = ""
            fields = other.get("fields")
            if isinstance(fields, Mapping):
                summary = str(fields.get("summary") or "")
            lines.append(f"{label or '关联'} {key} {summary}".strip())
    return lines


def render_issue(issue: Mapping[str, Any], max_comments: int) -> str:
    """把一单据渲染成模型和人都能直接读的紧凑文本。"""
    fields = issue.get("fields")
    if not isinstance(fields, Mapping):
        raise JiraError("Jira 返回的单据缺少 fields")

    key = str(issue.get("key") or "")
    status = _name_of(fields.get("status"))
    resolution = _name_of(fields.get("resolution"))
    lines = [
        f"单号：{key}",
        f"标题：{str(fields.get('summary') or '').strip()}",
        f"类型：{_name_of(fields.get('issuetype'))}　状态：{status}"
        + (f"　解决结果：{resolution}" if resolution else ""),
        f"经办人：{_name_of(fields.get('assignee')) or '未指派'}"
        f"　报告人：{_name_of(fields.get('reporter'))}",
        f"创建：{_short_time(fields.get('created'))}"
        f"　更新：{_short_time(fields.get('updated'))}"
        + (
            f"　截止：{_short_time(fields.get('duedate'))}"
            if fields.get("duedate")
            else ""
        ),
    ]

    extra: list[str] = []
    priority = _name_of(fields.get("priority"))
    if priority:
        extra.append(f"优先级：{priority}")
    project = _name_of(fields.get("project"))
    if project:
        extra.append(f"项目：{project}")
    for field_name, title in (
        ("labels", "标签"),
        ("components", "组件"),
        ("fixVersions", "修复版本"),
        ("versions", "影响版本"),
    ):
        names = _names_of(fields.get(field_name))
        if names:
            extra.append(f"{title}：{names}")
    if extra:
        lines.append("　".join(extra))

    parent = fields.get("parent")
    if isinstance(parent, Mapping):
        parent_fields = parent.get("fields")
        parent_summary = (
            str(parent_fields.get("summary") or "")
            if isinstance(parent_fields, Mapping)
            else ""
        )
        lines.append(
            f"父单：{parent.get('key') or ''} {parent_summary}".strip()
        )

    subtasks = fields.get("subtasks")
    if isinstance(subtasks, list) and subtasks:
        lines.append(f"子任务（{len(subtasks)}）：")
        for item in subtasks[:20]:
            if not isinstance(item, Mapping):
                continue
            item_fields = item.get("fields")
            item_summary = (
                str(item_fields.get("summary") or "")
                if isinstance(item_fields, Mapping)
                else ""
            )
            item_status = (
                _name_of(item_fields.get("status"))
                if isinstance(item_fields, Mapping)
                else ""
            )
            lines.append(
                f"  - {item.get('key')} [{item_status}] {item_summary}".rstrip()
            )

    links = _render_links(fields.get("issuelinks"))
    if links:
        lines.append(f"关联（{len(links)}）：")
        lines.extend(f"  - {line}" for line in links[:20])

    attachments = fields.get("attachment")
    if isinstance(attachments, list) and attachments:
        names = [
            str(item.get("filename") or "")
            for item in attachments
            if isinstance(item, Mapping)
        ]
        names = [name for name in names if name]
        if names:
            more = f" 等 {len(names)} 个" if len(names) > 8 else ""
            lines.append(f"附件：{'、'.join(names[:8])}{more}")

    environment = _render_rich(fields.get("environment"))
    if environment:
        lines.append("环境：")
        lines.append(_truncate(environment, 800, "环境信息过长"))

    description = _render_rich(fields.get("description"))
    lines.append("描述：")
    lines.append(
        _truncate(description, MAX_DESCRIPTION_CHARS, "描述过长") if description else "（无描述）"
    )

    comments = fields.get("comment")
    rows: list[Mapping[str, Any]] = []
    if isinstance(comments, Mapping):
        raw_rows = comments.get("comments")
        if isinstance(raw_rows, list):
            rows = [row for row in raw_rows if isinstance(row, Mapping)]
        total = comments.get("total")
    else:
        total = None
    if rows:
        shown = rows[-max_comments:]
        header = f"评论（共 {total if isinstance(total, int) else len(rows)} 条，显示最近 {len(shown)} 条）："
        lines.append(header)
        for row in shown:
            author = _name_of(row.get("author"))
            body = _render_rich(row.get("body"))
            body = _truncate(body, MAX_COMMENT_CHARS, "评论过长")
            lines.append(f"  [{(author + ' ' + _short_time(row.get('created'))).strip()}]")
            for body_line in body.splitlines():
                lines.append(f"    {body_line}")
    return "\n".join(lines)


def fetch_issue(key: str, max_comments: int) -> str:
    """按单号读取并渲染；失败抛 JiraError。"""
    token = _setting("JIRA_PAT")
    if not token:
        raise JiraError(
            "未配置 JIRA_PAT，读不了 issue。请在部署目录的 .env 里填入 Jira "
            "个人访问令牌（JIRA_PAT=...）后重试；工具也能读进程环境变量。"
        )
    base_url = (_setting("JIRA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    try:
        timeout = float(_setting("JIRA_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT)
    except ValueError:
        timeout = DEFAULT_TIMEOUT

    query = urllib.parse.urlencode({"fields": ",".join(_FIELDS)})
    url = f"{base_url}/rest/api/2/issue/{urllib.parse.quote(key)}?{query}"
    payload = _request_json(url, token, timeout)
    if not isinstance(payload, Mapping):
        raise JiraError("Jira 返回的单据结构异常")
    return render_issue(payload, max_comments)


def _parse_args(raw: Any) -> tuple[str, int]:
    if not isinstance(raw, Mapping):
        raise JiraError("工具参数必须是对象")
    key = extract_issue_key(raw.get("issue"))
    if not key:
        raise JiraError(
            "没找到单号：传 CQNSHCYDN-1422 这样的单号，或 issue.hsjdata.com/browse/ 链接"
        )
    raw_comments = raw.get("max_comments", DEFAULT_MAX_COMMENTS)
    try:
        max_comments = int(raw_comments)
    except (TypeError, ValueError):
        max_comments = DEFAULT_MAX_COMMENTS
    return key, max(0, min(MAX_MAX_COMMENTS, max_comments))


def handle(request: Any) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        return {"ok": False, "error": "请求必须是 JSON 对象"}
    name = str(request.get("tool") or "")
    if name not in {"jira_issue", ""}:
        return {"ok": False, "error": f"不支持的工具：{name}"}
    try:
        key, max_comments = _parse_args(request.get("args"))
        return {"ok": True, "key": key, "text": fetch_issue(key, max_comments)}
    except JiraError as error:
        return {"ok": False, "error": str(error)}


def main() -> None:
    # 工具后端始终以 stdout 一行 JSON 回报，退出码保持 0，
    # 否则 pi 会把它当成工具崩溃而不是可转述的失败原因。
    try:
        # stdin 同样按 UTF-8 字节读，避免 Windows 管道默认 cp936
        # 把调用方传进来的非 ASCII 参数解成乱码。
        raw_line = sys.stdin.buffer.readline().decode("utf-8", "replace")
        request = json.loads(raw_line) if raw_line.strip() else {}
        result = handle(request)
    except json.JSONDecodeError as error:
        result = {"ok": False, "error": f"请求不是合法 JSON：{error}"}
    except Exception as error:  # noqa: BLE001 - 兜底，避免工具调用挂掉
        result = {"ok": False, "error": f"内部错误：{type(error).__name__}: {error}"}
    # 必须直接写 UTF-8 字节：Windows 下管道 stdout 默认 cp936，
    # 用 print() 会把中文错误文本编码坏，模型读到乱码就判断不了原因。
    payload = json.dumps(result, ensure_ascii=False) + "\n"
    sys.stdout.buffer.write(payload.encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
