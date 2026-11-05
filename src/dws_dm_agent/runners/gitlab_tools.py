#!/usr/bin/env python3
"""只读 GitLab 后端：查项目、群组、保护分支、流水线、失败作业日志。

设计约束（与 jira_tools.py 一致）：

- 只用标准库。能力随仓库分发，换一台机器拉下代码就能用，不需要 pip install。
- 凭据只从进程环境变量或部署目录的 .env 读取，不走命令行参数，
  因此不会出现在进程列表里，也不会进入模型的上下文。
- 包内不写死任何仓库路径、服务名或主机地址——项目一律由调用方传入，
  这样同一份实现能被任意项目复用。
- 任何失败都以中文原因返回 JSON，退出码始终为 0；由 pi 扩展决定怎么呈现。

三种查询：gitlab_project（单个项目）、gitlab_group（群组下的项目清单）、
gitlab_pipeline（某次流水线及其失败作业）。

stdin 读一行 JSON：{"tool": "gitlab_project", "args": {...}}
stdout 写一行 JSON：{"ok": true, "text": "..."} 或 {"ok": false, "error": "..."}
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
from typing import Any

DEFAULT_BASE_URL = ""
TIMEOUT_SECONDS = 30.0

MAX_PIPELINES = 5
MAX_GROUP_PROJECTS = 20
MAX_IMAGE_JOBS = 10
MAX_FAILED_JOBS = 6
MAX_LOG_LINES = 40
MAX_LOG_CHARS = 4000

_ACCESS_LEVELS = {
    0: "无权限",
    5: "最小",
    10: "访客",
    20: "报告者",
    30: "开发者",
    40: "维护者",
    50: "所有者",
}

_STATUS_NAMES = {
    "created": "已创建",
    "waiting_for_resource": "等资源",
    "preparing": "准备中",
    "pending": "排队中",
    "running": "运行中",
    "success": "成功",
    "failed": "失败",
    "canceled": "已取消",
    "cancelled": "已取消",
    "skipped": "跳过",
    "manual": "待手动",
    "scheduled": "已排期",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# 构建日志里绝大多数是下载进度和「从哪下载的」这类噪音，真正的原因被埋在最后。
# 这些行不该占掉尾部窗口，否则模型看到的全是 Progress。
_LOG_NOISE_RE = re.compile(
    r"^\s*(?:Progress \(\d+\)|Downloaded from|Downloading from|\[INFO\] Downloading"
    r"|\[INFO\] Downloaded|Receiving objects|Resolving deltas|remote:"
    r"|section_start:|section_end:|Cleaning up project directory"
    r"|Running with gitlab-runner|Preparing the .* executor|Using Docker executor)"
)

# kaniko 推送时打的就是这一行；镜像是构建产物的唯一权威来源，不靠规则拼。
_IMAGE_PUSH_RE = re.compile(r"Pushing image to\s+(\S+)")

_ENV_KEYS = ("GITLAB_TOKEN", "GITLAB_BASE_URL")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# 与其他内置能力共用同一个部署目录 .env；专用变量优先，便于单独排查。
_ENV_FILE_VARS = ("DWS_GITLAB_ENV_FILE", "DWS_AGENT_ENV_FILE")


class GitlabError(Exception):
    """可直接展示给模型的失败原因。"""


class GitlabNotFound(GitlabError):
    """目标不存在（或不给当前账号看）。用它区分「查错类型」和「真出错」。"""


def _read_env_file(path: str) -> dict[str, str]:
    """按需读部署目录的 .env，只取 GITLAB_* 两项。

    服务端只用 .env 展开 config.json 的 `${...}`，并不把它注入 os.environ，
    子进程因此看不到 .env。这里自己读一次，好处是补令牌不需要重启服务。
    解析不了的行直接跳过——这是兜底路径，不该因无关内容整体失败。
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
    """环境变量优先，其次部署目录的 .env；都取不到返回空串。"""
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    for variable in _ENV_FILE_VARS:
        env_path = (os.environ.get(variable) or "").strip()
        if not env_path:
            continue
        if env_path not in _ENV_CACHE:
            _ENV_CACHE[env_path] = _read_env_file(env_path)
        value = (_ENV_CACHE[env_path].get(name) or "").strip()
        if value:
            return value
    return ""


def _base_url() -> str:
    configured = _setting("GITLAB_BASE_URL") or DEFAULT_BASE_URL
    configured = configured.strip().rstrip("/")
    if not configured:
        raise GitlabError(
            "未配置 GITLAB_BASE_URL。请在部署目录的 .env 里写 "
            "GITLAB_BASE_URL=https://你的 GitLab 地址"
        )
    return configured


def _token() -> str:
    token = _setting("GITLAB_TOKEN")
    if token:
        return token
    raise GitlabError(
        "未配置 GITLAB_TOKEN，查不了流水线。请在部署目录的 .env 里填入 "
        "GitLab 个人访问令牌（GITLAB_TOKEN=...，需要 read_api 权限）后重试；"
        "工具也能读进程环境变量。"
    )


def _open(path: str) -> Any:
    url = _base_url() + path
    request = urllib.request.Request(
        url,
        headers={
            "PRIVATE-TOKEN": _token(),
            "Accept": "application/json",
            "User-Agent": "dws-chat-agent/gitlab-tools",
        },
    )
    try:
        return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001 - 读不出响应体不影响分类
            detail = ""
        if error.code == 401:
            raise GitlabError("凭据无效（检查 GITLAB_TOKEN 是否过期或被吊销）") from error
        if error.code == 403:
            raise GitlabError("当前账号没有这个项目的访问权限") from error
        if error.code == 404:
            raise GitlabNotFound("项目或群组不存在，或当前账号看不到它") from error
        raise GitlabError(f"GitLab 返回 HTTP {error.code}：{detail}") from error
    except urllib.error.URLError as error:
        raise GitlabError(f"连不上 GitLab：{error.reason}") from error
    except TimeoutError as error:
        raise GitlabError(f"读取超时（{TIMEOUT_SECONDS:.0f} 秒）") from error


def _request_json(path: str) -> Any:
    with _open(path) as response:
        try:
            return json.loads(response.read().decode("utf-8", "replace"))
        except json.JSONDecodeError as error:
            raise GitlabError("GitLab 返回的不是合法 JSON") from error


def _request_text(path: str) -> str:
    with _open(path) as response:
        return response.read().decode("utf-8", "replace")


def _short_time(value: Any) -> str:
    text = str(value or "").strip()
    # GitLab 返回 2026-11-05T09:13:40.757+08:00；保留到分钟即可。
    if len(text) >= 16 and text[4] == "-" and text[10] == "T":
        return f"{text[:10]} {text[11:16]}"
    return text


def _level_name(value: Any) -> str:
    try:
        code = int(value)
    except (TypeError, ValueError):
        return str(value or "-")
    return _ACCESS_LEVELS.get(code, str(code))


def _status_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    return f"{_STATUS_NAMES.get(text, text)}({text})"


def _path_reference(value: Any, noun: str) -> str:
    """把项目/群组标识统一成 API 可用的形式。

    接受 `group/project`、完整网页地址、或纯数字 id；
    返回可直接拼进 URL 的字符串。
    """
    text = str(value or "").strip()
    if not text:
        raise GitlabError(f"没有给{noun}，需要 namespace/{noun} 或数字 id")
    if text.isdigit():
        return text
    if "://" in text:
        parts = urllib.parse.urlsplit(text)
        text = parts.path or ""
    text = text.strip().strip("/")
    for marker in ("/-/", "/blob/", "/tree/"):
        if marker in text:
            text = text.split(marker)[0]
    text = re.sub(r"\.git$", "", text)
    if "/" not in text:
        raise GitlabError(f"认不出{noun} {value!r}，需要 namespace/名称 形式")
    return urllib.parse.quote(text, safe="")


def project_reference(value: Any) -> str:
    return _path_reference(value, "项目")


def group_reference(value: Any) -> str:
    return _path_reference(value, "群组")


def _maybe_group(reference: str) -> dict[str, Any] | None:
    """项目查不到时，试探一下同名群组。查不到或出错都返回 None。"""
    try:
        payload = _request_json(f"/api/v4/groups/{reference}")
    except GitlabError:
        return None
    return payload if isinstance(payload, dict) else None


def resolve_project(project: Any) -> dict[str, Any]:
    reference = project_reference(project)
    try:
        payload = _request_json(f"/api/v4/projects/{reference}")
    except GitlabNotFound as error:
        # 网页上的链接经常指向群组（命名空间），而不是某个项目。直接说「不存在」
        # 会让人以为没权限，所以顺手探一下群组，给出可执行的下一步。
        group = _maybe_group(reference)
        if group is not None:
            raise GitlabError(
                f"{project} 是一个群组而不是项目（full_path="
                f"{group.get('full_path')}）。用 gitlab_group 查这个群组下面有哪些项目，"
                "再从里面挑具体的 namespace/项目名 查流水线。"
            ) from error
        raise
    if not isinstance(payload, dict):
        raise GitlabError("GitLab 返回的项目信息格式不对")
    return payload


def resolve_group(group: Any) -> dict[str, Any]:
    payload = _request_json(f"/api/v4/groups/{group_reference(group)}")
    if not isinstance(payload, dict):
        raise GitlabError("GitLab 返回的群组信息格式不对")
    return payload


def _get_with_total(path: str) -> tuple[Any, int | None]:
    """取 JSON 和 X-Total 头。总数拿不到时返回 None。"""
    with _open(path) as response:
        raw_total = response.headers.get("X-Total")
        try:
            data = json.loads(response.read().decode("utf-8", "replace"))
        except json.JSONDecodeError as error:
            raise GitlabError("GitLab 返回的不是合法 JSON") from error
    try:
        total = int(raw_total) if raw_total else None
    except (TypeError, ValueError):
        total = None
    return data, total


def _group_projects(group_id: int, recursive: bool) -> tuple[list[dict[str, Any]], int | None]:
    """群组下的项目。recursive=True 时含子群组。"""
    path = (
        f"/api/v4/groups/{group_id}/projects?per_page=100"
        "&order_by=last_activity_at"
        "&include_subgroups=" + ("true" if recursive else "false")
    )
    data, total = _get_with_total(path)
    return (data if isinstance(data, list) else []), total


def _group_subgroups(group_id: int) -> list[dict[str, Any]]:
    data, _ = _get_with_total(f"/api/v4/groups/{group_id}/subgroups?per_page=100")
    return data if isinstance(data, list) else []


def _project_id(payload: dict[str, Any]) -> int:
    try:
        return int(payload["id"])
    except (KeyError, TypeError, ValueError) as error:
        raise GitlabError("GitLab 返回的项目信息缺少 id") from error


def _my_level(project_payload: dict[str, Any]) -> int:
    permissions = project_payload.get("permissions")
    if not isinstance(permissions, dict):
        return 0
    levels: list[int] = []
    for key in ("project_access", "group_access"):
        entry = permissions.get(key)
        if isinstance(entry, dict):
            try:
                levels.append(int(entry.get("access_level")))
            except (TypeError, ValueError):
                continue
    return max(levels) if levels else 0


def describe_project(project: Any, max_pipelines: int = MAX_PIPELINES) -> str:
    payload = resolve_project(project)
    project_id = _project_id(payload)
    path = str(payload.get("path_with_namespace") or project)

    lines = [
        f"项目：{path}（id {project_id}）",
        f"默认分支：{payload.get('default_branch') or '-'}",
        f"我的级别：{_level_name(_my_level(payload))}",
    ]

    branches = _request_json(f"/api/v4/projects/{project_id}/protected_branches")
    if isinstance(branches, list) and branches:
        lines.append(f"保护分支（{len(branches)}）：")
        for branch in branches:
            if not isinstance(branch, dict):
                continue
            merge = branch.get("merge_access_levels") or [{}]
            push = branch.get("push_access_levels") or [{}]
            merge_level = merge[0].get("access_level") if isinstance(merge[0], dict) else None
            push_level = push[0].get("access_level") if isinstance(push[0], dict) else None
            lines.append(
                f"  {str(branch.get('name')):<24}"
                f" 合并={_level_name(merge_level)}"
                f" 推送={_level_name(push_level)}"
            )
    elif isinstance(branches, list):
        lines.append("保护分支：无")
    else:
        lines.append("保护分支：读取失败")

    limit = max(1, min(int(max_pipelines), MAX_PIPELINES))
    pipelines = _request_json(
        f"/api/v4/projects/{project_id}/pipelines?per_page={limit}"
    )
    if isinstance(pipelines, list) and pipelines:
        lines.append(f"最近流水线（最多 {limit} 条）：")
        for pipeline in pipelines:
            if not isinstance(pipeline, dict):
                continue
            lines.append(
                f"  #{pipeline.get('iid')}  ref={pipeline.get('ref')}"
                f"  {_status_name(pipeline.get('status'))}"
                f"  {str(pipeline.get('sha') or '')[:8]}"
                f"  {_short_time(pipeline.get('created_at'))}"
            )
    elif isinstance(pipelines, list):
        lines.append("最近流水线：无")
    else:
        lines.append("最近流水线：读取失败")

    web_url = payload.get("web_url")
    if web_url:
        lines.append(f"地址：{web_url}")
    return "\n".join(lines)


def describe_group(group: Any, max_projects: int = MAX_GROUP_PROJECTS) -> str:
    payload = resolve_group(group)
    try:
        group_id = int(payload["id"])
    except (KeyError, TypeError, ValueError) as error:
        raise GitlabError("GitLab 返回的群组信息缺少 id") from error

    lines = [
        f"群组：{payload.get('full_path') or group}（id {group_id}）",
        f"可见性：{payload.get('visibility') or '-'}",
    ]
    description = str(payload.get("description") or "").strip()
    if description:
        lines.append(f"说明：{description[:200]}")

    subgroups = _group_subgroups(group_id)
    if subgroups:
        lines.append(f"子群组（{len(subgroups)}）：")
        for subgroup in subgroups[:MAX_GROUP_PROJECTS]:
            if isinstance(subgroup, dict):
                lines.append(f"  {subgroup.get('full_path')}")
        if len(subgroups) > MAX_GROUP_PROJECTS:
            lines.append(f"  …还有 {len(subgroups) - MAX_GROUP_PROJECTS} 个")
    else:
        lines.append("子群组：无")

    limit = max(1, min(int(max_projects), MAX_GROUP_PROJECTS))
    projects, total = _group_projects(group_id, recursive=False)
    if projects:
        shown = projects[:limit]
        total_text = f"共 {total}" if total is not None else f"本页 {len(projects)}"
        lines.append(f"直属项目（{total_text}，按最近活跃排序）：")
        for project in shown:
            if not isinstance(project, dict):
                continue
            lines.append(
                f"  {project.get('path_with_namespace')}"
                f"  默认分支={project.get('default_branch') or '-'}"
                f"  最近活跃={_short_time(project.get('last_activity_at'))}"
            )
    else:
        lines.append("直属项目：无")

    deep, deep_total = _group_projects(group_id, recursive=True)
    if deep_total is not None:
        lines.append(f"含子群组合计项目：{deep_total}")
    elif deep:
        lines.append(f"含子群组合计项目：至少 {len(deep)} 个")

    web_url = payload.get("web_url")
    if web_url:
        lines.append(f"地址：{web_url}")
    return "\n".join(lines)


def _pipeline_of(project_id: int, iid: Any, ref: Any) -> dict[str, Any]:
    if iid not in (None, ""):
        try:
            wanted = int(iid)
        except (TypeError, ValueError) as error:
            raise GitlabError(f"流水线号 {iid!r} 不是数字") from error
        # 用户看到的是 iid（界面上的 #编号），而 /pipelines/<n> 要的是全局 id，
        # 两者不是一回事。先在列表里按 iid 找，找不到再当全局 id 直接取。
        listing = _request_json(
            f"/api/v4/projects/{project_id}/pipelines?per_page=100"
        )
        if isinstance(listing, list):
            for pipeline in listing:
                if not isinstance(pipeline, dict):
                    continue
                if pipeline.get("iid") == wanted or pipeline.get("id") == wanted:
                    return pipeline
            raise GitlabError(
                f"该项目最近 100 条流水线里没有 #{wanted}；"
                "它可能太旧了，改用 ref 查最新一条"
            )
        payload = _request_json(f"/api/v4/projects/{project_id}/pipelines/{wanted}")
        if not isinstance(payload, dict):
            raise GitlabError("流水线信息格式不对")
        return payload

    query = "/api/v4/projects/" + str(project_id) + "/pipelines?per_page=1"
    target_ref = str(ref or "").strip()
    if target_ref:
        query += "&ref=" + urllib.parse.quote(target_ref, safe="")
    payload = _request_json(query)
    if not isinstance(payload, list) or not payload:
        where = f"分支 {target_ref}" if target_ref else "该项目"
        raise GitlabError(f"{where}没有任何流水线记录")
    first = payload[0]
    if not isinstance(first, dict):
        raise GitlabError("流水线列表格式不对")
    return first


def _trace_text(project_id: int, job_id: Any) -> str:
    """取作业的完整日志（已去 ANSI），读不到返回空串。

    镜像名要从整份日志里找，不能用截断后的尾部——`Pushing image to` 往往出现得
    比结尾早，尾部窗口会把它切掉。
    """
    try:
        raw = _request_text(f"/api/v4/projects/{project_id}/jobs/{int(job_id)}/trace")
    except GitlabError:
        return ""
    return _ANSI_RE.sub("", raw).replace("\r\n", "\n").replace("\r", "\n")


def _trace_tail(project_id: int, job_id: Any) -> str:
    try:
        raw = _request_text(f"/api/v4/projects/{project_id}/jobs/{int(job_id)}/trace")
    except GitlabError as error:
        return f"（日志读取失败：{error}）"
    text = _ANSI_RE.sub("", raw).replace("\r\n", "\n").replace("\r", "\n")
    rows = [row.rstrip() for row in text.splitlines() if row.strip()]
    kept = [row for row in rows if not _LOG_NOISE_RE.match(row)]
    # 全是噪音时退回原始行，宁可给噪声也不能给空。
    rows = (kept or rows)[-MAX_LOG_LINES:]
    joined = "\n".join(rows)
    if len(joined) > MAX_LOG_CHARS:
        joined = "…" + joined[-MAX_LOG_CHARS:]
    return joined or "（日志为空）"


def _image_refs(trace: str) -> list[str]:
    """从 kaniko 日志里取出推送目标。

    只认 kaniko 自己那行 `Pushing image to <ref>`：镜像名是构建产物的唯一权威来源，
    靠仓库地址和命名规则去拼容易猜错（不同项目推的 Harbor 并不相同）。
    """
    found: list[str] = []
    for line in trace.splitlines():
        match = _IMAGE_PUSH_RE.search(line)
        if not match:
            continue
        reference = match.group(1).strip().strip('"\'')
        if reference and reference not in found:
            found.append(reference)
    return found


def describe_pipeline(project: Any, iid: Any = None, ref: Any = None) -> str:
    payload = resolve_project(project)
    project_id = _project_id(payload)
    pipeline = _pipeline_of(project_id, iid, ref)

    lines = [
        f"流水线 #{pipeline.get('iid')}"
        f"  ref={pipeline.get('ref')}"
        f"  状态={_status_name(pipeline.get('status'))}",
        f"提交 {str(pipeline.get('sha') or '')[:8]}"
        f"  创建 {_short_time(pipeline.get('created_at'))}"
        + (f"  耗时 {pipeline.get('duration')}s" if pipeline.get("duration") else ""),
    ]
    if pipeline.get("source"):
        lines.append(f"触发来源：{pipeline.get('source')}")

    jobs = _request_json(
        f"/api/v4/projects/{project_id}/pipelines/{pipeline.get('id')}/jobs?per_page=100"
    )
    failed: list[dict[str, Any]] = []
    docker_jobs: list[dict[str, Any]] = []
    if isinstance(jobs, list):
        for job in jobs:
            if not isinstance(job, dict):
                continue
            if str(job.get("status")) == "failed":
                failed.append(job)
            if str(job.get("stage")) == "docker":
                docker_jobs.append(job)

    # 镜像要放在失败作业之前：问「这个 tag 出了什么镜像」时，最需要的是这个名字。
    # 日志只取一次，失败作业和镜像共用，避免重复请求。
    traces: dict[Any, str] = {}

    def trace_of(job_id: Any) -> str:
        if job_id not in traces:
            traces[job_id] = _trace_tail(project_id, job_id)
        return traces[job_id]

    images: list[tuple[str, str, str]] = []
    for job in docker_jobs[:MAX_IMAGE_JOBS]:
        for reference in _image_refs(_trace_text(project_id, job.get("id"))):
            images.append((str(job.get("name")), str(job.get("status")), reference))
    if images:
        lines.append(f"镜像（{len(images)}）：")
        for name, status, reference in images:
            mark = "" if status == "success" else f"  [{_status_name(status)}]"
            lines.append(f"  {reference}{mark}")
            lines.append(f"    来自作业 {name}")
    elif docker_jobs:
        lines.append("镜像：docker 作业里没有解析到推送目标（未走到推送，或用了其他构建方式）")

    if not failed:
        status = str(pipeline.get("status"))
        lines.append("失败作业：无" if status != "failed" else "失败作业：未找到（可能是流水线级失败）")
    else:
        lines.append(f"失败作业（{len(failed)}）：")
        for job in failed[:MAX_FAILED_JOBS]:
            lines.append(f"  {job.get('name')}  阶段={job.get('stage')}")
            for row in trace_of(job.get("id")).splitlines():
                lines.append("    " + row)

    web_url = pipeline.get("web_url")
    if web_url:
        lines.append(f"地址：{web_url}")
    return "\n".join(lines)


def handle(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        return {"ok": False, "error": "请求格式不对，需要 JSON 对象"}
    tool = str(request.get("tool") or "").strip()
    args = request.get("args")
    if not isinstance(args, dict):
        args = {}

    try:
        if tool == "gitlab_project":
            return {
                "ok": True,
                "text": describe_project(args.get("project"), args.get("max_pipelines") or MAX_PIPELINES),
            }
        if tool == "gitlab_pipeline":
            return {
                "ok": True,
                "text": describe_pipeline(args.get("project"), args.get("iid"), args.get("ref")),
            }
        if tool == "gitlab_group":
            return {
                "ok": True,
                "text": describe_group(
                    args.get("group"), args.get("max_projects") or MAX_GROUP_PROJECTS
                ),
            }
        return {"ok": False, "error": f"未知工具 {tool!r}"}
    except GitlabError as error:
        return {"ok": False, "error": str(error)}


def main() -> int:
    # Windows 管道下 stdout 默认是 cp936，中文原因会变乱码；显式写 UTF-8 字节。
    raw = sys.stdin.buffer.readline().decode("utf-8", "replace")
    try:
        request = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as error:
        payload = {"ok": False, "error": f"请求不是合法 JSON：{error}"}
    else:
        payload = handle(request)
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
