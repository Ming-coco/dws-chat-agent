from __future__ import annotations

from pathlib import Path
from typing import Any


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
            result.extend(["--tools", ",".join(tool_names)])
        elif profile.read_only:
            result.append("--no-tools")
    if profile.options.get("provider"):
        result.extend(["--provider", str(profile.options["provider"])])
    result.extend(profile.extra_args)
    return result
