from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dws_dm_agent.front_eval import load_cases, matches_expected
from dws_dm_agent.runners.runtime import AgentRuntime, AgentSession
from dws_dm_agent.dashboard import (
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
            "contacts": [
                {
                    "alias": "alice",
                    "display_name": "Alice",
                    "user_id": "user-alice",
                    "open_dingtalk_id": "open-alice",
                }
            ],
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
                            "ack_enabled": True,
                            "ack": "收到，我在处理 {{request_count}} 条消息。",
                            "progress_enabled": True,
                            "max_progress_updates": 8,
                        },
                        "front": {
                            "agent": "front-agent",
                            "prompt": "prompts/front.md",
                        },
                        "worker": {
                            "agent": "worker-agent",
                            "prompt": "prompts/worker.md",
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
            root = Path(directory)
            runtime = self._runtime(root, "codex")

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
            self.assertEqual(runtime.max_progress_updates, 8)
            self.assertEqual(
                runtime.render_auto_message("ack", {"request_count": 2}),
                "收到，我在处理 2 条消息。",
            )
            raw = json.loads((root / "config.json").read_text(encoding="utf-8"))
            raw["workflows"]["presets"]["dm-default"]["auto_messages"]["ack_enabled"] = False
            runtime = AgentRuntime.from_config(raw, root / "config.json", root)
            self.assertEqual(runtime.render_auto_message("ack", {"request_count": 2}), "")
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
            self.assertEqual(
                front.environment["CODEX_HOME"],
                str((root / "front-run" / "codex-home").resolve()),
            )
            self.assertFalse((root / "front-run" / "codex-home" / "config.toml").exists())
            self.assertEqual(front.timeout_seconds, 30)
            self.assertEqual(worker.timeout_seconds, 300)

    def test_zero_timeout_is_unlimited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._runtime(root, "pi")
            raw = json.loads((root / "config.json").read_text(encoding="utf-8"))
            raw["agents"]["front-agent"]["timeout_seconds"] = 0

            runtime = AgentRuntime.from_config(raw, root / "config.json", root)

            self.assertEqual(
                runtime.prepare("front", "front", "prompt", root / "run").timeout_seconds,
                0,
            )

    def test_claude_and_pi_adapters_apply_read_only_only_to_front(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = self._runtime(root / "claude", "claude")
            claude_front = claude.prepare("front", "front", "prompt", root / "cf")
            claude_worker = claude.prepare("worker", "worker", "prompt", root / "cw")
            self.assertEqual(claude_front.protocol, "claude-stream-json")
            self.assertIn("plan", claude_front.argv)
            self.assertIn("stream-json", claude_front.argv)
            self.assertIn("Read,Grep,Glob,mcp__dws-escalate__escalate", claude_front.argv)
            self.assertIn("acceptEdits", claude_worker.argv)
            self.assertNotIn("Read,Grep,Glob", claude_worker.argv)

            pi = self._runtime(root / "pi", "pi")
            pi_front = pi.prepare("front", "front", "prompt", root / "pf")
            pi_worker = pi.prepare("worker", "worker", "prompt", root / "pw")
            self.assertEqual(pi_front.protocol, "pi-rpc")
            self.assertIn("rpc", pi_front.argv)
            self.assertIn("read,grep,find,ls,escalate", pi_front.argv)
            self.assertNotIn("read,grep,find,ls", pi_worker.argv)
            self.assertFalse(any(value.startswith("@") for value in pi_worker.argv))

            raw = json.loads((root / "pi" / "config.json").read_text(encoding="utf-8"))
            raw["agents"]["front-agent"]["options"]["tools"] = ""
            pi = AgentRuntime.from_config(raw, root / "pi" / "config.json", root / "pi")
            pi_front = pi.prepare("front", "front-fast", "prompt", root / "pf-fast")
            self.assertIn("escalate", pi_front.argv)
            self.assertNotIn("read,grep,find,ls", pi_front.argv)

            raw["agents"]["front-agent"]["options"]["tools"] = None
            pi = AgentRuntime.from_config(raw, root / "pi" / "config.json", root / "pi")
            pi_front = pi.prepare("front", "front-all", "prompt", root / "pf-all")
            self.assertNotIn("--tools", pi_front.argv)
            self.assertNotIn("--no-tools", pi_front.argv)
            self.assertIn("--extension", pi_front.argv)

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

    def test_front_mounts_escalate_adapter_and_worker_stays_plain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for driver in ("codex", "claude", "pi"):
                runtime = self._runtime(root / driver, driver)
                front = runtime.prepare(
                    "front", f"{driver}-front", "prompt", root / f"{driver}-front"
                )
                worker = runtime.prepare(
                    "worker", f"{driver}-worker", "prompt", root / f"{driver}-worker"
                )
                self.assertTrue((front.result_path.parent / "mcp.json").is_file())
                self.assertFalse((worker.result_path.parent / "mcp.json").exists())
                self.assertEqual(worker.result_path.name, "result.txt")
                self.assertNotIn("--json-schema", worker.argv)

            self.assertIn("mcp_servers.dws-escalate.command", " ".join(
                self._runtime(root / "codex-2", "codex").prepare(
                    "front", "front", "prompt", root / "codex-front-2"
                ).argv
            ))
            self.assertIn("--mcp-config", self._runtime(
                root / "claude-2", "claude"
            ).prepare("front", "front", "prompt", root / "claude-front-2").argv)
            self.assertIn("pi_escalate.ts", " ".join(
                self._runtime(root / "pi-2", "pi").prepare(
                    "front", "front", "prompt", root / "pi-front-2"
                ).argv
            ))

    def test_front_stage_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = self._runtime(root, "codex")
            raw = json.loads((root / "config.json").read_text(encoding="utf-8"))
            raw["workflows"]["presets"]["dm-default"].pop("front")
            runtime = AgentRuntime.from_config(raw, root / "config.json", root)

            self.assertFalse(runtime.has_stage("front"))
            self.assertTrue(runtime.has_stage("worker"))
            self.assertEqual([item["id"] for item in runtime.describe()["stages"]], ["worker"])

    def test_invalid_permission_layout_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            with self.assertRaisesRegex(ValueError, "worker.*read_only=false"):
                self._runtime(root, "codex", worker_read_only=True)

    def test_example_pi_front_loads_all_tools_and_extensions(self) -> None:
        root = Path(__file__).resolve().parents[1]
        raw = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
        front = raw["agents"]["pi-front"]
        prompt = (root / "prompts" / "front.md").read_text(encoding="utf-8")

        self.assertIsNone(front["options"]["tools"])
        self.assertEqual(front["extra_args"], [])
        self.assertTrue(front["read_only"])
        self.assertEqual(front["timeout_seconds"], 0)
        self.assertIn("Bash/CLI、skills、extensions、MCP", prompt)
        self.assertIn("`glab api` 时必须显式指定 `--method GET`", prompt)
        self.assertIn("工具可见不代表获得写权限", prompt)
        self.assertNotIn("预计耗时", prompt)


class FrontRoutingEvalTests(unittest.TestCase):
    def test_historical_cases_have_valid_expectations(self) -> None:
        cases = load_cases(
            Path(__file__).parents[1] / "evals" / "front-routing.json"
        )

        self.assertEqual(len(cases), 16)
        self.assertEqual(len({case["name"] for case in cases}), len(cases))
        self.assertTrue(matches_expected("", True, cases[4]))
        self.assertFalse(matches_expected("不用查。", False, cases[4]))


class AgentSessionProtocolTests(unittest.IsolatedAsyncioTestCase):
    def _session(self, root: Path, driver: str, stage: str = "worker") -> AgentSession:
        runtime = AgentRuntimeTests()._runtime(root, driver)
        return AgentSession(
            runtime.prepare(stage, "session", "prompt", root / "run"),
            "session",
        )

    async def test_codex_exposes_reasoning_summary_and_keeps_final_body_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory), "codex")
            await session._handle_event(
                {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {"itemId": "reason-1", "delta": "正在核对真实链路"},
                }
            )
            await session._handle_event(
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "这是最终正文。",
                        }
                    },
                }
            )
            session._finish()

            self.assertEqual(session.latest_progress, "正在核对真实链路")
            self.assertEqual(session.result(), "这是最终正文。")

    async def test_codex_retryable_stream_error_does_not_poison_completed_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory), "codex")
            session.turn_id = "turn-1"

            await session._handle_event(
                {
                    "method": "error",
                    "params": {
                        "error": {
                            "message": "Reconnecting... 2/5",
                            "additionalDetails": (
                                "stream disconnected before completion: "
                                "tls handshake eof"
                            ),
                        },
                        "willRetry": True,
                        "turnId": "turn-1",
                    },
                }
            )
            await session._handle_event(
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "恢复后的最终正文",
                        }
                    },
                }
            )
            await session._handle_event(
                {
                    "method": "turn/completed",
                    "params": {
                        "turn": {
                            "id": "turn-1",
                            "status": "completed",
                        }
                    },
                }
            )

            self.assertEqual(session.error, "")
            self.assertEqual(session.result(), "恢复后的最终正文")

    async def test_pi_exposes_native_thinking_and_keeps_text_as_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory), "pi")
            await session._handle_event(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "thinking_delta",
                        "contentIndex": 0,
                        "delta": "先确认上下文",
                    },
                }
            )
            await session._handle_event(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "contentIndex": 1,
                        "delta": "最终正文",
                    },
                }
            )
            await session._handle_event({"type": "agent_end"})

            self.assertEqual(session.latest_progress, "先确认上下文")
            self.assertEqual(session.result(), "最终正文")

    async def test_escalate_is_detected_for_all_provider_event_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = self._session(root / "codex", "codex", "front")
            pi = self._session(root / "pi", "pi", "front")
            claude = self._session(root / "claude", "claude", "front")

            await codex._handle_event(
                {
                    "method": "item/started",
                    "params": {
                        "item": {
                            "type": "mcpToolCall",
                            "tool": "escalate",
                        }
                    },
                }
            )
            await pi._handle_event(
                {"type": "tool_execution_start", "toolName": "escalate", "args": {}}
            )
            await claude._handle_event(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "mcp__dws-escalate__escalate",
                                "input": {},
                            }
                        ],
                    },
                }
            )

            self.assertTrue(codex.escalated)
            self.assertTrue(pi.escalated)
            self.assertTrue(claude.escalated)
            self.assertTrue(codex.done)
            self.assertTrue(pi.done)
            self.assertTrue(claude.done)


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
            self.assertIn("dws", view)
            view["dws"]["ai_tag"] = True
            view["contacts"].append(
                {
                    "alias": "bob",
                    "display_name": "Bob",
                    "user_id": "user-bob",
                    "open_dingtalk_id": "open-bob",
                }
            )
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
            self.assertTrue(raw["dws"]["ai_tag"])
            self.assertEqual([item["alias"] for item in raw["contacts"]], ["alice", "bob"])
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
            (root / "diamond-preview.html").write_text(
                "<!doctype html><title>diamond preview</title>", encoding="utf-8"
            )
            (root / "diamond-preview.css").write_text(
                "body { color: var(--accent); }", encoding="utf-8"
            )
            (root / "diamond-favicon.svg").write_text(
                "<svg><path d='M1 0 2 1 1 2 0 1Z'/></svg>", encoding="utf-8"
            )
            (root / "theme.js").write_text("window.themeTest = true;", encoding="utf-8")
            (root / "app.css").write_text("body { color: CanvasText; }", encoding="utf-8")
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
                contact_search=lambda query: [
                    {
                        "name": query,
                        "nick": "",
                        "flowerName": "",
                        "title": "Engineer",
                        "userId": "user-search",
                        "openDingTalkId": "open-search",
                    }
                ],
            )
            server.start()
            try:
                assert server.server is not None
                base = f"http://127.0.0.1:{server.server.server_port}"
                with urlopen(f"{base}/settings", timeout=2) as response:
                    settings_page = response.read().decode("utf-8")
                self.assertIn("settings test", settings_page)
                with urlopen(f"{base}/diamond-preview", timeout=2) as response:
                    self.assertIn("diamond preview", response.read().decode("utf-8"))
                with urlopen(f"{base}/diamond-preview.css", timeout=2) as response:
                    self.assertEqual(response.headers.get_content_type(), "text/css")
                    self.assertIn("--accent", response.read().decode("utf-8"))
                with urlopen(f"{base}/diamond-favicon.svg", timeout=2) as response:
                    self.assertEqual(response.headers.get_content_type(), "image/svg+xml")
                    self.assertIn("<svg>", response.read().decode("utf-8"))
                with urlopen(f"{base}/theme.js", timeout=2) as response:
                    self.assertEqual(response.headers.get_content_type(), "text/javascript")
                    self.assertIn("themeTest", response.read().decode("utf-8"))
                with urlopen(f"{base}/app.css", timeout=2) as response:
                    self.assertEqual(response.headers.get_content_type(), "text/css")
                    self.assertIn("CanvasText", response.read().decode("utf-8"))
                with urlopen(f"{base}/favicon.ico", timeout=2) as response:
                    self.assertEqual(response.headers.get_content_type(), "image/svg+xml")
                with urlopen(f"{base}/api/config", timeout=2) as response:
                    payload = json.load(response)
                with urlopen(f"{base}/api/contacts/search?q=Alice", timeout=2) as response:
                    search = json.load(response)
                self.assertEqual(search["results"][0]["name"], "Alice")
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


class AgentFallbackConfigTests(unittest.TestCase):
    def _raw(self, fallbacks: list[str]) -> dict:
        return {
            "contacts": [],
            "agents": {
                "front-agent": {
                    "driver": "pi",
                    "model": "fast-model",
                    "read_only": True,
                    "timeout_seconds": 30,
                    "options": {},
                },
                "worker-agent": {
                    "driver": "pi",
                    "model": "strong-model",
                    "read_only": False,
                    "timeout_seconds": 300,
                    "options": {},
                    "fallbacks": fallbacks,
                },
                "backup-agent": {
                    "driver": "pi",
                    "model": "backup-model",
                    "read_only": False,
                    "timeout_seconds": 300,
                    "options": {},
                },
            },
            "workflows": {
                "active": "dm-default",
                "presets": {
                    "dm-default": {
                        "supplement_strategy": "steer",
                        "supplement_prompt": "prompts/supplement.md",
                        "front": {"agent": "front-agent", "prompt": "prompts/front.md"},
                        "worker": {"agent": "worker-agent", "prompt": "prompts/worker.md"},
                    }
                },
            },
        }

    def _from_raw(self, tmp: str, raw: dict) -> AgentRuntime:
        root = Path(tmp)
        root.mkdir(parents=True, exist_ok=True)
        (root / "prompts").mkdir(parents=True, exist_ok=True)
        for name in ("front.md", "worker.md", "supplement.md"):
            (root / "prompts" / name).write_text("x {{self_name}}", encoding="utf-8")
        config_path = root / "config.json"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        return AgentRuntime.from_config(raw, config_path, root)

    def test_fallbacks_are_parsed_and_stage_override_swaps_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._from_raw(tmp, self._raw(["backup-agent"]))
            worker = runtime._stage("worker")
            self.assertEqual(worker.profile.fallbacks, ("backup-agent",))
            overridden = runtime.resolve_stage("worker", "backup-agent")
            self.assertEqual(overridden.profile.name, "backup-agent")
            self.assertEqual(overridden.profile.model, "backup-model")
            self.assertEqual(overridden.prompt_path, worker.prompt_path)
            self.assertEqual(
                runtime.resolve_stage("worker").profile.name, "worker-agent"
            )

    def test_unknown_fallback_agent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self._from_raw(tmp, self._raw(["missing-agent"]))



if __name__ == "__main__":
    unittest.main()
