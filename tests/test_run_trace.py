"""调用链记录的单元测试。

覆盖三件容易出错的事：
  1. 工具参数来自 start 事件、结果来自 end 事件，要能拼起来
  2. end 事件自带 durationMs，优先用它
  3. status 由 set_outcome 写入，finish 时兜底带上
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path

from dws_dm_agent.run_trace import (
    DEFAULT_RETENTION_DAYS,
    STEP_GUARD,
    STEP_TOOL,
    RunTrace,
    list_traces,
    load_trace,
    purge_expired,
)


class RunTraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent.parent / "build" / "test-tmp" / (
            f"runtrace-{os.getpid()}-{id(self)}"
        )
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.trace = RunTrace(
            self.root, trace_id="20261123-120000-abcd", contact="测试"
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_trace_file_named_after_trace_id(self) -> None:
        path = self.trace.finish(status="sent")
        self.assertEqual(path.name, "20261123-120000-abcd.json")

    def test_tool_step_keeps_args_and_result(self) -> None:
        # 参数和结果都要留下：排查时最想知道这一步到底在干什么
        self.trace.tool(
            "bash", started_ms=100, duration_ms=250,
            content="参数: {command: ls}\n结果: a.txt\nb.txt",
        )
        path = self.trace.finish(status="sent")
        data = load_trace(path)
        steps = [s for s in data["steps"] if s["kind"] == STEP_TOOL]
        self.assertEqual(len(steps), 1)
        self.assertIn("command: ls", steps[0]["content"])
        self.assertIn("a.txt", steps[0]["content"])
        self.assertEqual(steps[0]["duration"], 250)

    def test_status_written_from_outcome(self) -> None:
        self.trace.set_outcome("sent")
        path = self.trace.finish()
        data = load_trace(path)
        self.assertEqual(data["status"], "sent")

    def test_explicit_status_beats_outcome(self) -> None:
        self.trace.set_outcome("sent")
        path = self.trace.finish(status="error", reason="boom")
        data = load_trace(path)
        self.assertEqual(data["status"], "error")
        self.assertEqual(data["reason"], "boom")

    def test_guard_keeps_full_original_text(self) -> None:
        original = "这台库我连不上，需要你提供权限。" * 20
        self.trace.guard("asking_back", blocked=True, content=original)
        path = self.trace.finish(status="sent")
        data = load_trace(path)
        guards = [s for s in data["steps"] if s["kind"] == STEP_GUARD]
        self.assertEqual(len(guards), 1)
        self.assertFalse(guards[0]["ok"])
        # 全文保留，不截断
        self.assertEqual(guards[0]["content"], original)

    def test_stage_pairing(self) -> None:
        self.trace.stage_begin("前台判断")
        self.trace.stage_end("前台判断")
        path = self.trace.finish(status="sent")
        data = load_trace(path)
        stages = [s for s in data["steps"] if s["kind"] == "stage"]
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0]["name"], "前台判断")

    def test_unclosed_stage_is_closed_on_finish(self) -> None:
        # 中途抛异常时，没关的阶段也要补上，否则时间轴上会出现空洞
        self.trace.stage_begin("执行")
        path = self.trace.finish(status="error")
        data = load_trace(path)
        stages = [s for s in data["steps"] if s["kind"] == "stage"]
        self.assertEqual(len(stages), 1)

    def test_list_traces_summarises(self) -> None:
        self.trace.tool("bash", started_ms=0, duration_ms=5000, content="x")
        self.trace.tool("read", started_ms=5000, duration_ms=12000, content="y")
        self.trace.guard("asking_back", blocked=True, content="z")
        self.trace.finish(status="sent")

        items = list_traces(self.root)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["traceId"], "20261123-120000-abcd")
        self.assertEqual(item["toolCount"], 2)
        # 最慢的工具要能一眼看出来
        self.assertEqual(item["slowestTool"]["name"], "read")
        self.assertTrue(item["blocked"])

    def test_purge_expired_removes_old_files(self) -> None:
        # 造一个 10 天前的文件
        old = self.root / "20261113-120000-old0.json"
        self.root.mkdir(parents=True, exist_ok=True)
        old.write_text(json.dumps({"traceId": "old"}), encoding="utf-8")
        self.trace.finish(status="sent")

        removed = purge_expired(self.root, DEFAULT_RETENTION_DAYS)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        # 当天的要留下
        self.assertEqual(len(list_traces(self.root)), 1)

    def test_absolute_start_ms_is_converted_to_offset(self) -> None:
        # 调用方传的是 time.monotonic() 的毫秒值（开机以来的时长），
        # 直接展示会变成几万分钟，必须换算成相对本次处理的偏移。
        import time as _time
        base = int(_time.monotonic() * 1000)
        self.trace._base_ms = base
        self.trace.tool("bash", started_ms=base + 1200, duration_ms=300, content="x")
        path = self.trace.finish(status="sent")
        data = load_trace(path)
        step = data["steps"][0]
        self.assertLess(step["started"], 5000, "偏移应在一秒量级")

    def test_small_start_ms_kept_as_offset(self) -> None:
        # 本来就是偏移的（比如手工构造），原样保留
        self.trace.tool("bash", started_ms=2100, duration_ms=100, content="x")
        path = self.trace.finish(status="sent")
        self.assertEqual(load_trace(path)["steps"][0]["started"], 2100)

    def test_finish_is_idempotent(self) -> None:
        first = self.trace.finish(status="sent")
        second = self.trace.finish(status="sent")
        self.assertIsNotNone(first)
        self.assertIsNone(second)


if __name__ == "__main__":
    unittest.main()
