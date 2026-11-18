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
        if tool == "e2e_list_configs":
            _emit(_list_configs())
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


def _list_configs() -> dict[str, Any]:
    """列出可用的测试配置。

    真实配置被 gitignore，所以这里读的是本机实际存在的文件，
    不硬编码任何系统名。
    """
    configs = []
    if CONFIG_DIR.is_dir():
        for path in sorted(CONFIG_DIR.glob("*.json")):
            if path.name.startswith("example-"):
                continue
            name = _read_config_name(path)
            configs.append({"id": path.stem, "name": name or path.stem})
    return {
        "configs": configs,
        "hint": (
            "用 id 调 e2e_run。没有列出任何配置时，说明本机还没建真实配置；"
            "照 configs/example-webapp.json 的格式建一份，值用环境变量引用。"
        ),
    }


def _read_config_name(path: Path) -> str:
    """读配置里的显示名，顺便展开 ${VAR}。

    配置里 name 常写成 ${E2E_NAME}，直接读会漏出变量名，
    对使用者没有任何意义。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        raw = str(data.get("name") or "")
    except Exception:  # noqa: BLE001
        return ""

    if not raw:
        return ""

    def replace(match: "re.Match[str]") -> str:
        return os.environ.get(match.group(1), "") or ""

    import re

    expanded = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, raw).strip()
    return expanded


def _discover(args: dict[str, Any]) -> dict[str, Any]:
    """发现菜单。返回结构化的候选与结论，便于 agent 决定是否需要问人。"""
    config_id = str(args.get("config") or "").strip()
    if not config_id:
        return {"error": "config is required"}

    payload = _invoke(["discover", "--config", config_id, "--json"])
    # CLI 正常输出里也有 error 键（空串），只判断键存在会让转换永远走不到
    if payload.get("error"):
        return payload

    status = str(payload.get("status") or "")
    result: dict[str, Any] = {
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
    config_id = str(args.get("config") or "").strip()
    if not config_id:
        return {"error": "config is required"}

    mode = str(args.get("mode") or "smoke").strip()
    if mode != "smoke":
        return {
            "error": "only smoke mode is allowed here",
            "hint": (
                "smoke 只打开页面、不操作表单，可以自动跑。"
                "full 会填表单和点查询，可能产生业务数据，需要人工确认后手动执行。"
            ),
        }

    command = ["run", "--config", config_id, "--json"]
    only = str(args.get("only") or "").strip()
    if only:
        command.extend(["--only", only])

    payload = _invoke(command)
    # 注意：CLI 正常输出里也有 "error" 键（值通常是空串），
    # 不能只判断键存在，否则转换逻辑永远走不到。
    if payload.get("error"):
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


def _invoke(command: list[str]) -> dict[str, Any]:
    """调用 e2e_runner CLI，解析它输出的 JSON。"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SRC)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.update(_env_file_values())

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "e2e_runner", *command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            env=environment,
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


def _env_file_values() -> dict[str, str]:
    """从部署目录的 .env 读被测系统的配置。

    服务不会把 .env 灌进 os.environ，所以这里自己读一次。
    只取 E2E_ 开头的键，不碰别的。
    """
    path = os.environ.get("DWS_E2E_ENV_FILE", "").strip()
    if not path:
        return {}
    env_path = Path(path)
    if not env_path.is_file():
        return {}

    values: dict[str, str] = {}
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            if key.startswith("E2E_"):
                values[key] = value.strip()
    except OSError:
        return {}
    return values


def _missing_env_hint(stderr: str) -> str:
    """启动失败的常见原因是环境变量没填，给一句可操作的话。"""
    if "E2E_BASE_URL" in stderr or "E2E_USER" in stderr or "E2E_PASS" in stderr:
        return (
            "调用方需要 E2E_BASE_URL / E2E_USER / E2E_PASS。"
            "在部署目录的 .env 里补上即可，不用重启服务。"
        )
    if "playwright" in stderr.lower():
        return "缺少 playwright，需要执行 pip install playwright"
    return ""


if __name__ == "__main__":
    sys.exit(main())
