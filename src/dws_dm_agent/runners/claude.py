from __future__ import annotations

import json
from typing import Any, Mapping


def command(profile: Any, schema: Mapping[str, Any]) -> list[str]:
    result = [
        *profile.command,
        "--print", "--verbose", "--no-session-persistence",
        "--input-format", "stream-json", "--output-format", "stream-json",
        "--include-partial-messages", "--json-schema",
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
    ]
    if profile.model:
        result.extend(["--model", profile.model])
    if profile.read_only:
        result.extend(["--permission-mode", "plan", "--tools", "Read,Grep,Glob"])
    elif profile.options.get("permission_mode"):
        result.extend(["--permission-mode", str(profile.options["permission_mode"])])
    if profile.options.get("dangerously_skip_permissions"):
        result.append("--dangerously-skip-permissions")
    result.extend(profile.extra_args)
    return result
