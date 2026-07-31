from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


class EscalateMcpTests(unittest.TestCase):
    def test_stdio_tool_has_no_arguments_and_can_be_called(self) -> None:
        root = Path(__file__).resolve().parents[1]
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "escalate", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "escalate", "arguments": {"reason": "no"}},
            },
        ]
        completed = subprocess.run(
            [sys.executable, "-m", "dws_dm_agent.runners.escalate_mcp"],
            input="\n".join(json.dumps(item) for item in requests) + "\n",
            text=True,
            capture_output=True,
            check=True,
            env={**os.environ, "PYTHONPATH": str(root / "src")},
        )
        responses = [json.loads(line) for line in completed.stdout.splitlines()]

        tool = responses[1]["result"]["tools"][0]
        self.assertEqual(tool["inputSchema"]["properties"], {})
        self.assertFalse(responses[2]["result"]["isError"])
        self.assertTrue(responses[3]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
