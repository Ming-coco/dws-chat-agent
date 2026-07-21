from __future__ import annotations

from pathlib import Path
from typing import Any


def command(profile: Any) -> list[str]:
    return [*profile.command, "app-server", "--listen", "stdio://", *profile.extra_args]


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
