from __future__ import annotations

import asyncio
import base64
import copy
import json
import sys
import tempfile
import unittest
from datetime import timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dws_dm_agent.front_eval import load_cases, matches_expected
from dws_dm_agent.runners import pi as pi_module
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
            # 前台显式工具集在 escalate 之外追加服务工具（no_reply/事项记忆）。
            self.assertIn(
                "read,grep,find,ls,escalate,no_reply,task_record,task_lookup",
                pi_front.argv,
            )
            self.assertNotIn("read,grep,find,ls", pi_worker.argv)
            self.assertFalse(any(value.startswith("@") for value in pi_worker.argv))

            raw = json.loads((root / "pi" / "config.json").read_text(encoding="utf-8"))
            raw["agents"]["front-agent"]["options"]["tools"] = ""
            pi = AgentRuntime.from_config(raw, root / "pi" / "config.json", root / "pi")
            pi_front = pi.prepare("front", "front-fast", "prompt", root / "pf-fast")
            # tools 为空串时 escalate 仍强制挂载，服务工具随之进入列表。
            self.assertIn(
                "escalate,no_reply,task_record,task_lookup", pi_front.argv
            )
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
            await session._handle_event(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": "最终正文"}],
                    },
                }
            )
            await session._handle_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "你好"}]},
                        {
                            "role": "assistant",
                            "stopReason": "stop",
                            "content": [{"type": "text", "text": "最终正文"}],
                        },
                    ],
                }
            )

            self.assertEqual(session.latest_progress, "先确认上下文")
            self.assertEqual(session.result(), "最终正文")

    async def test_pi_empty_agent_end_fails_safe_without_reprompting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory), "pi")
            # 未 start 的 session 没有可写的 stdin：若仍尝试重发 prompt，
            # _handle_event 会抛 BrokenPipeError 使本测试失败。
            await session._handle_event({"type": "agent_end", "messages": []})

            self.assertTrue(session.done)
            self.assertEqual(session.error, "agent_result_missing")
            with self.assertRaises(ValueError):
                session.result()

    async def test_pi_agent_end_with_will_retry_keeps_session_open_until_final(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory), "pi")
            await session._handle_event(
                {"type": "agent_end", "messages": [], "willRetry": True}
            )
            # pi 自行重试期间会话必须保持打开， runner 不重发 prompt 也不结束。
            self.assertFalse(session.done)

            await session._handle_event(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": "重试后的终稿"}],
                    },
                }
            )
            await session._handle_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "stopReason": "stop",
                            "content": [{"type": "text", "text": "重试后的终稿"}],
                        }
                    ],
                }
            )

            self.assertTrue(session.done)
            self.assertEqual(session.error, "")
            self.assertEqual(session.result(), "重试后的终稿")

    async def test_pi_already_processing_response_does_not_fail_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory), "pi")
            await session._handle_event(
                {
                    "type": "response",
                    "id": 99,
                    "success": False,
                    "error": (
                        "Agent is already processing. Specify streamingBehavior "
                        "('steer' or 'followUp') to queue the message."
                    ),
                }
            )
            # 会话仍在健康处理后续 turn，不能因此作废整场。
            self.assertFalse(session.done)
            self.assertEqual(session.error, "")

            await session._handle_event(
                {
                    "type": "response",
                    "id": 100,
                    "success": False,
                    "error": "provider unavailable",
                }
            )
            self.assertTrue(session.done)
            self.assertIn("provider unavailable", session.error)

    async def test_pi_thinking_only_run_never_returns_thinking_as_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory), "pi")
            await session._handle_event(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "thinking_delta",
                        "contentIndex": 0,
                        "delta": "先并行读取图片、检查 dws_agent 后台",
                    },
                }
            )
            await session._handle_event({"type": "agent_end", "messages": []})

            # 实时思考展示保留，但绝不作为最终回复返回。
            self.assertEqual(
                session.latest_progress, "先并行读取图片、检查 dws_agent 后台"
            )
            self.assertTrue(session.done)
            self.assertEqual(session.error, "agent_result_missing")
            with self.assertRaises(ValueError):
                session.result()

    async def test_pi_prefers_last_assistant_message_and_drops_interim_talk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory), "pi")
            interim = {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "toolUse",
                    "content": [
                        {"type": "text", "text": "先并行读取图片、检查 dws_agent 后台。"},
                        {"type": "toolCall", "toolCallId": "t1", "name": "read"},
                    ],
                },
            }
            await session._handle_event(interim)
            await session._handle_event(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": "第一版结论"}],
                    },
                }
            )
            await session._handle_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "任务"}]},
                        {
                            "role": "assistant",
                            "stopReason": "toolUse",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "先并行读取图片、检查 dws_agent 后台。",
                                },
                                {
                                    "type": "toolCall",
                                    "toolCallId": "t1",
                                    "name": "read",
                                },
                            ],
                        },
                        {
                            "role": "toolResult",
                            "toolCallId": "t1",
                            "content": [{"type": "text", "text": "ok"}],
                        },
                        {
                            "role": "assistant",
                            "stopReason": "stop",
                            "content": [{"type": "text", "text": "最终交付正文。"}],
                        },
                    ],
                }
            )

            # 中间陈述只出现在进度里；终稿取 agent_end 扫出的最后一条 assistant 文本。
            self.assertEqual(
                session.latest_progress, "先并行读取图片、检查 dws_agent 后台。"
            )
            self.assertEqual(session.result(), "最终交付正文。")

    async def test_pi_run_ending_on_interim_statement_yields_no_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory), "pi")
            await session._handle_event(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": "第一版结论"}],
                    },
                }
            )
            await session._handle_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "stopReason": "stop",
                            "content": [{"type": "text", "text": "第一版结论"}],
                        },
                        {
                            "role": "assistant",
                            "stopReason": "toolUse",
                            "content": [
                                {"type": "text", "text": "让我再确认一下。"},
                                {"type": "toolCall", "toolCallId": "t2", "name": "read"},
                            ],
                        },
                    ],
                }
            )

            # 结束时只有中间陈述、没有终稿：宁可空回复走 fallback，
            # 不能把之前的候选文本当交付发出。
            self.assertTrue(session.done)
            self.assertEqual(session.error, "agent_result_missing")
            with self.assertRaises(ValueError):
                session.result()

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


class PiNoReplyControlTests(unittest.IsolatedAsyncioTestCase):
    def _session(self, root: Path) -> AgentSession:
        runtime = AgentRuntimeTests()._runtime(root, "pi")
        return AgentSession(
            runtime.prepare("worker", "session", "prompt", root / "run"),
            "session",
        )

    async def _finish_with_final_text(self, session: AgentSession) -> None:
        await session._handle_event(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": "最终正文"}],
                },
            }
        )
        await session._handle_event(
            {
                "type": "agent_end",
                "messages": [
                    {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": "最终正文"}],
                    }
                ],
            }
        )

    async def test_pi_no_reply_tool_end_captures_silent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory))
            await session._handle_event(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "tool-1",
                    "toolName": "no_reply",
                    "result": {},
                    "isError": False,
                }
            )

            self.assertTrue(session.silent)
            self.assertTrue(session.done)
            self.assertEqual(session.error, "")
            with self.assertRaises(ValueError):
                session.result()

    async def test_pi_no_reply_tool_error_keeps_normal_reply_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory))
            await session._handle_event(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "tool-1",
                    "toolName": "no_reply",
                    "result": {},
                    "isError": True,
                }
            )

            # 工具执行失败不算沉默，模型继续正常收尾交付正文。
            self.assertFalse(session.silent)
            self.assertFalse(session.done)
            await self._finish_with_final_text(session)
            self.assertEqual(session.result(), "最终正文")

    async def test_pi_agent_end_after_silent_keeps_error_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory))
            await session._handle_event(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "tool-1",
                    "toolName": "dws__no_reply",
                    "result": {},
                    "isError": False,
                }
            )
            await session._handle_event({"type": "agent_end", "messages": []})

            self.assertTrue(session.silent)
            self.assertTrue(session.done)
            # silent 是成功终态，不能被记成 agent_result_missing。
            self.assertEqual(session.error, "")

    async def test_pi_body_after_silent_is_not_deliverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory))
            await session._handle_event(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "tool-1",
                    "toolName": "no_reply",
                    "result": {},
                    "isError": False,
                }
            )
            await self._finish_with_final_text(session)

            # 调用 silent 后忽略后续正文：没有任何路径把它当回复交付。
            self.assertTrue(session.silent)
            with self.assertRaises(ValueError):
                session.result()


class PiDwsToolsMountTests(unittest.TestCase):
    def test_pi_mounts_dws_tools_for_front_and_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntimeTests()._runtime(root, "pi")
            front = runtime.prepare("front", "front", "prompt", root / "front-run")
            worker = runtime.prepare("worker", "worker", "prompt", root / "worker-run")

            self.assertIn("pi_dws_tools.ts", " ".join(front.argv))
            self.assertIn("pi_dws_tools.ts", " ".join(worker.argv))
            self.assertIn("pi_escalate.ts", " ".join(front.argv))
            self.assertNotIn("pi_escalate.ts", " ".join(worker.argv))
            # 显式工具集只出现在 front；worker 默认全量工具，不能被收窄。
            self.assertIn(
                "read,grep,find,ls,escalate,no_reply,task_record,task_lookup",
                front.argv,
            )
            self.assertNotIn("--tools", worker.argv)

    def test_pi_prepare_injects_task_tool_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntimeTests()._runtime(root, "pi")
            task_context = {
                "db_path": "/tmp/audit.sqlite3",
                "self_id": "self-open",
                "contact_id": "u-1",
                "conversation_id": "conversation-1",
                "run_id": "session-1",
                "evidence_time": "2026-10-12T12:00:00+08:00",
                "message_ids": ["message-1"],
            }

            prepared = runtime.prepare(
                "worker", "session-1", "prompt", root / "run",
                task_context=task_context,
            )

            self.assertEqual(
                json.loads(prepared.environment["DWS_TASK_CONTEXT"]), task_context
            )
            self.assertTrue(prepared.environment["DWS_TASK_SCRIPT"].endswith("task_tools.py"))
            self.assertTrue(prepared.environment["DWS_TASK_PYTHON"])

    def test_pi_prepare_writes_refreshable_task_context_file(self) -> None:
        # 环境变量在 pi 进程启动后无法修改；上下文文件由服务在 steer 后
        # 原子重写，task_tools.py 每次调用优先读它。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntimeTests()._runtime(root, "pi")
            task_context = {
                "db_path": "/tmp/audit.sqlite3",
                "self_id": "self-open",
                "contact_id": "u-1",
                "conversation_id": "conversation-1",
                "run_id": "session-1",
                "evidence_time": "2026-10-12T12:00:00+08:00",
                "message_ids": ["message-1"],
            }

            prepared = runtime.prepare(
                "worker", "session-1", "prompt", root / "run",
                task_context=task_context,
            )

            context_file = Path(prepared.environment["DWS_TASK_CONTEXT_FILE"])
            self.assertEqual(context_file, prepared.task_context_path)
            self.assertEqual(
                json.loads(context_file.read_text(encoding="utf-8")), task_context
            )

    def test_dws_tool_runtime_files_ship_with_the_package(self) -> None:
        # pi 扩展与后端脚本是运行时依赖（--extension / DWS_TASK_SCRIPT 直接
        # 按路径引用）：文件缺失会让 pi 会话起不来，必须随 Git 交付。
        runners_dir = Path(pi_module.__file__).resolve().parent
        for name in (
            "pi_dws_tools.ts",
            "task_tools.py",
            "pi_escalate.ts",
            "escalate_mcp.py",
        ):
            self.assertTrue((runners_dir / name).is_file(), name)

    def test_non_pi_protocols_do_not_receive_task_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntimeTests()._runtime(root, "codex")
            task_context = {"db_path": "/tmp/audit.sqlite3"}

            front = runtime.prepare(
                "front", "front", "prompt", root / "front-run",
                task_context=task_context,
            )

            self.assertNotIn("DWS_TASK_CONTEXT", front.environment)
            self.assertNotIn("DWS_TASK_SCRIPT", front.environment)


class ServiceToolPromptConstraintTests(unittest.TestCase):
    """提示词引用服务工具（no_reply/task_record/task_lookup）时的协议约束。

    这些工具只在 pi-rpc 挂载；非 pi 的阶段 agent 或备用链配服务工具
    提示词必须在加载期报错，而不是让模型收到指向不存在工具的指令。
    """

    def test_non_pi_worker_loads_when_its_prompts_skip_service_tools(self) -> None:
        # 逐阶段校验：前台提示词引用服务工具只约束前台 agent；worker 提示词
        # 与补充提示词都不引用时，worker 及其备用链可继续使用非 pi 协议。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            AgentRuntimeTests()._runtime(root, "codex")
            (root / "prompts" / "front.md").write_text(
                "front {{self_name}} 可调用 no_reply()、task_record、task_lookup",
                encoding="utf-8",
            )
            raw = json.loads((root / "config.json").read_text(encoding="utf-8"))
            raw["agents"]["front-agent"] = {
                "driver": "pi",
                "model": "fast-model",
                "read_only": True,
                "timeout_seconds": 30,
                "options": {},
            }

            runtime = AgentRuntime.from_config(raw, root / "config.json", root)

            self.assertEqual(runtime.profile_name("front"), "front-agent")
            self.assertEqual(runtime.profile_name("worker"), "worker-agent")

    def test_shipped_prompts_with_example_pi_default_load(self) -> None:
        # 当前默认预设（config.example.json 的 pi-default，全 pi 链）叠加
        # 仓库自带提示词（front/worker/supplement 都引用服务工具）必须原样
        # 通过加载期校验。
        root = Path(__file__).resolve().parents[1]
        raw = json.loads((root / "config.example.json").read_text(encoding="utf-8"))

        runtime = AgentRuntime.from_config(
            raw, root / "config.example.json", root
        )

        self.assertEqual(runtime.profile_name("front"), "pi-front")
        self.assertEqual(runtime.profile_name("worker"), "pi-worker")

    def test_non_pi_agents_with_service_tool_prompt_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            AgentRuntimeTests()._runtime(root, "codex")
            self.assertIsNotNone(
                AgentRuntime.from_config(
                    json.loads((root / "config.json").read_text(encoding="utf-8")),
                    root / "config.json",
                    root,
                )
            )
            (root / "prompts" / "worker.md").write_text(
                "worker {{self_name}} 可调用 no_reply() 沉默收尾，"
                "并用 task_record/task_lookup 维护事项",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as ctx:
                AgentRuntime.from_config(
                    json.loads((root / "config.json").read_text(encoding="utf-8")),
                    root / "config.json",
                    root,
                )
            self.assertIn("service tools", str(ctx.exception))
            self.assertIn("codex-app-server", str(ctx.exception))
            self.assertIn("cannot mount them", str(ctx.exception))

    def test_pi_agents_with_service_tool_prompt_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            AgentRuntimeTests()._runtime(root, "pi")
            (root / "prompts" / "worker.md").write_text(
                "worker {{self_name}} 可调用 no_reply()、task_record、task_lookup",
                encoding="utf-8",
            )

            runtime = AgentRuntime.from_config(
                json.loads((root / "config.json").read_text(encoding="utf-8")),
                root / "config.json",
                root,
            )

            self.assertEqual(runtime.profile_name("worker"), "worker-agent")

    def test_non_pi_fallback_with_service_tool_prompt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            AgentRuntimeTests()._runtime(root, "pi")
            (root / "prompts" / "worker.md").write_text(
                "worker {{self_name}} 可调用 no_reply()", encoding="utf-8"
            )
            raw = json.loads((root / "config.json").read_text(encoding="utf-8"))
            raw["agents"]["codex-backup"] = {
                "driver": "codex",
                "binary": "codex",
                "model": "backup-model",
                "reasoning_effort": "high",
                "read_only": False,
                "timeout_seconds": 30,
                "options": {},
            }
            raw["agents"]["worker-agent"]["fallbacks"] = ["codex-backup"]

            with self.assertRaises(ValueError) as ctx:
                AgentRuntime.from_config(raw, root / "config.json", root)
            self.assertIn("codex-backup", str(ctx.exception))

    def test_service_tool_supplement_prompt_constrains_all_stages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            AgentRuntimeTests()._runtime(root, "codex")
            (root / "prompts" / "supplement.md").write_text(
                "supplement {{supplement_messages_json}} 无需回复时调用 no_reply()",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as ctx:
                AgentRuntime.from_config(
                    json.loads((root / "config.json").read_text(encoding="utf-8")),
                    root / "config.json",
                    root,
                )
            self.assertIn("service tools", str(ctx.exception))


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


class PiNativeImageInputTests(unittest.TestCase):
    """pi RPC 原生图片输入：首条 prompt 命令以 ImageContent 携带 base64 图片。"""

    def test_prepare_encodes_local_images_as_image_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntimeTests()._runtime(root, "pi")
            png = root / "shot.png"
            png_bytes = b"\x89PNG\r\n\x1a\nfake-png"
            png.write_bytes(png_bytes)
            jpg = root / "shot.jpg"
            jpg_bytes = b"\xff\xd8\xfffake-jpg"
            jpg.write_bytes(jpg_bytes)
            prepared = runtime.prepare(
                "worker", "session", "prompt", root / "run", (png, jpg)
            )

            self.assertEqual(prepared.protocol, "pi-rpc")
            self.assertEqual(
                prepared.pi_images,
                (
                    {
                        "type": "image",
                        "data": base64.b64encode(png_bytes).decode("ascii"),
                        "mimeType": "image/png",
                    },
                    {
                        "type": "image",
                        "data": base64.b64encode(jpg_bytes).decode("ascii"),
                        "mimeType": "image/jpeg",
                    },
                ),
            )
            # pi 不再走 codex 的 localImage 路径输入
            self.assertEqual(prepared.local_image_paths, ())

    def test_prepare_rejects_unknown_image_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntimeTests()._runtime(root, "pi")
            bmp = root / "shot.bmp"
            bmp.write_bytes(b"BMfake")
            with self.assertRaisesRegex(ValueError, "unsupported pi image format"):
                runtime.prepare("worker", "session", "prompt", root / "run", (bmp,))

    def test_first_prompt_command_carries_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntimeTests()._runtime(root, "pi")
            png = root / "shot.png"
            png_bytes = b"\x89PNG\r\n\x1a\nfake-png"
            png.write_bytes(png_bytes)
            prepared = runtime.prepare(
                "worker", "session", "看下这张截图", root / "run", (png,)
            )
            command = AgentSession(prepared, "session")._pi_prompt_command()

            self.assertEqual(command["type"], "prompt")
            self.assertEqual(command["message"], "看下这张截图")
            self.assertEqual(
                command["images"],
                [
                    {
                        "type": "image",
                        "data": base64.b64encode(png_bytes).decode("ascii"),
                        "mimeType": "image/png",
                    }
                ],
            )

    def test_prompt_command_without_images_omits_the_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntimeTests()._runtime(root, "pi")
            prepared = runtime.prepare("worker", "session", "prompt", root / "run")
            command = AgentSession(prepared, "session")._pi_prompt_command()

            self.assertNotIn("images", command)
            self.assertEqual(
                command, {"type": "prompt", "message": "prompt"}
            )

    def test_codex_prepare_still_uses_local_image_paths(self) -> None:
        # 兼容守卫：codex 继续以 localImage 路径输入，不产生 pi_images
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntimeTests()._runtime(root, "codex")
            png = root / "shot.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\nfake-png")
            prepared = runtime.prepare(
                "worker", "session", "prompt", root / "run", (png,)
            )

            self.assertEqual(prepared.protocol, "codex-app-server")
            self.assertEqual(prepared.local_image_paths, (png,))
            self.assertEqual(prepared.pi_images, ())


# 模拟 pi RPC 子进程：先回 prompt 命令的 response，再输出一条带整批
# base64 图片的 agent_end 大事件行，用于验证事件流单行上限。
_BIG_LINE_EMITTER = """
import base64, json, sys

request = json.loads(sys.stdin.readline())
print(json.dumps({{
    "id": request.get("id"),
    "type": "response",
    "command": "prompt",
    "success": True,
}}), flush=True)

image = base64.b64encode(
    b"\\x89PNG\\r\\n\\x1a\\n" + b"a" * ({image_bytes} - 8)
).decode("ascii")
agent_end = {{
    "type": "agent_end",
    "messages": [
        {{
            "role": "user",
            "content": [
                {{"type": "text", "text": "看下这四张截图"}},
                *[
                    {{"type": "image", "data": image, "mimeType": "image/png"}}
                    for _ in range({image_count})
                ],
            ],
        }},
        {{
            "role": "assistant",
            "stopReason": "stop",
            "content": [{{"type": "text", "text": "最终正文"}}],
        }},
    ],
}}
print(json.dumps(agent_end), flush=True)
"""


class PiEventStreamLimitTests(unittest.IsolatedAsyncioTestCase):
    """事件流单行上限与图片预算联动：允许下载的整批图片必须能原样读回。"""

    async def _close_session(self, session: AgentSession) -> None:
        """关闭会话并在本测试事件循环存活时回收子进程 transport，避免跨测试
        触发 BaseSubprocessTransport.__del__ 的 Event loop is closed 噪音。"""
        import gc

        await session.close()
        # session.process 仍持有子进程 transport 引用：先解除引用并让
        # connection_lost/process_exited 回调跑完，再在本循环存活时回收，
        # 避免跨测试触发 __del__ 的 Event loop is closed 噪音。
        session.process = None
        for _ in range(4):
            await asyncio.sleep(0)
        gc.collect()

    def test_stream_limit_covers_worst_case_image_echo(self) -> None:
        # 上限必须 ≥ 整批图片 base64 膨胀（pi 会把带图用户消息原样回传），
        # 且不小于旧默认 8MiB；service 下载侧常量必须来自同一 core 定义。
        from dws_dm_agent.runners import runtime as runtime_module
        from dws_dm_agent.core import MAX_IMAGES, MAX_IMAGE_BYTES

        worst_case_base64 = (MAX_IMAGES * MAX_IMAGE_BYTES * 4 + 2) // 3
        self.assertGreaterEqual(
            runtime_module._EVENT_STREAM_LIMIT, worst_case_base64
        )
        self.assertGreater(runtime_module._EVENT_STREAM_LIMIT, 8 * 1024 * 1024)
        from dws_dm_agent import service as service_module

        self.assertIs(service_module._MAX_IMAGES, MAX_IMAGES)
        self.assertIs(service_module._MAX_IMAGE_BYTES, MAX_IMAGE_BYTES)

    async def test_worst_case_image_echo_line_is_read_back(self) -> None:
        # 真实子进程读回：构造 4×20MiB 图片预算的 agent_end 单行（约 107MiB），
        # 会话必须完整读回、解析并提取终稿，而不是 event_stream_error。
        from dws_dm_agent.core import MAX_IMAGES, MAX_IMAGE_BYTES

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            emitter = root / "emitter.py"
            emitter.write_text(
                _BIG_LINE_EMITTER.format(
                    image_bytes=MAX_IMAGE_BYTES, image_count=MAX_IMAGES
                ),
                encoding="utf-8",
            )
            runtime = AgentRuntimeTests()._runtime(
                root,
                "pi",
                command=[sys.executable, str(emitter)],
                protocol="pi-rpc",
            )
            session = AgentSession(
                runtime.prepare("worker", "session", "prompt", root / "run"),
                "session",
            )
            try:
                await session.start()
                await session.wait(timeout=180)
                self.assertEqual(session.error, "")
                self.assertEqual(session.result(), "最终正文")
                # 大行完整落盘：至少超过单张图片的 base64 体积
                self.assertGreater(
                    session.prepared.events_path.stat().st_size,
                    MAX_IMAGE_BYTES * 4 // 3,
                )
            finally:
                await self._close_session(session)

    async def test_line_beyond_limit_fails_gracefully(self) -> None:
        # 行长超过上限时按 event_stream_error 失败会话（可走 fallback 链），
        # 不能挂死或抛未捕获异常；用缩小后的上限验证该降级路径仍在。
        from dws_dm_agent.runners import runtime as runtime_module
        from dws_dm_agent.core import MAX_IMAGE_BYTES

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            emitter = root / "emitter.py"
            emitter.write_text(
                _BIG_LINE_EMITTER.format(image_bytes=MAX_IMAGE_BYTES, image_count=2),
                encoding="utf-8",
            )
            runtime = AgentRuntimeTests()._runtime(
                root,
                "pi",
                command=[sys.executable, str(emitter)],
                protocol="pi-rpc",
            )
            session = AgentSession(
                runtime.prepare("worker", "session", "prompt", root / "run"),
                "session",
            )
            try:
                with patch.object(
                    runtime_module, "_EVENT_STREAM_LIMIT", 1024 * 1024
                ):
                    await session.start()
                    await session.wait(timeout=60)
                self.assertTrue(session.done)
                self.assertTrue(
                    session.error.startswith("event_stream_error"),
                    session.error,
                )
            finally:
                await self._close_session(session)


if __name__ == "__main__":
    unittest.main()
