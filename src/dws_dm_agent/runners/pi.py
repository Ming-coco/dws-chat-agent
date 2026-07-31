from __future__ import annotations

from pathlib import Path
from typing import Any


def command(profile: Any) -> list[str]:
    result = [*profile.command, "--mode", "rpc", "--no-session"]
    if profile.model:
        result.extend(["--model", profile.model])
    if profile.reasoning_effort:
        result.extend(["--thinking", profile.reasoning_effort])
    if profile.read_only and profile.options.get("tools", "read,grep,find,ls"):
        result.extend(
            ["--tools", str(profile.options.get("tools", "read,grep,find,ls"))]
        )
    elif profile.read_only:
        result.append("--no-tools")
    elif profile.options.get("tools"):
        result.extend(["--tools", str(profile.options["tools"])])
    if profile.options.get("provider"):
        result.extend(["--provider", str(profile.options["provider"])])
    if profile.options.get("escalate_tool"):
        result.extend(
            ["--extension", str(Path(__file__).with_name("pi_escalate.ts"))]
        )
    result.extend(profile.extra_args)
    return result
