from __future__ import annotations

import json
import sys
from typing import Any


TOOL = {
    "name": "escalate",
    "description": "Hand the current request to the execution agent.",
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


def _respond(request_id: Any, result: Any) -> None:
    print(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            _respond(
                request_id,
                {
                    "protocolVersion": request.get("params", {}).get(
                        "protocolVersion", "2025-06-18"
                    ),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "dws-escalate", "version": "0.1.0"},
                },
            )
        elif method == "ping":
            _respond(request_id, {})
        elif method == "tools/list":
            _respond(request_id, {"tools": [TOOL]})
        elif method == "tools/call":
            params = request.get("params", {})
            valid = (
                isinstance(params, dict)
                and params.get("name") == "escalate"
                and params.get("arguments", {}) == {}
            )
            _respond(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Escalated to the execution agent."
                                if valid
                                else "escalate accepts no arguments"
                            ),
                        }
                    ],
                    "isError": not valid,
                },
            )
        elif request_id is not None:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": "Method not found"},
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
