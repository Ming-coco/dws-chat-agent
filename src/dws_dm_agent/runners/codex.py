from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def command(profile: Any, mount_escalate: bool = False) -> list[str]:
    result = [*profile.command, "app-server", "--listen", "stdio://"]
    if mount_escalate:
        script = Path(__file__).with_name("escalate_mcp.py").resolve()
        result.extend(
            [
                "-c",
                f"mcp_servers.dws-escalate.command={json.dumps(sys.executable)}",
                "-c",
                "mcp_servers.dws-escalate.args="
                + json.dumps([str(script)]),
            ]
        )
    result.extend(profile.extra_args)
    return result


def thread_options(profile: Any, workspace_root: Path) -> dict[str, Any]:
    options = profile.options
    sandbox = "read-only" if profile.read_only else str(
        options.get("sandbox", "danger-full-access")
    )
    config: dict[str, Any] = {}
    if options.get("web_search"):
        config["web_search"] = options["web_search"]
    if sandbox == "workspace-write":
        config["sandbox_workspace_write"] = {
            "network_access": bool(options.get("network_access", False)),
            "writable_roots": [
                str(Path(value).expanduser().resolve())
                for value in options.get("writable_roots", [])
            ],
        }
    result: dict[str, Any] = {
        "cwd": str(workspace_root),
        "ephemeral": True,
        "approvalPolicy": "never",
        "sandbox": sandbox,
    }
    if profile.model:
        result["model"] = profile.model
    if config:
        result["config"] = config
    return result
