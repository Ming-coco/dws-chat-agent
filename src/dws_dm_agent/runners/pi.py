from __future__ import annotations

from typing import Any


def command(profile: Any) -> list[str]:
    result = [*profile.command, "--mode", "rpc", "--no-session"]
    if profile.model:
        result.extend(["--model", profile.model])
    if profile.reasoning_effort:
        result.extend(["--thinking", profile.reasoning_effort])
    if profile.read_only:
        result.extend(["--tools", "read,grep,find,ls"])
    elif profile.options.get("tools"):
        result.extend(["--tools", str(profile.options["tools"])])
    if profile.options.get("provider"):
        result.extend(["--provider", str(profile.options["provider"])])
    result.extend(profile.extra_args)
    return result
