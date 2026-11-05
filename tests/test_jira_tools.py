"""issue 读取能力（jira_tools.py + pi_jira.ts）的回归测试。

覆盖四件事：
1. 单号提取要能吃下裸单号和 browse 链接；
2. Jira 返回的 ADF/富文本要摊平成模型能读的文本，不能吐原始 JSON；
3. pi 的命令行默认挂载 pi_jira.ts，且不得把 worker 的全量工具误收窄成白名单
   （这条曾经是真实的回归点）；
4. 提示词必须写明「状态/解决结果只是工作流字段，不是部署证据」——真出过一次：
   拿「生产待发布(测试)」当结论去跟人确认上线与否，等于用字段代替事实。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dws_dm_agent.runners import jira_tools
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


class IssueKeyTests(unittest.TestCase):
    def test_requested_fields_cover_everything_render_reads(self) -> None:
        # 真出过一次：render_issue 支持评论，但 _FIELDS 里没有 comment，
        # Jira 就不返回评论，看起来像「这个单没有评论」。渲染读到的字段
        # 必须都在请求列表里。
        for field in (
            "summary",
            "status",
            "resolution",
            "issuetype",
            "assignee",
            "reporter",
            "created",
            "updated",
            "duedate",
            "labels",
            "components",
            "fixVersions",
            "versions",
            "description",
            "comment",
            "parent",
            "subtasks",
            "issuelinks",
            "attachment",
            "project",
            "environment",
        ):
            with self.subTest(field=field):
                self.assertIn(field, jira_tools._FIELDS)

    def test_plain_key(self) -> None:
        self.assertEqual(jira_tools.extract_issue_key("CQNSHCYDN-1422"), "CQNSHCYDN-1422")

    def test_browse_link_with_query(self) -> None:
        # 楚芸当天发的就是这种带 ?filter=-2 的链接。
        self.assertEqual(
            jira_tools.extract_issue_key(
                "https://issue.hsjdata.com/browse/CQNSHCYDN-1422?filter=-2"
            ),
            "CQNSHCYDN-1422",
        )

    def test_key_inside_sentence(self) -> None:
        self.assertEqual(
            jira_tools.extract_issue_key("这个 CQNSHCYDN-1447 你看下什么时候上线"),
            "CQNSHCYDN-1447",
        )

    def test_project_key_may_contain_digits_and_underscores(self) -> None:
        self.assertEqual(jira_tools.extract_issue_key("AB2_C-9"), "AB2_C-9")

    def test_no_key_returns_empty(self) -> None:
        for value in ("", "没有单号", "abc-123", None):
            with self.subTest(value=value):
                self.assertEqual(jira_tools.extract_issue_key(value), "")


class RenderTests(unittest.TestCase):
    def _issue(self, **fields: object) -> dict:
        base = {
            "summary": "行政区划字典表更新",
            "status": {"name": "已上线"},
            "resolution": {"name": "完成"},
            "issuetype": {"name": "需求"},
            "assignee": {"displayName": "叶锦洋"},
            "reporter": {"displayName": "龚楚芸"},
            "created": "2026-10-29T10:20:31.000+0800",
            "updated": "2026-11-03T18:20:31.000+0800",
            "description": None,
        }
        base.update(fields)
        return {"key": "CQNSHCYDN-1447", "fields": base}

    def test_basic_fields_are_readable(self) -> None:
        text = jira_tools.render_issue(self._issue(), 10)
        self.assertIn("CQNSHCYDN-1447", text)
        self.assertIn("行政区划字典表更新", text)
        self.assertIn("已上线", text)
        self.assertIn("叶锦洋", text)
        # 时间要压成 2026-10-29 10:20，不要原样的 ISO 串。
        self.assertIn("2026-10-29 10:20", text)
        self.assertNotIn("T10:20:31", text)

    def test_adf_description_is_flattened(self) -> None:
        adf = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "背景说明"}]},
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "第一条"}],
                                }
                            ],
                        }
                    ],
                },
                {"type": "mention", "attrs": {"text": "叶锦洋"}},
            ],
        }
        text = jira_tools.render_issue(self._issue(description=adf), 10)
        self.assertIn("背景说明", text)
        self.assertIn("第一条", text)
        self.assertIn("@叶锦洋", text)
        # 不能把 ADF 的原始 JSON 泄露给模型。
        self.assertNotIn('"type": "doc"', text)

    def test_wiki_string_description_passes_through(self) -> None:
        text = jira_tools.render_issue(self._issue(description="h3. 验收\n* 数据已上线"), 10)
        self.assertIn("数据已上线", text)

    def test_missing_description_is_explicit(self) -> None:
        self.assertIn("（无描述）", jira_tools.render_issue(self._issue(), 10))

    def test_comments_are_trimmed_to_the_most_recent(self) -> None:
        comments = {
            "total": 3,
            "comments": [
                {"author": {"displayName": "甲"}, "body": "最早", "created": "2026-11-01T09:00:00.000+0800"},
                {"author": {"displayName": "乙"}, "body": "中间", "created": "2026-11-02T09:00:00.000+0800"},
                {"author": {"displayName": "丙"}, "body": "最新", "created": "2026-11-03T09:00:00.000+0800"},
            ],
        }
        text = jira_tools.render_issue(self._issue(comment=comments), 2)
        self.assertIn("共 3 条", text)
        self.assertIn("最新", text)
        self.assertIn("中间", text)
        self.assertNotIn("最早", text)

    def test_subtasks_and_links_are_listed(self) -> None:
        issue = self._issue(
            subtasks=[
                {"key": "CQNSHCYDN-1448", "fields": {"summary": "补字典", "status": {"name": "进行中"}}}
            ],
            issuelinks=[
                {
                    "type": {"outward": "blocks"},
                    "outwardIssue": {"key": "CQNSHCYDN-1500", "fields": {"summary": "下游任务"}},
                }
            ],
        )
        text = jira_tools.render_issue(issue, 10)
        self.assertIn("CQNSHCYDN-1448", text)
        self.assertIn("补字典", text)
        self.assertIn("blocks", text)
        self.assertIn("CQNSHCYDN-1500", text)

    def test_long_description_is_truncated(self) -> None:
        text = jira_tools.render_issue(self._issue(description="啊" * 9000), 10)
        self.assertIn("已截断", text)
        self.assertLess(len(text), 9000)

    def test_missing_fields_raises(self) -> None:
        with self.assertRaises(jira_tools.JiraError):
            jira_tools.render_issue({"key": "X-1"}, 10)


class HandleTests(unittest.TestCase):
    def test_missing_token_is_reported_with_actionable_text(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            result = jira_tools.handle(
                {"tool": "jira_issue", "args": {"issue": "CQNSHCYDN-1422"}}
            )
        self.assertFalse(result["ok"])
        # 报错要说清楚缺什么，否则模型只会说「读不了」。
        self.assertIn("JIRA_PAT", result["error"])

    def test_no_key_is_rejected_before_any_request(self) -> None:
        result = jira_tools.handle({"tool": "jira_issue", "args": {}})
        self.assertFalse(result["ok"])
        self.assertIn("单号", result["error"])

    def test_unknown_tool_is_rejected(self) -> None:
        result = jira_tools.handle({"tool": "jira_delete", "args": {}})
        self.assertFalse(result["ok"])

    def test_comment_count_is_clamped(self) -> None:
        key, count = jira_tools._parse_args({"issue": "A-1", "max_comments": 999})
        self.assertEqual(key, "A-1")
        self.assertEqual(count, jira_tools.MAX_MAX_COMMENTS)


class DotenvFallbackTests(unittest.TestCase):
    """服务不把 .env 注入子进程，令牌必须能从 .env 读到。"""

    def _env_file(self, directory: str, body: str) -> Path:
        path = Path(directory) / ".env"
        path.write_text(body, encoding="utf-8")
        return path

    def test_reads_token_from_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._env_file(directory, "JIRA_PAT=tok-123\nJIRA_BASE_URL=http://example.test\n")
            with mock.patch.dict(
                "os.environ", {"DWS_JIRA_ENV_FILE": str(path)}, clear=True
            ):
                jira_tools._ENV_CACHE.clear()
                self.assertEqual(jira_tools._setting("JIRA_PAT"), "tok-123")
                self.assertEqual(jira_tools._setting("JIRA_BASE_URL"), "http://example.test")

    def test_process_environment_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._env_file(directory, "JIRA_PAT=from-file\n")
            with mock.patch.dict(
                "os.environ",
                {"DWS_JIRA_ENV_FILE": str(path), "JIRA_PAT": "from-env"},
                clear=True,
            ):
                jira_tools._ENV_CACHE.clear()
                self.assertEqual(jira_tools._setting("JIRA_PAT"), "from-env")

    def test_export_prefix_and_quotes_are_stripped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._env_file(
                directory, '# comment\nexport JIRA_PAT="quoted-token"\n\n'
            )
            with mock.patch.dict(
                "os.environ", {"DWS_JIRA_ENV_FILE": str(path)}, clear=True
            ):
                jira_tools._ENV_CACHE.clear()
                self.assertEqual(jira_tools._setting("JIRA_PAT"), "quoted-token")

    def test_unrelated_and_malformed_lines_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._env_file(
                directory, "UNRELATED=x\ngarbage line\nJIRA_PAT=good\n"
            )
            with mock.patch.dict(
                "os.environ", {"DWS_JIRA_ENV_FILE": str(path)}, clear=True
            ):
                jira_tools._ENV_CACHE.clear()
                self.assertEqual(jira_tools._setting("JIRA_PAT"), "good")

    def test_missing_env_file_is_not_an_error(self) -> None:
        with mock.patch.dict(
            "os.environ", {"DWS_JIRA_ENV_FILE": "Z:/no/such/file"}, clear=True
        ):
            jira_tools._ENV_CACHE.clear()
            self.assertEqual(jira_tools._setting("JIRA_PAT"), "")


class PiWiringTests(unittest.TestCase):
    """pi 命令行挂载规则。"""

    def test_jira_extension_is_mounted_by_default(self) -> None:
        # config.json 被 gitignore，开关只放那里会让新部署拿不到能力，
        # 所以默认必须挂载。
        argv = pi_module.command(_Profile(), False)
        self.assertTrue(any("pi_jira.ts" in arg for arg in argv))

    def test_jira_extension_can_be_disabled(self) -> None:
        argv = pi_module.command(_Profile(jira=False), False)
        self.assertFalse(any("pi_jira.ts" in arg for arg in argv))

    def test_jira_tool_joins_an_explicit_allowlist(self) -> None:
        argv = pi_module.command(_Profile(read_only=True, tools="read,grep"), True)
        tools = argv[argv.index("--tools") + 1]
        self.assertIn("jira_issue", tools)

    def test_enabling_jira_does_not_restrict_the_worker_toolset(self) -> None:
        # worker 的 tools 缺省为空串 = 不限制；若这里凭空生成 --tools，
        # read/write/bash 会被全部收走，worker 就废了。
        argv = pi_module.command(_Profile(), False)
        self.assertNotIn("--tools", argv)
        self.assertNotIn("--no-tools", argv)


class PromptGuardTests(unittest.TestCase):
    """状态字段不得被当成部署证据。"""

    PROMPTS = ("prompts/front.md", "prompts/worker.md")

    def _text(self, relative: str) -> str:
        return (Path(__file__).resolve().parents[1] / relative).read_text(
            encoding="utf-8"
        )

    def test_prompts_state_that_status_is_only_a_workflow_field(self) -> None:
        for relative in self.PROMPTS:
            text = self._text(relative)
            with self.subTest(prompt=relative):
                self.assertIn("工作流字段", text)
                # 必须明确否掉「用状态推断是否已上线」。
                self.assertIn("不能拿来确认", text)

    def test_prompts_do_not_exempt_a_published_status(self) -> None:
        # 曾经写成「等于已发布就直接答已上线」，等于又用状态当依据，
        # 和上一条自相矛盾。规则必须对状态取值一视同仁。
        for relative in self.PROMPTS:
            text = self._text(relative)
            with self.subTest(prompt=relative):
                self.assertIn("已发布", text)
                self.assertIn("待发布", text)
                self.assertNotIn("直接答「已上线」", text)

    def test_prompts_forbid_claiming_a_release_from_status(self) -> None:
        for relative in self.PROMPTS:
            text = self._text(relative)
            with self.subTest(prompt=relative):
                self.assertIn("上线", text)
                # 不能只是提一句，要给出「没有记录就如实说没有」的落点。
                self.assertIn("记录", text)

    def test_tool_description_carries_the_same_warning(self) -> None:
        # 模型也会读工具描述，两处口径必须一致。
        text = self._text("src/dws_dm_agent/runners/pi_jira.ts")
        self.assertIn("workflow fields", text)
        self.assertIn("never confirm", text)


class UncertaintyGuardTests(unittest.TestCase):
    """判断不了的事必须在回复里点出来，交给对方或本人定，不能自己拍板。"""

    PROMPTS = ("prompts/front.md", "prompts/worker.md")

    def _text(self, relative: str) -> str:
        return (Path(__file__).resolve().parents[1] / relative).read_text(
            encoding="utf-8"
        )

    def test_prompts_require_flagging_undecidable_points(self) -> None:
        for relative in self.PROMPTS:
            text = self._text(relative)
            with self.subTest(prompt=relative):
                # 要有固定的落地形式，否则模型只会写一句「可能有风险」。
                self.assertIn("需要确认", text)
                self.assertIn("判断不了", text)
                self.assertIn("自己拍板", text)

    def test_prompts_say_what_to_spell_out(self) -> None:
        for relative in self.PROMPTS:
            text = self._text(relative)
            with self.subTest(prompt=relative):
                # 三点必须写清：卡在哪、为什么、要谁做什么。
                self.assertIn("为什么卡住", text)
                self.assertIn("需要谁", text)

    def test_prompts_do_not_invent_doubts_to_look_careful(self) -> None:
        for relative in self.PROMPTS:
            text = self._text(relative)
            with self.subTest(prompt=relative):
                # 只在真有时才写，避免每条回复都挂一段「需要确认」。
                self.assertIn("空造疑虑", text)


if __name__ == "__main__":
    unittest.main()
