from __future__ import annotations

from pathlib import Path
from typing import Any


def command(profile: Any, mcp_config_path: Path | None = None) -> list[str]:
    result = [
        *profile.command,
        "--print", "--verbose", "--no-session-persistence",
        "--input-format", "stream-json", "--output-format", "stream-json",
        "--include-partial-messages",
    ]
    if mcp_config_path is not None:
        result.extend(["--mcp-config", str(mcp_config_path), "--strict-mcp-config"])
    if profile.model:
        result.extend(["--model", profile.model])
    if profile.read_only:
        tools = ["Read", "Grep", "Glob"]
        if mcp_config_path is not None:
            tools.append("mcp__dws-escalate__escalate")
        result.extend(["--permission-mode", "plan", "--tools", ",".join(tools)])
    elif profile.options.get("permission_mode"):
        result.extend(["--permission-mode", str(profile.options["permission_mode"])])
    if profile.options.get("dangerously_skip_permissions"):
        result.append("--dangerously-skip-permissions")
    result.extend(profile.extra_args)
    return result
