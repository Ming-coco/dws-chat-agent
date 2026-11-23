"""E2E 桥接层的测试。

改造后的语义：工具参数是「项目名」，地址和账号从 state/projects/<项目>.json 读。
覆盖点：
  - 项目列表只返回配了前端地址的
  - 缺 project 时把可选项目列出来
  - 项目没配过时明确报错，不猜
  - full 模式一律拒绝
  - CLI 输出被抽成问题页列表，通过的页面只留名字
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dws_dm_agent.runners import e2e_tools
from dws_dm_agent.project_config import ProjectConfig, ProjectStore, ServerConfig


class ProjectBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / "build" / "test-tmp" / (
            f"e2e-bridge-{os.getpid()}-{id(self)}"
        )
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.store = ProjectStore(self.root / "projects")
        self._env = mock.patch.dict(
            os.environ, {"DWS_STATE_DIR": str(self.root)}, clear=False
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def _configure(self, name: str, frontend: str = "http://192.0.2.10:8090") -> None:
        self.store.save(
            ProjectConfig(
                name=name,
                frontend=frontend,
                login_user="tester",
                login_password="secret",
                server=ServerConfig(host="192.0.2.10"),
            )
        )

    def test_lists_only_projects_with_frontend(self) -> None:
        self._configure("项目甲")
        # 没有前端地址的项目不该出现在测试列表里
        self.store.save(ProjectConfig(name="只有服务器", server=ServerConfig(host="x")))

        result = e2e_tools._list_projects()
        names = [item["project"] for item in result["projects"]]
        self.assertEqual(names, ["项目甲"])
        self.assertEqual(result["projects"][0]["frontend"], "http://192.0.2.10:8090")

    def test_empty_list_gives_actionable_hint(self) -> None:
        result = e2e_tools._list_projects()
        self.assertEqual(result["projects"], [])
        self.assertIn("项目配置", result["hint"])

    def test_missing_project_lists_candidates(self) -> None:
        self._configure("项目甲")
        self._configure("项目乙")
        result = e2e_tools._missing_project()
        self.assertEqual(result["error"], "missing project")
        self.assertEqual(sorted(result["available"]), ["项目乙", "项目甲"])
        # 多个项目时必须问，不能自作主张
        self.assertIn("问清楚", result["hint"])

    def test_single_project_hint_names_it(self) -> None:
        self._configure("项目甲")
        result = e2e_tools._missing_project()
        self.assertIn("项目甲", result["hint"])

    def test_no_projects_asks_for_configuration(self) -> None:
        result = e2e_tools._missing_project()
        self.assertIn("还没有项目配置", result["error"])

    def test_unknown_project_is_reported(self) -> None:
        self._configure("项目甲")
        result = e2e_tools._resolve_project("随便一个")
        self.assertIsInstance(result, dict)
        self.assertIn("还没有配置", result["error"])
        self.assertEqual(result["configured"], ["项目甲"])

    def test_project_without_frontend_is_rejected(self) -> None:
        self.store.save(ProjectConfig(name="空项目"))
        result = e2e_tools._resolve_project("空项目")
        self.assertIsInstance(result, dict)
        self.assertIn("没填前端地址", result["error"])

    def test_project_without_login_is_rejected(self) -> None:
        self.store.save(ProjectConfig(name="无账号", frontend="http://x"))
        result = e2e_tools._resolve_project("无账号")
        self.assertIsInstance(result, dict)
        self.assertIn("登录账号", result["error"])

    def test_resolve_builds_config_and_env(self) -> None:
        self._configure("项目甲")
        resolved = e2e_tools._resolve_project("项目甲")
        self.assertIsInstance(resolved, tuple)
        config_path, environment = resolved

        # 账号密码走环境变量，配置文件里只留变量名
        text = config_path.read_text(encoding="utf-8")
        self.assertIn("${E2E_USER}", text)
        self.assertNotIn("secret", text)
        self.assertEqual(environment["E2E_BASE_URL"], "http://192.0.2.10:8090")
        self.assertEqual(environment["E2E_USER"], "tester")
        self.assertEqual(environment["E2E_PASS"], "secret")

    def test_full_mode_is_refused(self) -> None:
        self._configure("项目甲")
        result = e2e_tools._run({"project": "项目甲", "mode": "full"})
        self.assertIn("only smoke", result["error"])

    def test_run_without_project_does_not_start_browser(self) -> None:
        self._configure("项目甲")
        result = e2e_tools._run({})
        self.assertEqual(result["error"], "missing project")


class RunPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / "build" / "test-tmp" / (
            f"e2e-run-{os.getpid()}-{id(self)}"
        )
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.store = ProjectStore(self.root / "projects")
        self.store.save(
            ProjectConfig(
                name="项目甲",
                frontend="http://192.0.2.10:8090",
                login_user="tester",
                login_password="secret",
            )
        )
        self._env = mock.patch.dict(
            os.environ, {"DWS_STATE_DIR": str(self.root)}, clear=False
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_problems_are_listed_and_passes_summarised(self) -> None:
        cli_output = {
            "overall": "FAIL",
            "menuSource": "api",
            "durationMs": 120000,
            "error": "",
            "summary": {"total": 3, "pass": 1, "warn": 1, "fail": 1},
            "pages": [
                {"name": "首页", "level": "PASS"},
                {"name": "风控/预警", "level": "WARN", "layer": "data",
                 "reason": "页面显示空数据", "details": ["a", "b", "c"]},
                {"name": "外贸/查询", "level": "FAIL", "layer": "render",
                 "reason": "渲染失败", "details": []},
            ],
        }
        with mock.patch.object(e2e_tools, "_invoke", return_value=cli_output):
            result = e2e_tools._run({"project": "项目甲"})

        self.assertEqual(result["project"], "项目甲")
        self.assertEqual(result["overall"], "FAIL")
        self.assertEqual(len(result["problems"]), 2)
        self.assertEqual(result["problems"][0]["page"], "风控/预警")
        # details 只留两条，避免把整页 DOM 塞给模型
        self.assertEqual(len(result["problems"][0]["details"]), 2)
        self.assertEqual(result["passedPages"], ["首页"])

    def test_empty_string_error_is_not_treated_as_failure(self) -> None:
        # CLI 正常输出里带 "error": ""，不能当成失败
        cli_output = {
            "overall": "PASS", "error": "", "summary": {}, "pages": [],
        }
        with mock.patch.object(e2e_tools, "_invoke", return_value=cli_output):
            result = e2e_tools._run({"project": "项目甲"})
        self.assertEqual(result["overall"], "PASS")

    def test_real_error_is_passed_through(self) -> None:
        with mock.patch.object(
            e2e_tools, "_invoke", return_value={"error": "登录失败"}
        ):
            result = e2e_tools._run({"project": "项目甲"})
        self.assertEqual(result["error"], "登录失败")
        self.assertEqual(result["project"], "项目甲")


class MenuFlattenTests(unittest.TestCase):
    def test_only_leaf_pages_are_listed(self) -> None:
        nodes = [
            {
                "name": "营销",
                "kind": "menu",
                "fullPath": "/market",
                "children": [
                    {"name": "列表", "kind": "menu", "fullPath": "/market/list", "children": []},
                ],
            },
        ]
        names = e2e_tools._flatten_menu_names(nodes)
        # 容器菜单不是页面，只列叶子
        self.assertEqual(names, ["营销 / 列表"])


if __name__ == "__main__":
    unittest.main()
