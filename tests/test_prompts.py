"""提示词渲染与「无终稿」诊断的测试。

不用 tempfile：沙箱下 tempfile 清理会 PermissionError，这里只用普通目录。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from uuid import uuid4

from dws_dm_agent.prompts import (
    SOFT_MISSING_VARIABLES,
    SUPPLEMENT_PROMPT_VARIABLES,
    render_prompt,
)


def workdir(tag: str) -> Path:
    base = Path(__file__).resolve().parent.parent / "build" / "test-tmp"
    directory = base / f"{tag}-{uuid4().hex[:12]}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class PromptRenderTests(unittest.TestCase):
    def test_soft_variable_missing_renders_empty(self) -> None:
        # 10-28 那 4 次「处理过程中发生异常」就是这个原因：group_reminder
        # 没传进来，整轮回复直接失败。
        text = render_prompt(
            "开头\n{{group_reminder}}\n正文 {{self_name}}",
            {"self_name": "张三"},
        )
        self.assertIn("正文 张三", text)
        self.assertNotIn("{{group_reminder}}", text)

    def test_console_reminder_missing_renders_empty(self) -> None:
        text = render_prompt("{{console_reminder}}答 {{self_name}}", {"self_name": "张三"})
        self.assertEqual(text.strip(), "答 张三")

    def test_hard_variable_missing_still_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing prompt variables"):
            render_prompt("{{self_name}} 与 {{contact_name}}", {"self_name": "张三"})

    def test_all_variables_present_renders_normally(self) -> None:
        text = render_prompt(
            "{{self_name}}/{{conversation_kind}}",
            {"self_name": "张三", "conversation_kind": "群聊"},
        )
        self.assertEqual(text, "张三/群聊")

    def test_soft_set_covers_the_documented_optional_variables(self) -> None:
        # 这几个都是「补充说明」性质，不该因为它们缺失而失败。
        for name in ("group_reminder", "console_reminder", "prior_attempt"):
            self.assertIn(name, SOFT_MISSING_VARIABLES)

    def test_supplement_variables_exclude_group_reminder(self) -> None:
        # 补充提示词模板本身不引用 group_reminder；列出这条是为了防止
        # 以后有人把它加进 supplement 模板却又不在 SOFT 集合里。
        self.assertNotIn("group_reminder", SUPPLEMENT_PROMPT_VARIABLES)


class MissingResultDiagnosticsTests(unittest.TestCase):
    """无终稿时要带上现场，否则审计里只有一句 agent_result_missing。"""

    def test_error_includes_stream_and_stderr_state(self) -> None:
        from types import SimpleNamespace

        from dws_dm_agent.runners.runtime import AgentSession

        directory = workdir("missingresult")
        result_path = directory / "result.txt"
        stderr_path = directory / "stderr.log"
        stderr_path.write_text(
            "Error: Cannot find module 'pi-ai'\n    at Module._resolve\n",
            encoding="utf-8",
        )

        session = AgentSession.__new__(AgentSession)
        session._final_text = ""
        session._stream_text = ""
        session._escalated = False
        session._silent = False
        session.error = ""
        session._complete = SimpleNamespace(set=lambda: None)
        session._updates = SimpleNamespace(
            put_nowait=lambda value: None, empty=lambda: True
        )
        session.prepared = SimpleNamespace(
            result_path=result_path, stderr_path=stderr_path
        )

        session._finish()

        self.assertIn("agent_result_missing", session.error)
        self.assertIn("stream_chars=0", session.error)
        self.assertIn("stderr_bytes=", session.error)
        self.assertIn("Cannot find module", session.error)

    def test_result_path_written_when_stream_text_exists(self) -> None:
        from types import SimpleNamespace

        from dws_dm_agent.runners.runtime import AgentSession

        directory = workdir("missingresult2")
        result_path = directory / "result.txt"
        stderr_path = directory / "stderr.log"

        session = AgentSession.__new__(AgentSession)
        session._final_text = ""
        session._stream_text = "半截的流式文本"
        session._escalated = False
        session._silent = False
        session.error = ""
        session._complete = SimpleNamespace(set=lambda: None)
        session._updates = SimpleNamespace(
            put_nowait=lambda value: None, empty=lambda: True
        )
        session.prepared = SimpleNamespace(
            result_path=result_path, stderr_path=stderr_path
        )

        session._finish()

        self.assertTrue(result_path.is_file())
        self.assertIn("半截的流式文本", result_path.read_text(encoding="utf-8"))
        self.assertIn("stream_chars=", session.error)


if __name__ == "__main__":
    unittest.main()
