"""E2E 工具的 agent 桥接：从 stdin 读请求，向 stdout 写一行 JSON。

协议与 gitlab_tools / jira_tools 一致：一次调用一个请求，输出一行结果。

为什么需要这层：e2e_runner 是命令行工具，参数多、输出是给人看的；
agent 需要的是结构化结果和明确的失败原因。这层负责：

  - 把工具参数翻译成 e2e_runner 调用
  - 把结果压缩成 agent 能直接转述的形状
  - 把「需要人工确认」和「发现失败」区分开

安全约束：网页测试有副作用，smoke（只打开页面）可自动跑，
full（会填表、点查询）必须由人明确要求。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# 本文件在 src/dws_dm_agent/runners/ 下，仓库根要往上 4 层
ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
CONFIG_DIR = ROOT / "configs"

# 单次运行的最长等待。页面测试本身要一两分钟，给的宽一些。
TIMEOUT_SECONDS = 900


def main() -> int:
    try:
        request = json.loads(sys.stdin.readline() or "{}")
    except json.JSONDecodeError as error:
        _emit({"error": f"invalid request json: {error}"})
        return 0

    tool = str(request.get("tool") or "")
    args = request.get("args") or {}

    try:
        if tool in ("e2e_projects", "e2e_list_configs"):
            _emit(_list_projects())
        elif tool == "e2e_discover":
            _emit(_discover(args))
        elif tool == "e2e_run":
            _emit(_run(args))
        else:
            _emit({"error": f"unknown tool: {tool}"})
    except Exception as error:  # noqa: BLE001 桥接层不能崩，把原因交回给模型
        _emit({"error": f"{type(error).__name__}: {error}"})
    return 0


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _project_store():
    """项目配置存储。state 目录由服务通过环境变量告知。"""
    state_dir = os.environ.get("DWS_STATE_DIR", "").strip()
    if not state_dir:
        return None
    sys.path.insert(0, str(SRC))
    from dws_dm_agent.project_config import ProjectStore

    return ProjectStore(Path(state_dir) / "projects")


def _list_projects() -> dict[str, Any]:
    """列出配好了前端地址的项目。

    只列有前端地址的：没有地址就没法测，列出来只会让人白点一次。
    """
    store = _project_store()
    if store is None:
        return {"error": "服务没有提供 state 目录，无法读取项目配置"}

    projects: list[dict[str, Any]] = []
    for name in store.list_configured():
        try:
            config = store.load(name)
        except Exception:  # noqa: BLE001
            continue
        if not config.frontend:
            continue
        projects.append(
            {
                "project": name,
                "frontend": config.frontend,
                "loginUser": config.login_user,
                "note": config.note,
            }
        )

    return {
        "projects": projects,
        "hint": (
            "用 project 字段调 e2e_run。列表为空说明还没有项目配好前端地址，"
            "需要到「设置 → 项目配置」里填写。"
        ),
    }


def _discover(args: dict[str, Any]) -> dict[str, Any]:
    """发现菜单。返回结构化的候选与结论，便于 agent 决定是否需要问人。"""
    project = str(args.get("project") or "").strip()
    if not project:
        return _missing_project()

    resolved = _resolve_project(project)
    if isinstance(resolved, dict):
        return resolved

    config, environment = resolved
    payload = _invoke(
        ["discover", "--config", str(config), "--json"], environment=environment
    )
    # CLI 正常输出里也有 error 键（空串），只判断键存在会让转换永远走不到
    if payload.get("error"):
        return payload

    status = str(payload.get("status") or "")
    result: dict[str, Any] = {
        "project": project,
        "status": status,
        "message": payload.get("message", ""),
        "menuCount": payload.get("menuCount", 0),
    }

    if status == "ok":
        result["menus"] = _flatten_menu_names(payload.get("menus") or [])
    else:
        # 需要人确认时把候选摆出来，让 agent 能照实转述
        candidates = payload.get("candidates") or []
        result["candidates"] = [
            {
                "label": item.get("label", ""),
                "topCount": item.get("topCount", 0),
                "urlCount": item.get("urlCount", 0),
                "sample": item.get("sample", []),
            }
            for item in candidates[:5]
        ]
        result["suggested"] = payload.get("suggested", -1)
        result["needsHuman"] = status == "needs_confirmation"

    return result


def _run(args: dict[str, Any]) -> dict[str, Any]:
    """跑页面体检。

    mode 只接受 smoke。full 会填表单、点查询，可能触发导出或写数据，
    必须由人明确要求后从命令行跑，不走 agent 自动触发。
    """
    project = str(args.get("project") or "").strip()
    if not project:
        return _missing_project()

    mode = str(args.get("mode") or "smoke").strip()
    if mode != "smoke":
        return {
            "error": "only smoke mode is allowed here",
            "hint": (
                "smoke 只打开页面、不操作表单，可以自动跑。"
                "full 会填表单和点查询，可能产生业务数据，需要人工确认后手动执行。"
            ),
        }

    resolved = _resolve_project(project)
    if isinstance(resolved, dict):
        return resolved

    config, environment = resolved
    command = ["run", "--config", str(config), "--json"]
    only = str(args.get("only") or "").strip()
    if only:
        command.extend(["--only", only])

    payload = _invoke(command, environment=environment)
    # 注意：CLI 正常输出里也有 "error" 键（值通常是空串），
    # 不能只判断键存在，否则转换逻辑永远走不到。
    if payload.get("error"):
        payload.setdefault("project", project)
        return payload

    summary = payload.get("summary") or {}
    # 只把有问题的页面回给模型：通过的页面它不需要逐条转述
    problems = [
        {
            "page": page.get("name", ""),
            "level": page.get("level", ""),
            "layer": page.get("layer", ""),
            "reason": page.get("reason", ""),
            "details": page.get("details", [])[:2],
        }
        for page in payload.get("pages") or []
        if page.get("level") != "PASS"
    ]
    passed = [
        page.get("name", "")
        for page in payload.get("pages") or []
        if page.get("level") == "PASS"
    ]

    return {
        "project": project,
        "overall": payload.get("overall", ""),
        "menuSource": payload.get("menuSource", ""),
        "durationMs": payload.get("durationMs", 0),
        "error": payload.get("error", ""),
        "summary": summary,
        "problems": problems,
        "passedPages": passed,
        "hint": (
            "overall=FAIL 说明有页面明确坏了，按 layer 说明是前端还是后端；"
            "WARN 多为空数据，需要业务判断真假；"
            "回复时只讲结论和问题页，不要逐页罗列通过的页面。"
        ),
    }


def _missing_project() -> dict[str, Any]:
    """没给项目时，把配好的项目列出来让 agent 问一句。"""
    listing = _list_projects()
    projects = [item["project"] for item in listing.get("projects") or []]
    if not projects:
        return {
            "error": "还没有项目配置",
            "hint": (
                "需要先在「设置 → 项目配置」里填前端地址和登录账号，"
                "然后再来跑测试。"
            ),
        }
    if len(projects) == 1:
        return {
            "error": "missing project",
            "hint": f"直接传 project=\"{projects[0]}\" 重试即可。",
            "available": projects,
        }
    return {
        "error": "missing project",
        "hint": "有多个项目可测，需要问清楚测哪一个，然后带 project 参数重试。",
        "available": projects,
    }


def _resolve_project(project: str) -> tuple[Path, dict[str, str]] | dict[str, Any]:
    """把项目名解析成一份临时 e2e 配置 + 运行环境变量。

    返回 (配置文件路径, 环境变量) 表示成功；返回 dict 表示失败，
    内容直接回给模型。
    """
    store = _project_store()
    if store is None:
        return {"error": "服务没有提供 state 目录，无法读取项目配置"}

    if not store.exists(project):
        listing = _list_projects()
        return {
            "error": f"项目 {project} 还没有配置",
            "hint": "到「设置 → 项目配置」里选这个项目填好前端地址和登录账号。",
            "configured": [
                item["project"] for item in listing.get("projects") or []
            ],
        }

    config = store.load(project)
    if not config.frontend:
        return {
            "error": f"项目 {project} 没填前端地址",
            "hint": "前端地址是必填项，去「设置 → 项目配置」补上。",
        }

    if not config.login_user or not config.login_password:
        return {
            "error": f"项目 {project} 缺少登录账号或密码",
            "hint": "页面测试需要登录，去「设置 → 项目配置」补上登录账号和密码。",
        }

    # 生成一份临时配置给 e2e_runner 用。账号密码走环境变量，
    # 不落盘——配置文件里只写 ${VAR} 引用。
    runtime_dir = ROOT / "build" / "e2e-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    config_path = runtime_dir / f"project-{_safe_name(project)}.json"

    payload = {
        "name": project,
        "baseUrl": "${E2E_BASE_URL}",
        "browser": {"headless": True, "viewport": {"width": 1600, "height": 1000}},
        "login": {
            "username": "${E2E_USER}",
            "password": "${E2E_PASS}",
            "successUrlNotContains": "/login",
        },
        "discover": {"mode": "auto", "maxDepth": 2},
        "detect": {
            "consoleErrors": True,
            "pageErrors": True,
            "httpStatusMin": 400,
            "apiKeyword": "/api/",
            "minVisibleElements": 5,
            "minBodyText": 20,
            "pageTimeoutMs": 30000,
            "settleMs": 2500,
        },
        "ignore": {
            "consolePatterns": ["favicon", "ResizeObserver loop", "hm.baidu.com"],
            "apiPatterns": [],
        },
        "logs": {"enabled": False},
    }
    config_path.write_text(
        __import__("json").dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    environment = {
        "E2E_BASE_URL": config.frontend,
        "E2E_USER": config.login_user,
        "E2E_PASS": config.login_password,
    }
    return config_path, environment


def _safe_name(name: str) -> str:
    """项目名转成可做文件名的形式。"""
    keep = "".join(
        char if (char.isalnum() or char in "-_") else "_" for char in name
    )
    return keep.strip("_") or "project"


def _flatten_menu_names(nodes: list[dict[str, Any]], prefix: str = "") -> list[str]:
    """列出「可测试的页面」，与 navigate.collect_targets 保持同一口径。

    只收叶子页面：容器菜单（营销、风控）本身不是页面，
    把它们列出来会让人以为有 31 个页面，实际测的只有叶子那批。
    """
    names: list[str] = []

    def walk(items: list[dict[str, Any]], prefix: str) -> None:
        for node in items:
            name = str(node.get("name") or "")
            label = f"{prefix} / {name}" if prefix else name
            children = node.get("children") or []
            has_nav_child = any(
                child.get("kind") == "menu" and child.get("fullPath")
                for child in children
            )
            is_page = (
                node.get("kind") == "menu"
                and bool(node.get("fullPath"))
                and not has_nav_child
            )
            if is_page:
                names.append(label)
            if children:
                walk(children, label)

    walk(nodes, prefix)
    return names


def _invoke(
    command: list[str], environment: dict[str, str] | None = None
) -> dict[str, Any]:
    """调用 e2e_runner CLI，解析它输出的 JSON。

    environment 里带着被测系统的地址和账号，由调用方从项目配置里取。
    不再读全局 .env：一台机器上会有多套系统，全局变量只能容一套。
    """
    merged = dict(os.environ)
    merged["PYTHONPATH"] = str(SRC)
    merged["PYTHONDONTWRITEBYTECODE"] = "1"
    merged["PYTHONIOENCODING"] = "utf-8"
    if environment:
        merged.update(environment)

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "e2e_runner", *command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            env=merged,
            cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        return {"error": f"页面测试超时（超过 {TIMEOUT_SECONDS} 秒）"}

    stdout = (completed.stdout or "").strip()
    if not stdout:
        stderr = (completed.stderr or "").strip()
        missing = _missing_env_hint(stderr)
        return {
            "error": "测试没有输出任何结果",
            "stderr": stderr[-600:],
            "exitCode": completed.returncode,
            **({"hint": missing} if missing else {}),
        }

    # CLI 的 --json 把结果打在最后一行
    last_line = stdout.splitlines()[-1]
    try:
        return json.loads(last_line)
    except json.JSONDecodeError:
        return {
            "error": "测试输出无法解析",
            "stdout": stdout[-600:],
            "stderr": (completed.stderr or "")[-400:],
            "exitCode": completed.returncode,
        }


def _missing_env_hint(stderr: str) -> str:
    """启动失败的常见原因给一句可操作的话。"""
    if "playwright" in stderr.lower():
        return "缺少 playwright，需要执行 pip install playwright"
    return ""


if __name__ == "__main__":
    sys.exit(main())
