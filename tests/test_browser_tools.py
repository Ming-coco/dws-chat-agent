"""浏览器能力（browser_tools.py + pi_browser.ts）的回归测试。

覆盖四件事：
1. 步骤校验要挡住未知动作、空脚本、超量步骤，并给出可操作的中文原因；
2. 断言失败必须如实报失败并带上实际内容，不能把「没找到」当成通过；
3. 浏览器定位要能用 BROWSER_PATH 覆盖，找不到时给出明确指引；
4. pi 命令行默认挂载 pi_browser.ts，且不得把 worker 的全量工具误收窄。

不启动真实浏览器——那部分靠手工端到端脚本验证；这里只锁协议与判定逻辑。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dws_dm_agent.runners import browser_tools
from dws_dm_agent.runners import pi as pi_module


class _Profile:
    """最小化的 AgentProfile 替身，只保留 pi.command 读取的字段。"""

    command = ["node", "cli.js"]
    model = "m"
    reasoning_effort = "low"
    extra_args: list[str] = []

    def __init__(self, read_only: bool = False, **options: object) -> None:
        self.read_only = read_only
        self.options = options


class SettingTests(unittest.TestCase):
    def setUp(self) -> None:
        browser_tools._ENV_CACHE.clear()

    def test_env_var_wins_over_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("BROWSER_PATH=/from/file\n", encoding="utf-8")
            with mock.patch.dict(
                browser_tools.os.environ,
                {
                    "BROWSER_PATH": "/from/env",
                    "DWS_BROWSER_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                self.assertEqual(browser_tools._setting("BROWSER_PATH"), "/from/env")

    def test_generic_env_file_variable_also_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("BROWSER_HEADLESS=0\n", encoding="utf-8")
            with mock.patch.dict(browser_tools.os.environ, {}, clear=True):
                browser_tools.os.environ["DWS_AGENT_ENV_FILE"] = str(env_file)
                self.assertEqual(browser_tools._setting("BROWSER_HEADLESS"), "0")

    def test_headless_defaults_on(self) -> None:
        with mock.patch.dict(browser_tools.os.environ, {}, clear=True):
            self.assertTrue(browser_tools._headless())

    def test_headless_can_be_disabled(self) -> None:
        with mock.patch.dict(
            browser_tools.os.environ, {"BROWSER_HEADLESS": "false"}, clear=True
        ):
            self.assertFalse(browser_tools._headless())


class FindBrowserTests(unittest.TestCase):
    def test_configured_path_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "chrome.exe"
            fake.write_bytes(b"")
            with mock.patch.dict(
                browser_tools.os.environ, {"BROWSER_PATH": str(fake)}, clear=True
            ):
                self.assertEqual(browser_tools.find_browser(), str(fake))

    def test_configured_path_that_is_missing_is_reported(self) -> None:
        with mock.patch.dict(
            browser_tools.os.environ, {"BROWSER_PATH": "/nope/chrome"}, clear=True
        ):
            with self.assertRaises(browser_tools.BrowserError) as ctx:
                browser_tools.find_browser()
        self.assertIn("BROWSER_PATH", str(ctx.exception))

    def test_missing_browser_explains_how_to_fix(self) -> None:
        with mock.patch.dict(browser_tools.os.environ, {}, clear=True):
            with mock.patch.object(browser_tools.platform, "system", return_value="Windows"):
                with mock.patch.object(browser_tools.Path, "is_file", return_value=False):
                    with self.assertRaises(browser_tools.BrowserError) as ctx:
                        browser_tools.find_browser()
        message = str(ctx.exception)
        self.assertIn("BROWSER_PATH", message)
        self.assertIn(".env", message)


class StepValidationTests(unittest.TestCase):
    def test_empty_steps_is_rejected(self) -> None:
        with self.assertRaises(browser_tools.BrowserError) as ctx:
            browser_tools.run_script([])
        self.assertIn("步骤", str(ctx.exception))

    def test_unknown_action_lists_supported_ones(self) -> None:
        with self.assertRaises(browser_tools.BrowserError) as ctx:
            browser_tools.run_step(None, {"drag": "#a"}, 0)  # type: ignore[arg-type]
        message = str(ctx.exception)
        self.assertIn("drag", message)
        self.assertIn("expect_text", message)

    def test_too_many_steps_is_rejected(self) -> None:
        steps = [{"wait": 1} for _ in range(browser_tools.MAX_STEPS + 1)]
        with self.assertRaises(browser_tools.BrowserError) as ctx:
            browser_tools.run_script(steps)
        self.assertIn(str(browser_tools.MAX_STEPS), str(ctx.exception))

    def test_non_object_step_is_rejected(self) -> None:
        with self.assertRaises(browser_tools.BrowserError):
            browser_tools.run_step(None, "click", 0)  # type: ignore[arg-type]

    def test_bare_url_is_treated_as_goto(self) -> None:
        # url 字符串是常见简写，应当等价于单个 goto 步骤。
        with mock.patch.object(browser_tools, "Session") as session_cls:
            session_cls.return_value.start.return_value = None
            session_cls.return_value.evaluate.return_value = ""
            captured: list = []

            def fake_run(session, step, index):  # noqa: ANN001
                captured.append(step)
                return {"step": index, "action": "goto"}

            with mock.patch.object(browser_tools, "run_step", side_effect=fake_run):
                result = browser_tools.run_script(None, url="https://example.com")
        self.assertTrue(result["ok"])
        self.assertEqual(captured, [{"goto": "https://example.com"}])

    def test_timeout_is_clamped(self) -> None:
        self.assertEqual(
            browser_tools._step_timeout({"timeout_ms": 1}), 200
        )
        self.assertEqual(
            browser_tools._step_timeout({"timeout_ms": 10**9}),
            browser_tools.MAX_TIMEOUT_MS,
        )
        self.assertEqual(
            browser_tools._step_timeout({}), browser_tools.DEFAULT_TIMEOUT_MS
        )


class AssertionTests(unittest.TestCase):
    """断言失败必须如实失败，并带上实际内容。"""

    def _session(self, text: str) -> mock.MagicMock:
        session = mock.MagicMock()
        session.evaluate.return_value = text
        return session

    def test_expect_text_passes_when_present(self) -> None:
        session = self._session("欢迎回来，张三")
        result = browser_tools.run_step(
            session, {"expect_text": {"contains": "欢迎回来"}}, 0
        )
        self.assertTrue(result["ok"])

    def test_expect_text_fails_with_actual_content(self) -> None:
        session = self._session("页面报错了")
        with self.assertRaises(browser_tools.BrowserError) as ctx:
            browser_tools.run_step(
                session, {"expect_text": {"contains": "仪表盘"}}, 0
            )
        message = str(ctx.exception)
        self.assertIn("断言失败", message)
        self.assertIn("仪表盘", message)
        self.assertIn("页面报错了", message)

    def test_missing_element_is_reported_as_failure(self) -> None:
        session = mock.MagicMock()
        session.evaluate.return_value = None
        with self.assertRaises(browser_tools.BrowserError) as ctx:
            browser_tools.run_step(session, {"text": "#missing"}, 0)
        self.assertIn("#missing", str(ctx.exception))

    def test_missing_clickable_element_is_reported(self) -> None:
        # click 走的是 Session.element_center（它自己带「找不到元素」的判定），
        # 所以这里要把真方法绑到替身上，否则测的只是替身的行为。
        session = mock.MagicMock()
        session.evaluate.return_value = None
        session.element_center = lambda selector, index=0: (
            browser_tools.Session.element_center(session, selector, index)
        )
        with self.assertRaises(browser_tools.BrowserError) as ctx:
            browser_tools.run_step(session, {"click": "#nope"}, 0)
        self.assertIn("#nope", str(ctx.exception))

    def test_click_text_reports_the_needle(self) -> None:
        session = mock.MagicMock()
        session.evaluate.return_value = None
        with self.assertRaises(browser_tools.BrowserError) as ctx:
            browser_tools.run_step(session, {"click_text": "提交审批"}, 0)
        self.assertIn("提交审批", str(ctx.exception))

    def test_eval_exception_is_surfaced(self) -> None:
        session = mock.MagicMock()
        session.evaluate.side_effect = browser_tools.BrowserError("页面脚本出错：boom")
        with self.assertRaises(browser_tools.BrowserError) as ctx:
            browser_tools.run_step(session, {"eval": "throw new Error('boom')"}, 0)
        self.assertIn("boom", str(ctx.exception))


class HandleTests(unittest.TestCase):
    def test_unknown_tool_is_reported(self) -> None:
        result = browser_tools.handle({"tool": "browser_click", "args": {}})
        self.assertFalse(result["ok"])
        self.assertIn("browser_click", result["error"])

    def test_non_object_request_is_reported(self) -> None:
        self.assertFalse(browser_tools.handle("nope")["ok"])

    def test_failure_keeps_completed_steps(self) -> None:
        # 前几步成功、后面失败时，已完成的步骤必须保留，便于定位断在哪。
        with mock.patch.object(browser_tools, "Session"):
            with mock.patch.object(
                browser_tools,
                "run_step",
                side_effect=[
                    {"step": 0, "action": "goto"},
                    browser_tools.BrowserError("第二步炸了"),
                ],
            ):
                result = browser_tools.run_script([{"goto": "u"}, {"click": "#a"}])
        self.assertFalse(result["ok"])
        self.assertIn("第二步炸了", result["error"])
        self.assertEqual(len(result["steps"]), 1)


class PiWiringTests(unittest.TestCase):
    def test_browser_extension_is_mounted_by_default(self) -> None:
        argv = pi_module.command(_Profile(), False)
        self.assertTrue(any("pi_browser.ts" in arg for arg in argv))

    def test_browser_extension_can_be_disabled(self) -> None:
        argv = pi_module.command(_Profile(browser=False), False)
        self.assertFalse(any("pi_browser.ts" in arg for arg in argv))

    def test_browser_tool_joins_an_explicit_allowlist(self) -> None:
        argv = pi_module.command(_Profile(read_only=True, tools="read,grep"), True)
        tools = argv[argv.index("--tools") + 1]
        self.assertIn("browser_run", tools)

    def test_browser_does_not_restrict_the_worker_toolset(self) -> None:
        argv = pi_module.command(_Profile(), False)
        self.assertNotIn("--tools", argv)
        self.assertNotIn("--no-tools", argv)


if __name__ == "__main__":
    unittest.main()
