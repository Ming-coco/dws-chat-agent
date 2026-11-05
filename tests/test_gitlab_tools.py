"""GitLab 只读能力（gitlab_tools.py + pi_gitlab.ts）的回归测试。

覆盖四件事：
1. 项目标识要能吃下 group/project、网页地址、数字 id 三种写法；
2. 失败作业日志必须过滤掉下载进度和 runner 元数据，否则真因会被刷出窗口；
3. 凭据只走环境变量或部署目录 .env，缺令牌时给出可操作的中文原因；
4. pi 命令行默认挂载 pi_gitlab.ts，且不得把 worker 的全量工具误收窄成白名单。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dws_dm_agent.runners import gitlab_tools
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


class ProjectReferenceTests(unittest.TestCase):
    def test_plain_path_is_encoded(self) -> None:
        self.assertEqual(
            gitlab_tools.project_reference("xk/product/zhike/ys-zk-product"),
            "xk%2Fproduct%2Fzhike%2Fys-zk-product",
        )

    def test_web_url_is_reduced_to_path(self) -> None:
        self.assertEqual(
            gitlab_tools.project_reference(
                "https://git.youshu.com.cn/xk/product/zhike/ys-zk-product"
            ),
            "xk%2Fproduct%2Fzhike%2Fys-zk-product",
        )

    def test_blob_url_is_cut_at_marker(self) -> None:
        self.assertEqual(
            gitlab_tools.project_reference(
                "https://git.youshu.com.cn/xk/product/zhike/ys-zk-product/-/blob/dev/README.md"
            ),
            "xk%2Fproduct%2Fzhike%2Fys-zk-product",
        )

    def test_numeric_id_passes_through(self) -> None:
        self.assertEqual(gitlab_tools.project_reference(2509), "2509")

    def test_dot_git_suffix_is_stripped(self) -> None:
        self.assertEqual(
            gitlab_tools.project_reference("xk/product/zhike/ys-zk-product.git"),
            "xk%2Fproduct%2Fzhike%2Fys-zk-product",
        )

    def test_missing_project_is_rejected(self) -> None:
        with self.assertRaises(gitlab_tools.GitlabError):
            gitlab_tools.project_reference("   ")

    def test_reference_without_namespace_is_rejected(self) -> None:
        with self.assertRaises(gitlab_tools.GitlabError):
            gitlab_tools.project_reference("just-a-name")


class LogNoiseTests(unittest.TestCase):
    def test_download_progress_is_dropped(self) -> None:
        raw = "\n".join(
            [
                "Progress (4): 238 kB | 5.0 kB",
                "Downloaded from hsjdata: https://maven/x.jar (238 kB at 55 kB/s)",
                "[ERROR] Could not find artifact com.x:y:jar:1.0",
            ]
        )
        with mock.patch.object(gitlab_tools, "_request_text", return_value=raw):
            text = gitlab_tools._trace_tail(1, 2)
        self.assertIn("Could not find artifact", text)
        self.assertNotIn("Progress (", text)
        self.assertNotIn("Downloaded from", text)

    def test_runner_metadata_is_dropped(self) -> None:
        raw = "\n".join(
            [
                "section_end:1793677937:step_script",
                "Cleaning up project directory and file based variables",
                "ERROR: Job failed: exit code 1",
            ]
        )
        with mock.patch.object(gitlab_tools, "_request_text", return_value=raw):
            text = gitlab_tools._trace_tail(1, 2)
        self.assertIn("Job failed", text)
        self.assertNotIn("section_end", text)
        self.assertNotIn("Cleaning up", text)

    def test_all_noise_falls_back_to_raw_lines(self) -> None:
        # 全是噪音时宁可给噪声也不能给空，否则模型会以为没日志。
        raw = "Progress (1): 1 kB\nProgress (1): 2 kB"
        with mock.patch.object(gitlab_tools, "_request_text", return_value=raw):
            text = gitlab_tools._trace_tail(1, 2)
        self.assertIn("Progress", text)

    def test_ansi_is_stripped(self) -> None:
        raw = "\x1b[31mERROR\x1b[0m boom"
        with mock.patch.object(gitlab_tools, "_request_text", return_value=raw):
            text = gitlab_tools._trace_tail(1, 2)
        self.assertIn("ERROR boom", text)
        self.assertNotIn("\x1b", text)


class SettingTests(unittest.TestCase):
    def setUp(self) -> None:
        gitlab_tools._ENV_CACHE.clear()

    def test_env_var_wins_over_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("GITLAB_TOKEN=from-file\n", encoding="utf-8")
            with mock.patch.dict(
                gitlab_tools.os.environ,
                {
                    "GITLAB_TOKEN": "from-env",
                    "DWS_GITLAB_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                self.assertEqual(gitlab_tools._setting("GITLAB_TOKEN"), "from-env")

    def test_generic_env_file_variable_also_works(self) -> None:
        # 后续新增能力共用 DWS_AGENT_ENV_FILE，部署目录不必再配专用变量。
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("GITLAB_TOKEN=shared\n", encoding="utf-8")
            with mock.patch.dict(gitlab_tools.os.environ, {}, clear=True):
                gitlab_tools.os.environ["DWS_AGENT_ENV_FILE"] = str(env_file)
                self.assertEqual(gitlab_tools._setting("GITLAB_TOKEN"), "shared")

    def test_bom_and_quotes_are_handled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_bytes(
                "\ufeffexport GITLAB_TOKEN='quoted-token'\n".encode("utf-8")
            )
            values = gitlab_tools._read_env_file(str(env_file))
        self.assertEqual(values["GITLAB_TOKEN"], "quoted-token")

    def test_missing_token_explains_what_to_do(self) -> None:
        with mock.patch.dict(gitlab_tools.os.environ, {}, clear=True):
            with self.assertRaises(gitlab_tools.GitlabError) as ctx:
                gitlab_tools._token()
        message = str(ctx.exception)
        self.assertIn("GITLAB_TOKEN", message)
        self.assertIn(".env", message)


class RenderTests(unittest.TestCase):
    def _project(self) -> dict:
        return {
            "id": 2509,
            "path_with_namespace": "xk/product/zhike/ys-zk-product",
            "default_branch": "dev",
            "web_url": "https://git.youshu.com.cn/xk/product/zhike/ys-zk-product",
            "permissions": {"project_access": None, "group_access": {"access_level": 30}},
        }

    def test_project_shows_level_and_protected_branches(self) -> None:
        branches = [
            {
                "name": "dev",
                "merge_access_levels": [{"access_level": 30}],
                "push_access_levels": [{"access_level": 40}],
            }
        ]
        pipelines = [
            {"iid": 9206, "ref": "dev", "status": "success", "sha": "ed35af1c99", "created_at": "2026-11-04T17:13:40.000+08:00"}
        ]

        def fake_json(path: str):
            if "protected_branches" in path:
                return branches
            if "pipelines?" in path:
                return pipelines
            return self._project()

        with mock.patch.object(gitlab_tools, "_request_json", side_effect=fake_json):
            text = gitlab_tools.describe_project("xk/product/zhike/ys-zk-product")
        self.assertIn("开发者", text)
        self.assertIn("dev", text)
        self.assertIn("推送=维护者", text)
        self.assertIn("#9206", text)
        self.assertIn("成功(success)", text)

    def test_pipeline_without_failure_says_so(self) -> None:
        def fake_json(path: str):
            if "/pipelines/295433/jobs" in path:
                return [{"id": 1, "status": "success", "name": "build"}]
            if "pipelines?" in path:
                return [{"id": 295433, "iid": 9206, "ref": "dev", "status": "success", "sha": "ed35af1c99"}]
            return self._project()

        with mock.patch.object(gitlab_tools, "_request_json", side_effect=fake_json):
            text = gitlab_tools.describe_pipeline("xk/product/zhike/ys-zk-product", 9206)
        self.assertIn("失败作业：无", text)

    def test_pipeline_lookup_by_iid_searches_list(self) -> None:
        # /pipelines/<n> 要的是全局 id，用户给的是 iid，必须先在列表里找。
        seen: list[str] = []

        def fake_json(path: str):
            seen.append(path)
            if "pipelines?per_page=100" in path:
                return [{"id": 111, "iid": 9205, "ref": "dev-jm-2.0", "status": "failed"}]
            if "/jobs" in path:
                return []
            return self._project()

        with mock.patch.object(gitlab_tools, "_request_json", side_effect=fake_json):
            text = gitlab_tools.describe_pipeline("xk/product/zhike/ys-zk-product", 9205)
        self.assertIn("dev-jm-2.0", text)
        self.assertTrue(any("pipelines?per_page=100" in path for path in seen))
        self.assertFalse(any("/pipelines/9205" in path for path in seen))

    def test_unknown_pipeline_number_is_reported(self) -> None:
        def fake_json(path: str):
            if "pipelines?per_page=100" in path:
                return [{"id": 1, "iid": 2}]
            return self._project()

        with mock.patch.object(gitlab_tools, "_request_json", side_effect=fake_json):
            with self.assertRaises(gitlab_tools.GitlabError) as ctx:
                gitlab_tools.describe_pipeline("p/q", 999)
        self.assertIn("999", str(ctx.exception))


class GroupTests(unittest.TestCase):
    """群组查询：网页上很多「仓库链接」其实指向群组而不是项目。"""

    GROUP = {
        "id": 52,
        "full_path": "xk/project",
        "path": "project",
        "name": "project",
        "visibility": "internal",
        "description": "所有项目都放在这个目录中",
        "web_url": "https://git.example.com/groups/xk/project",
    }

    def test_group_reference_accepts_web_url(self) -> None:
        self.assertEqual(
            gitlab_tools.group_reference("https://git.example.com/xk/project"),
            gitlab_tools.group_reference("xk/project"),
        )

    def test_group_reference_rejects_bare_name(self) -> None:
        # 短名有歧义，宁可报错让人补全，也不要猜一个群组出来。
        with self.assertRaises(gitlab_tools.GitlabError) as ctx:
            gitlab_tools.group_reference("project")
        self.assertIn("群组", str(ctx.exception))

    def _fake(self, *, projects=None, subgroups=None, totals=(0, 0)):
        projects = projects if projects is not None else []
        subgroups = subgroups if subgroups is not None else []

        def fake_json(path: str):
            if path.startswith("/api/v4/groups/") and "/projects" not in path and "/subgroups" not in path:
                return self.GROUP
            if "/subgroups" in path:
                return subgroups
            return projects

        return fake_json

    def _fake_get_with_total(self, projects, subgroups, totals):
        """让 _get_with_total 返回固定数据与总数。"""

        def fake(path: str):
            if "/subgroups" in path:
                return subgroups, len(subgroups)
            recursive = "include_subgroups=true" in path
            total = totals[1] if recursive else totals[0]
            return projects, total

        return fake

    def test_describe_group_lists_projects_and_total(self) -> None:
        projects = [
            {
                "path_with_namespace": "xk/project/cqrcb/zhike-cust-backend-cqrcb",
                "default_branch": "dev",
                "last_activity_at": "2026-11-04T22:26:00.000+08:00",
            }
        ]
        subgroups = [{"full_path": "xk/project/cqrcb"}]
        with mock.patch.object(gitlab_tools, "_request_json", side_effect=self._fake()):
            with mock.patch.object(
                gitlab_tools,
                "_get_with_total",
                side_effect=self._fake_get_with_total(projects, subgroups, (24, 426)),
            ):
                text = gitlab_tools.describe_group("xk/project")
        self.assertIn("xk/project", text)
        self.assertIn("子群组（1）", text)
        self.assertIn("直属项目（共 24", text)
        self.assertIn("xk/project/cqrcb/zhike-cust-backend-cqrcb", text)
        self.assertIn("含子群组合计项目：426", text)
        self.assertIn("2026-11-04 22:26", text)

    def test_describe_group_says_when_empty(self) -> None:
        with mock.patch.object(gitlab_tools, "_request_json", side_effect=self._fake()):
            with mock.patch.object(
                gitlab_tools,
                "_get_with_total",
                side_effect=self._fake_get_with_total([], [], (0, 0)),
            ):
                text = gitlab_tools.describe_group("xk/project")
        self.assertIn("子群组：无", text)
        self.assertIn("直属项目：无", text)

    def test_group_query_is_dispatched(self) -> None:
        with mock.patch.object(gitlab_tools, "describe_group", return_value="ok") as spy:
            result = gitlab_tools.handle(
                {"tool": "gitlab_group", "args": {"group": "xk/project"}}
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "ok")
        spy.assert_called_once()

    def test_grouping_a_group_as_project_explains_the_difference(self) -> None:
        """把群组当项目查时，不能只说「不存在」，要说清它是群组。

        之前锦洋发来 xk/project，工具回「项目不存在或看不到」，
        实际它是个装了 426 个项目的群组——提示必须点出这一点。
        """

        def fake_json(path: str):
            if path.startswith("/api/v4/projects/"):
                raise gitlab_tools.GitlabNotFound("不存在")
            return self.GROUP

        with mock.patch.object(gitlab_tools, "_request_json", side_effect=fake_json):
            with self.assertRaises(gitlab_tools.GitlabError) as ctx:
                gitlab_tools.describe_project("xk/project")
        message = str(ctx.exception)
        self.assertIn("群组", message)
        self.assertIn("gitlab_group", message)

    def test_missing_project_stays_a_plain_error_when_no_group_matches(self) -> None:
        def fake_json(path: str):
            raise gitlab_tools.GitlabNotFound("不存在")

        with mock.patch.object(gitlab_tools, "_request_json", side_effect=fake_json):
            with self.assertRaises(gitlab_tools.GitlabNotFound):
                gitlab_tools.describe_project("p/q")


class ImageTests(unittest.TestCase):
    """镜像名只能从 kaniko 日志里读，不能靠命名规则拼。"""

    def test_pushing_image_line_is_parsed(self) -> None:
        trace = (
            "INFO[0031] Pushing image to harbor.yscredit.com/proj/app:1.3.13\n"
            "INFO[0032] Pushed image to 1 destinations\n"
        )
        self.assertEqual(
            gitlab_tools._image_refs(trace),
            ["harbor.yscredit.com/proj/app:1.3.13"],
        )

    def test_ansi_colour_codes_do_not_hide_the_line(self) -> None:
        trace = "\x1b[36mINFO\x1b[0m[0025] Pushing image to reg.example.com/a/b:2.0.0\n"
        cleaned = gitlab_tools._ANSI_RE.sub("", trace)
        self.assertEqual(
            gitlab_tools._image_refs(cleaned), ["reg.example.com/a/b:2.0.0"]
        )

    def test_duplicates_are_collapsed(self) -> None:
        trace = (
            "Pushing image to reg/a:1\n"
            "Pushing image to reg/a:1\n"
            "Pushing image to reg/b:1\n"
        )
        self.assertEqual(gitlab_tools._image_refs(trace), ["reg/a:1", "reg/b:1"])

    def test_missing_push_line_yields_nothing(self) -> None:
        # 没走到推送时不能编一个名字出来。
        self.assertEqual(gitlab_tools._image_refs("ERROR: Job failed: exit code 1"), [])

    def test_images_are_reported_before_failed_jobs(self) -> None:
        """问「这个 tag 出了什么镜像」时，镜像必须出现在失败作业之前。

        只看日志尾部会把 Pushing image to 切掉——它出现在构建中段，
        所以镜像要从完整日志里取。
        """
        pipeline = {
            "id": 900,
            "iid": 155,
            "ref": "1.3.13",
            "status": "failed",
            "sha": "a4d8ab5c" * 5,
            "created_at": "2026-11-05T13:45:00.000+08:00",
        }
        jobs = [
            {"id": 1, "name": "docker_build_main", "stage": "docker", "status": "success"},
            {"id": 2, "name": "docker_build_main_arm", "stage": "docker", "status": "failed"},
        ]

        def fake_json(path: str):
            # 作业列表的路径同样以 /api/v4/projects/ 开头，必须先判它。
            if "/pipelines/900/jobs" in path:
                return jobs
            if "/protected_branches" in path or "/pipelines" in path:
                return []
            return {"id": 7, "path_with_namespace": "p/q", "default_branch": "dev"}

        def fake_text(path: str):
            if "/jobs/1/trace" in path:
                return "Pushing image to harbor.example.com/p/q-main:1.3.13\n"
            return "ERROR: arm build needs BUILD_REGISTRY_PROJECT\n"

        with mock.patch.object(gitlab_tools, "_request_json", side_effect=fake_json):
            with mock.patch.object(gitlab_tools, "_pipeline_of", return_value=pipeline):
                with mock.patch.object(gitlab_tools, "_request_text", side_effect=fake_text):
                    text = gitlab_tools.describe_pipeline("p/q", 155)

        image_at = text.index("harbor.example.com/p/q-main:1.3.13")
        failed_at = text.index("失败作业")
        self.assertLess(image_at, failed_at)
        # 失败的 arm 作业没推成，不能给出镜像名。
        self.assertNotIn("q-main-arm", text)


class HandleTests(unittest.TestCase):
    def test_missing_token_is_returned_not_raised(self) -> None:
        # 只缺令牌时应准确指出缺的是令牌，而不是笼统地说配置不对。
        environment = {"GITLAB_BASE_URL": "https://git.example.com"}
        with mock.patch.dict(gitlab_tools.os.environ, environment, clear=True):
            result = gitlab_tools.handle(
                {"tool": "gitlab_project", "args": {"project": "p/q"}}
            )
        self.assertFalse(result["ok"])
        self.assertIn("GITLAB_TOKEN", result["error"])

    def test_missing_base_url_is_reported_separately(self) -> None:
        environment = {"GITLAB_TOKEN": "t"}
        with mock.patch.dict(gitlab_tools.os.environ, environment, clear=True):
            result = gitlab_tools.handle(
                {"tool": "gitlab_project", "args": {"project": "p/q"}}
            )
        self.assertFalse(result["ok"])
        self.assertIn("GITLAB_BASE_URL", result["error"])

    def test_unknown_tool_is_reported(self) -> None:
        result = gitlab_tools.handle({"tool": "gitlab_merge", "args": {}})
        self.assertFalse(result["ok"])
        self.assertIn("gitlab_merge", result["error"])

    def test_non_object_request_is_reported(self) -> None:
        self.assertFalse(gitlab_tools.handle(["nope"])["ok"])


class PiWiringTests(unittest.TestCase):
    def test_gitlab_extension_is_mounted_by_default(self) -> None:
        argv = pi_module.command(_Profile(), False)
        self.assertTrue(any("pi_gitlab.ts" in arg for arg in argv))

    def test_gitlab_extension_can_be_disabled(self) -> None:
        argv = pi_module.command(_Profile(gitlab=False), False)
        self.assertFalse(any("pi_gitlab.ts" in arg for arg in argv))

    def test_gitlab_tools_join_an_explicit_allowlist(self) -> None:
        argv = pi_module.command(_Profile(read_only=True, tools="read,grep"), True)
        tools = argv[argv.index("--tools") + 1]
        self.assertIn("gitlab_project", tools)
        self.assertIn("gitlab_pipeline", tools)
        self.assertIn("gitlab_group", tools)

    def test_gitlab_does_not_restrict_the_worker_toolset(self) -> None:
        # worker 的 tools 缺省为空串 = 不限制；凭空生成 --tools 会把
        # read/write/bash 全部收走。
        argv = pi_module.command(_Profile(), False)
        self.assertNotIn("--tools", argv)
        self.assertNotIn("--no-tools", argv)


if __name__ == "__main__":
    unittest.main()
