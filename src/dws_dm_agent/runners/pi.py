from __future__ import annotations

from pathlib import Path
from typing import Any


# 本服务自有工具：no_reply 控制结果 + 事项记忆读写，front/worker 都挂载。
_DWS_TOOL_NAMES = ("no_reply", "task_record", "task_lookup")


def command(profile: Any, mount_escalate: bool = False, *, model_override: str | None = None) -> list[str]:
    result = [*profile.command, "--mode", "rpc", "--no-session"]
    model = model_override or profile.model
    if model:
        result.extend(["--model", model])
    if profile.reasoning_effort:
        result.extend(["--thinking", profile.reasoning_effort])
    # 扩展白名单：options.extensions 非空时禁用自动发现，只加载显式列出的扩展；
    # 空/缺省时保持 pi 默认的扩展发现行为（加载全部已安装扩展）。
    extensions = profile.options.get("extensions")
    if extensions:
        result.append("--no-extensions")
        for extension in extensions:
            result.extend(["--extension", str(extension)])
    # 服务工具扩展始终加载（no_reply/task_record/task_lookup），不受白名单影响。
    result.extend(["--extension", str(Path(__file__).with_name("pi_dws_tools.ts"))])
    if mount_escalate:
        result.extend(["--extension", str(Path(__file__).with_name("pi_escalate.ts"))])
    configured_tools = profile.options.get(
        "tools", "read,grep,find,ls" if profile.read_only else ""
    )
    if configured_tools is not None:
        tool_names = [
            name.strip() for name in str(configured_tools).split(",") if name.strip()
        ]
        if mount_escalate and "escalate" not in tool_names:
            tool_names.append("escalate")
        if tool_names:
            # 只在已经显式限制工具集时追加服务工具；未限制时保持全量工具，
            # 否则 worker 会被误收窄成只有这三个工具。
            for name in _DWS_TOOL_NAMES:
                if name not in tool_names:
                    tool_names.append(name)
            result.extend(["--tools", ",".join(tool_names)])
        elif profile.read_only:
            result.append("--no-tools")
    if profile.options.get("provider"):
        result.extend(["--provider", str(profile.options["provider"])])
    result.extend(profile.extra_args)
    return result
