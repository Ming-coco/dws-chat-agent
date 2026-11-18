"""e2e 工具的 agent 桥接测试。

重点覆盖踩过的坑：
  - 结果转换不能被「error 键存在但为空」短路掉
  - 只回问题页，不回全部页面
  - full 模式必须拒绝
"""

from __future__ import annotations

import unittest

from dws_dm_agent.runners import e2e_tools


class RunPayloadShapeTests(unittest.TestCase):
    """_run 的输出形状。用假 CLI 输出驱动，不真的开浏览器。"""

    def setUp(self) -> None:
        self._original = e2e_tools._invoke

    def tearDown(self) -> None:
        e2e_tools._invoke = self._original  # type: ignore[assignment]

    def _fake(self, payload: dict) -> None:
        e2e_tools._invoke = lambda command: payload  # type: ignore[assignment]

    def test_converts_cli_output_into_problem_list(self) -> None:
        # CLI 正常输出里 error 是空串。曾经的 bug：用 `"error" in payload`
        # 判断失败，导致转换逻辑永远走不到，把完整 pages 直接透给模型。
        self._fake(
            {
                "overall": "WARN",
                "error": "",
                "durationMs": 96000,
                "menuSource": "https://x/api/menu",
                "summary": {"total": 3, "passed": 2, "warned": 1, "failed": 0},
                "pages": [
                    {"name": "A", "level": "PASS", "reason": "正常", "layer": "", "details": []},
                    {"name": "B", "level": "PASS", "reason": "正常", "layer": "", "details": []},
                    {
                        "name": "C",
                        "level": "WARN",
                        "reason": "页面显示空数据",
                        "layer": "unknown",
                        "details": ["d1", "d2", "d3"],
                    },
                ],
            }
        )

        result = e2e_tools._run({"config": "demo"})

        self.assertNotIn("pages", result)
        self.assertEqual(result["overall"], "WARN")
        self.assertEqual(len(result["problems"]), 1)
        self.assertEqual(result["problems"][0]["page"], "C")
        # details 只留前两条，避免把长错误灌给模型
        self.assertEqual(result["problems"][0]["details"], ["d1", "d2"])
        self.assertEqual(result["passedPages"], ["A", "B"])

    def test_real_error_is_returned_as_is(self) -> None:
        self._fake({"error": "登录失败: 账号密码不对"})
        result = e2e_tools._run({"config": "demo"})
        self.assertEqual(result["error"], "登录失败: 账号密码不对")

    def test_full_mode_is_refused(self) -> None:
        # full 会填表单、点查询，可能产生业务数据，不能由 agent 自动触发
        called = {"count": 0}

        def never(command):  # noqa: ANN001
            called["count"] += 1
            return {}

        e2e_tools._invoke = never  # type: ignore[assignment]
        result = e2e_tools._run({"config": "demo", "mode": "full"})

        self.assertIn("error", result)
        self.assertIn("smoke", result["error"])
        self.assertEqual(called["count"], 0, "拒绝时不应该真的去跑测试")

    def test_missing_config_is_reported(self) -> None:
        result = e2e_tools._run({})
        self.assertIn("config", result["error"])


class DiscoverPayloadShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original = e2e_tools._invoke

    def tearDown(self) -> None:
        e2e_tools._invoke = self._original  # type: ignore[assignment]

    def test_ok_result_lists_only_leaf_pages(self) -> None:
        # 容器菜单（营销）不是页面，不该出现在可测页面列表里
        e2e_tools._invoke = lambda command: {  # type: ignore[assignment]
            "status": "ok",
            "message": "ok",
            "menuCount": 1,
            "menus": [
                {
                    "name": "营销",
                    "kind": "menu",
                    "fullPath": "/mkt",
                    "children": [
                        {"name": "标签筛客", "kind": "menu", "fullPath": "/mkt/label", "children": []},
                        {"name": "导出", "kind": "button", "fullPath": "", "children": []},
                    ],
                }
            ],
        }

        result = e2e_tools._discover({"config": "demo"})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["menus"], ["营销 / 标签筛客"])

    def test_needs_confirmation_exposes_candidates(self) -> None:
        e2e_tools._invoke = lambda command: {  # type: ignore[assignment]
            "status": "needs_confirmation",
            "message": "两个候选得分接近",
            "menuCount": 0,
            "suggested": 0,
            "candidates": [
                {"label": "api/a", "topCount": 10, "urlCount": 30, "sample": ["首页"]},
                {"label": "api/b", "topCount": 8, "urlCount": 25, "sample": ["甲"]},
            ],
        }

        result = e2e_tools._discover({"config": "demo"})

        self.assertTrue(result["needsHuman"])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertNotIn("menus", result)


class ConfigListingTests(unittest.TestCase):
    def test_example_config_is_not_listed(self) -> None:
        # example-webapp.json 是模板，不是可测系统
        result = e2e_tools._list_configs()
        ids = [item["id"] for item in result["configs"]]
        self.assertNotIn("example-webapp", ids)


if __name__ == "__main__":
    unittest.main()
