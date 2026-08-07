from __future__ import annotations

from pathlib import Path
from typing import Any


def command(profile: Any, mount_escalate: bool = False) -> list[str]:
    result = [*profile.command, "--mode", "rpc", "--no-session"]
    if profile.model:
        result.extend(["--model", profile.model])
    if profile.reasoning_effort:
        result.extend(["--thinking", profile.reasoning_effort])
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
