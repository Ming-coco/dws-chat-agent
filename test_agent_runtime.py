from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_runtime import AgentRuntime, AgentSession
from dashboard import (
    AgentConfigStore,
    ConfigBusyError,
    ConfigConflictError,
    DashboardServer,
)


class AgentRuntimeTests(unittest.TestCase):
    def _runtime(
        self,
        root: Path,
        driver: str,
        *,
        worker_read_only: bool = False,
        command: list[str] | None = None,
        protocol: str = "",
    ) -> AgentRuntime:
        (root / "prompts").mkdir(parents=True)
        (root / "prompts" / "front.md").write_text(
            "front {{self_name}} via {{agent_name}}", encoding="utf-8"
        )
        (root / "prompts" / "worker.md").write_text(
            "worker {{self_name}} via {{agent_name}}", encoding="utf-8"
        )
        (root / "prompts" / "supplement.md").write_text(
            "supplement {{supplement_messages_json}}", encoding="utf-8"
        )
        for name in ("front", "worker"):
            (root / f"{name}.schema.json").write_text(
                json.dumps(
                    {
                        "type": "object",
                        "properties": {"action": {"type": "string"}},
                        "required": ["action"],
                    }
                ),
                encoding="utf-8",
            )
        options: dict[str, object] = {}
        if driver == "codex":
            options = {
                "sandbox": "workspace-write",
                "network_access": True,
                "writable_roots": [str(root / "worktrees")],
                "web_search": "disabled",
            }
        elif driver == "claude":
            options = {"permission_mode": "acceptEdits"}
        agent_launch = (
            {"command": command, "protocol": protocol}
            if command is not None
            else {"binary": driver}
        )
        raw = {
            "agents": {
                "front-agent": {
                    "driver": driver,
                    **agent_launch,
                    "model": "fast-model",
                    "reasoning_effort": "medium",
                    "read_only": True,
                    "timeout_seconds": 30,
                    "options": options,
                },
                "worker-agent": {
                    "driver": driver,
                    **agent_launch,
                    "model": "strong-model",
                    "reasoning_effort": "high",
                    "read_only": worker_read_only,
                    "timeout_seconds": 300,
                    "options": options,
                    "environment": {"RUNTIME_TEST": "1"},
                },
            },
            "workflows": {
                "active": "dm-default",
                "presets": {
                    "dm-default": {
                        "supplement_strategy": "steer",
                        "supplement_prompt": "prompts/supplement.md",
                        "auto_messages": {
                            "ack": "收到，我在处理 {{request_count}} 条消息。",
                            "progress_enabled": True,
                            "progress_interval_seconds": 90,
                            "max_progress_updates": 8,
                        },
                        "front": {
                            "agent": "front-agent",
                            "prompt": "prompts/front.md",
                            "schema": "front.schema.json",
                        },
                        "worker": {
                            "agent": "worker-agent",
                            "prompt": "prompts/worker.md",
                            "schema": "worker.schema.json",
                        },
                    }
                },
            },
        }
        config_path = root / "config.json"
        config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return AgentRuntime.from_config(raw, config_path, root)

    def test_workflow_renders_prompts_and_exposes_safe_dashboard_description(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._runtime(Path(directory), "codex")

            rendered = runtime.render(
                "front", {"self_name": "Operator", "agent_name": "Fast"}
            )
            description = runtime.describe()

            self.assertEqual(rendered, "front Operator via Fast")
            self.assertEqual(description["name"], "dm-default")
            self.assertEqual(description["stages"][0]["agent"], "front-agent")
            self.assertEqual(description["stages"][1]["prompt"], "worker {{self_name}} via {{agent_name}}")
            self.assertEqual(runtime.supplement_strategy, "steer")
            self.assertEqual(
                runtime.render_supplement(
                    {"supplement_messages_json": '[{"text":"补充"}]'}
                ),
                'supplement [{"text":"补充"}]',
            )
            self.assertEqual(runtime.progress_interval_seconds, 90)
            self.assertEqual(runtime.max_progress_updates, 8)
            self.assertEqual(
                runtime.render_auto_message("ack", {"request_count": 2}),
                "收到，我在处理 2 条消息。",
            )
            self.assertEqual(description["autoMessages"]["ack"], "收到，我在处理 {{request_count}} 条消息。")
            self.assertNotIn("RUNTIME_TEST", json.dumps(description))

    def test_front_is_runtime_read_only_but_worker_permissions_do_not_change_by_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = self._runtime(root, "codex")

            front = runtime.prepare("front", "front-session", "prompt", root / "front-run")
            worker = runtime.prepare("worker", "worker-session", "prompt", root / "worker-run")

            self.assertEqual(front.protocol, "codex-app-server")
            self.assertEqual(worker.protocol, "codex-app-server")
            self.assertEqual(front.argv[1:4], ("app-server", "--listen", "stdio://"))
            self.assertEqual(front.thread_options["sandbox"], "read-only")
            self.assertEqual(worker.thread_options["sandbox"], "workspace-write")
            self.assertEqual(worker.environment["RUNTIME_TEST"], "1")
            self.assertEqual(front.timeout_seconds, 30)
            self.assertEqual(worker.timeout_seconds, 300)

    def test_claude_and_pi_adapters_apply_read_only_only_to_front(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = self._runtime(root / "claude", "claude")
            claude_front = claude.prepare("front", "front", "prompt", root / "cf")
            claude_worker = claude.prepare("worker", "worker", "prompt", root / "cw")
            self.assertEqual(claude_front.protocol, "claude-stream-json")
            self.assertIn("plan", claude_front.argv)
            self.assertIn("stream-json", claude_front.argv)
            self.assertIn("Read,Grep,Glob", claude_front.argv)
            self.assertIn("acceptEdits", claude_worker.argv)
            self.assertNotIn("Read,Grep,Glob", claude_worker.argv)

            pi = self._runtime(root / "pi", "pi")
            pi_front = pi.prepare("front", "front", "prompt", root / "pf")
            pi_worker = pi.prepare("worker", "worker", "prompt", root / "pw")
            self.assertEqual(pi_front.protocol, "pi-rpc")
            self.assertIn("rpc", pi_front.argv)
            self.assertIn("read,grep,find,ls", pi_front.argv)
            self.assertNotIn("read,grep,find,ls", pi_worker.argv)
            self.assertFalse(any(value.startswith("@") for value in pi_worker.argv))

    def test_launcher_command_is_independent_from_the_session_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ccr = self._runtime(
                root / "ccr",
                "claude",
                command=["ccr", "code"],
                protocol="claude-stream-json",
            )
            ccr_worker = ccr.prepare("worker", "ccr", "prompt", root / "ccr-run")
            self.assertEqual(ccr_worker.argv[:2], ("ccr", "code"))
            self.assertIn("stream-json", ccr_worker.argv)

            omp = self._runtime(
                root / "omp",
                "pi",
                command=["omp"],
                protocol="pi-rpc",
            )
            omp_worker = omp.prepare("worker", "omp", "prompt", root / "omp-run")
            self.assertEqual(omp_worker.argv[0], "omp")
            self.assertEqual(omp_worker.protocol, "pi-rpc")
            self.assertIn("rpc", omp_worker.argv)

            description = ccr.describe()
            self.assertEqual(
                description["availableAgents"][0]["launchCommand"], ["ccr", "code"]
            )

    def test_each_adapter_reads_structured_result_and_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            codex = self._runtime(root / "codex", "codex").prepare(
                "worker", "codex", "prompt", root / "codex-run"
            )
            codex.result_path.write_text('{"action":"reply"}', encoding="utf-8")
            codex.events_path.write_text(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "checking tests"},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(codex.read_decision()["action"], "reply")
            self.assertEqual(codex.latest_progress(), "checking tests")

            claude = self._runtime(root / "claude", "claude").prepare(
                "worker", "claude", "prompt", root / "claude-run"
            )
            claude.events_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "content": [{"type": "text", "text": "checking code"}]
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "result",
                                "structured_output": {"action": "reply"},
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            self.assertEqual(claude.read_decision()["action"], "reply")
            self.assertEqual(claude.latest_progress(), "checking code")

            pi = self._runtime(root / "pi", "pi").prepare(
                "worker", "pi", "prompt", root / "pi-run"
            )
            pi.events_path.write_text(
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": '{"action":"reply"}'}
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(pi.read_decision()["action"], "reply")

    def test_invalid_permission_layout_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            with self.assertRaisesRegex(ValueError, "worker.*read_only=false"):
                self._runtime(root, "codex", worker_read_only=True)


class AgentSessionProtocolTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, root: Path, driver: str) -> AgentRuntime:
        return AgentRuntimeTests()._runtime(root, driver)

    async def test_codex_app_server_steers_the_active_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fake_codex.py"
            script.write_text(
                """import json, sys
for line in sys.stdin:
    value = json.loads(line)
    method = value.get('method')
    if method == 'initialize':
        print(json.dumps({'id': value['id'], 'result': {'ok': True}}), flush=True)
    elif method == 'thread/start':
        print(json.dumps({'id': value['id'], 'result': {'thread': {'id': 'thread-1'}}}), flush=True)
    elif method == 'turn/start':
        print(json.dumps({'id': value['id'], 'result': {'turn': {'id': 'turn-1'}}}), flush=True)
        print(json.dumps({'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'phase': 'commentary', 'text': 'checking code'}}}), flush=True)
    elif method == 'turn/steer':
        print(json.dumps({'id': value['id'], 'result': {'turnId': 'turn-1'}}), flush=True)
        print(json.dumps({'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'phase': 'final_answer', 'text': '{\"action\":\"reply\"}'}}}), flush=True)
        print(json.dumps({'method': 'turn/completed', 'params': {'turn': {'id': 'turn-1', 'status': 'completed'}}}), flush=True)
""",
                encoding="utf-8",
            )
            prepared = self._runtime(root, "codex").prepare(
                "worker", "session", "initial", root / "run"
            )
            prepared = replace(prepared, argv=(sys.executable, str(script)))
            session = AgentSession(prepared, "session")
            try:
                await session.start()
                await asyncio.sleep(0.05)
                self.assertEqual(session.latest_progress, "checking code")
                self.assertTrue(await session.steer("new detail"))
                await session.wait(timeout=2)
                self.assertEqual(session.decision()["action"], "reply")
            finally:
                await session.close()


    async def test_pi_rpc_uses_native_streaming_steer_without_a_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fake_pi.py"
            script.write_text(
                """import json, sys
count = 0
for line in sys.stdin:
    value = json.loads(line)
    if value.get('type') == 'prompt':
        count += 1
        print(json.dumps({'id': value['id'], 'type': 'response', 'command': 'prompt', 'success': True}), flush=True)
        if count == 1:
            print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'checking code'}]}}), flush=True)
        else:
            print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '{\"action\":\"reply\"}'}]}}), flush=True)
            print(json.dumps({'type': 'agent_end'}), flush=True)
""",
                encoding="utf-8",
            )
            prepared = self._runtime(root, "pi").prepare(
                "worker", "session", "initial", root / "run"
            )
            prepared = replace(prepared, argv=(sys.executable, str(script)))
            session = AgentSession(prepared, "session")
            try:
                await session.start()
                await asyncio.sleep(0.05)
                self.assertTrue(await session.steer("new detail"))
                await session.wait(timeout=2)
                self.assertEqual(session.decision()["action"], "reply")
            finally:
                await session.close()

    async def test_claude_stream_json_accepts_a_running_followup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fake_claude.py"
            script.write_text(
                """import json, sys
count = 0
for line in sys.stdin:
    value = json.loads(line)
    if value.get('type') != 'user':
        continue
    count += 1
    if count == 1:
        print(json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'checking code'}]}}), flush=True)
    else:
        print(json.dumps({'type': 'result', 'structured_output': {'action': 'reply'}, 'is_error': False}), flush=True)
""",
                encoding="utf-8",
            )
            prepared = self._runtime(root, "claude").prepare(
                "worker", "session", "initial", root / "run"
            )
            prepared = replace(prepared, argv=(sys.executable, str(script)))
            session = AgentSession(prepared, "session")
            try:
                await session.start()
                await asyncio.sleep(0.05)
                self.assertTrue(await session.steer("new detail"))
                await session.wait(timeout=2)
                self.assertEqual(session.decision()["action"], "reply")
            finally:
                await session.close()

    async def test_custom_jsonl_cli_can_start_report_progress_steer_and_finish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fake_custom_agent.py"
            script.write_text(
                """import json, sys
for line in sys.stdin:
    value = json.loads(line)
    event_type = value.get('type')
    if event_type == 'start':
        print(json.dumps({'id': value['id'], 'type': 'response', 'success': True}), flush=True)
        print(json.dumps({'type': 'progress', 'text': 'inspecting repository'}), flush=True)
    elif event_type == 'steer':
        print(json.dumps({'id': value['id'], 'type': 'response', 'success': True}), flush=True)
        print(json.dumps({'type': 'result', 'result': {'action': 'reply', 'sessionId': value['sessionId']}}), flush=True)
        break
    elif event_type == 'abort':
        print(json.dumps({'id': value['id'], 'type': 'response', 'success': True}), flush=True)
""",
                encoding="utf-8",
            )
            runtime = AgentRuntimeTests()._runtime(
                root,
                "custom",
                command=[sys.executable, str(script)],
                protocol="custom-jsonl-v1",
            )
            prepared = runtime.prepare("worker", "custom-session", "initial", root / "run")
            self.assertEqual(prepared.argv, (sys.executable, str(script)))
            session = AgentSession(prepared, "custom-session")
            try:
                await session.start()
                await asyncio.sleep(0.05)
                self.assertEqual(session.latest_progress, "inspecting repository")
                self.assertTrue(await session.steer("new detail"))
                await session.wait(timeout=2)
                self.assertEqual(session.decision()["action"], "reply")
                self.assertEqual(session.decision()["sessionId"], "custom-session")
            finally:
                await session.close()

    async def test_custom_cli_startup_failure_is_reported_without_ack_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "failing_custom_agent.py"
            script.write_text("raise SystemExit(7)\n", encoding="utf-8")
            runtime = AgentRuntimeTests()._runtime(
                root,
                "custom",
                command=[sys.executable, str(script)],
                protocol="custom-jsonl-v1",
            )
            session = AgentSession(
                runtime.prepare("worker", "failed-session", "initial", root / "run"),
                "failed-session",
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "agent_exit_7"):
                    await asyncio.wait_for(session.start(), timeout=1)
            finally:
                await session.close()


class AgentConfigStoreTests(unittest.TestCase):
    def test_safe_agent_workflow_and_prompt_fields_are_saved_and_applied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            AgentRuntimeTests()._runtime(root, "codex")
            applied: list[dict[str, object]] = []
            store = AgentConfigStore(
                root / "config.json",
                root,
                on_apply=lambda raw: applied.append(copy.deepcopy(raw)),
                is_idle=lambda: True,
            )

            view = store.snapshot()
            self.assertNotIn(
                "environment", json.dumps(view["agents"], ensure_ascii=False)
            )
            view["agents"]["worker-agent"]["command"] = ["ccr", "code"]
            view["agents"]["worker-agent"]["protocol"] = "claude-stream-json"
            view["agents"]["worker-agent"]["driver"] = "claude"
            view["workflows"]["presets"]["dm-default"]["auto_messages"][
                "ack"
            ] = "收到，正在定位具体代码。"
            view["prompts"]["prompts/worker.md"] = "worker {{self_name}} updated"

            saved = store.save(view)

            raw = json.loads((root / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["agents"]["worker-agent"]["command"], ["ccr", "code"])
            self.assertEqual(raw["agents"]["worker-agent"]["environment"], {"RUNTIME_TEST": "1"})
            self.assertEqual(
                raw["workflows"]["presets"]["dm-default"]["auto_messages"]["ack"],
                "收到，正在定位具体代码。",
            )
            self.assertEqual(
                (root / "prompts" / "worker.md").read_text(encoding="utf-8"),
                "worker {{self_name}} updated",
            )
            self.assertEqual(len(applied), 1)
            self.assertNotEqual(saved["revision"], view["revision"])

    def test_save_rejects_busy_runtime_stale_revision_and_invalid_front(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            AgentRuntimeTests()._runtime(root, "codex")
            busy = AgentConfigStore(root / "config.json", root, is_idle=lambda: False)
            with self.assertRaises(ConfigBusyError):
                busy.save(busy.snapshot())

            store = AgentConfigStore(root / "config.json", root, is_idle=lambda: True)
            stale = store.snapshot()
            changed = store.snapshot()
            changed["agents"]["front-agent"]["model"] = "new-fast-model"
            store.save(changed)
            with self.assertRaises(ConfigConflictError):
                store.save(stale)

            invalid = store.snapshot()
            invalid["agents"]["front-agent"]["read_only"] = False
            before = (root / "config.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "front agent"):
                store.save(invalid)
            self.assertEqual((root / "config.json").read_bytes(), before)

    def test_dashboard_config_http_endpoint_reads_and_saves_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            AgentRuntimeTests()._runtime(root, "codex")
            html = root / "dashboard.html"
            html.write_text("<!doctype html><title>test</title>", encoding="utf-8")
            settings_html = root / "settings.html"
            settings_html.write_text(
                "<!doctype html><title>settings test</title>", encoding="utf-8"
            )
            (root / "theme.js").write_text("window.themeTest = true;", encoding="utf-8")
            (root / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
            store = AgentConfigStore(root / "config.json", root)
            server = DashboardServer(
                root / "audit.sqlite3",
                root / "runtime.json",
                html,
                {},
                timezone.utc,
                port=0,
                config_store=store,
            )
            server.start()
            try:
                assert server.server is not None
                base = f"http://127.0.0.1:{server.server.server_port}"
                with urlopen(f"{base}/settings", timeout=2) as response:
                    settings_page = response.read().decode("utf-8")
                self.assertIn("settings test", settings_page)
                with urlopen(f"{base}/theme.js", timeout=2) as response:
                    self.assertEqual(response.headers.get_content_type(), "text/javascript")
                    self.assertIn("themeTest", response.read().decode("utf-8"))
                with urlopen(f"{base}/favicon.ico", timeout=2) as response:
                    self.assertEqual(response.headers.get_content_type(), "image/svg+xml")
                with urlopen(f"{base}/api/config", timeout=2) as response:
                    payload = json.load(response)
                payload["agents"]["front-agent"]["model"] = "updated-fast-model"
                request = Request(
                    f"{base}/api/config",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=2) as response:
                    saved = json.load(response)
                self.assertEqual(
                    saved["agents"]["front-agent"]["model"],
                    "updated-fast-model",
                )

                blocked = Request(
                    f"{base}/api/config",
                    data=json.dumps(saved).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://outside.example",
                    },
                    method="POST",
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(blocked, timeout=2)
                self.assertEqual(raised.exception.code, 403)
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
