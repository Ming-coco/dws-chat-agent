"""项目配置读写的测试。

重点覆盖踩过的坑：
  - Windows 上 os.chmod 不管用，必须用 icacls，而且要用「计算机名\\用户名」，
    只传用户名会授权给一个解析不出来的账户，结果文件谁都读不了
  - 保存时收到掩码表示「用户没改密码」，必须保留原值
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from dws_dm_agent.project_config import (
    MASK,
    DatabaseConfig,
    ProjectConfig,
    ProjectConfigError,
    ProjectStore,
    ServerConfig,
    describe,
)


class ProjectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent.parent / "build" / "test-tmp" / (
            f"projectstore-{os.getpid()}-{id(self)}"
        )
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.store = ProjectStore(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _sample(self, name: str = "项目甲") -> ProjectConfig:
        return ProjectConfig(
            name=name,
            frontend="http://192.0.2.10:8090",
            note="UAT",
            server=ServerConfig(
                host="192.0.2.10", port=22, user="admin",
                password="server-secret", deploy_dir="/opt/app",
            ),
            databases=[
                DatabaseConfig(
                    name="主库", type="mysql", host="192.0.2.20", port=3306,
                    schema="app_db", user="root", password="db-secret",
                ),
            ],
            login_user="analyst",
            login_password="login-secret",
        )

    def test_save_then_load(self) -> None:
        self.store.save(self._sample())
        loaded = self.store.load("项目甲")
        self.assertEqual(loaded.frontends[0].url, "http://192.0.2.10:8090")
        self.assertEqual(loaded.server.host, "192.0.2.10")
        self.assertEqual(len(loaded.databases), 1)
        self.assertEqual(loaded.databases[0].schema, "app_db")

    def test_password_is_masked_on_read(self) -> None:
        self.store.save(self._sample())
        masked = self.store.load("项目甲").to_dict(mask_password=True)
        self.assertEqual(masked["server"]["password"], MASK)
        self.assertEqual(masked["databases"][0]["password"], MASK)

    def test_real_password_reaches_disk(self) -> None:
        # 掩码只用于展示；文件里必须是真值，否则 agent 拿不到密码
        path = self.store.save(self._sample())
        raw = path.read_text(encoding="utf-8")
        self.assertIn("server-secret", raw)
        self.assertIn("db-secret", raw)

    def test_repeated_save_is_allowed(self) -> None:
        # 权限收紧后如果写不进去，改配置就成了一次性的。
        # 实测踩过：icacls 只传用户名导致文件锁死，第二次保存直接 Permission denied。
        self.store.save(self._sample())
        for index in range(3):
            config = self.store.load("项目甲")
            config.frontends[0].url = f"http://192.0.2.10:809{index}"
            self.store.save(config)
        self.assertTrue(self.store.load("项目甲").frontends[0].url.endswith("8092"))

    def test_masked_password_keeps_previous_value(self) -> None:
        self.store.save(self._sample())
        config = self.store.load("项目甲")
        config.server.password = MASK  # 用户没改
        self.store.save(config)
        self.assertEqual(self.store.load("项目甲").server.password, "server-secret")

    def test_new_password_replaces_previous(self) -> None:
        self.store.save(self._sample())
        config = self.store.load("项目甲")
        config.server.password = "new-secret"
        self.store.save(config)
        self.assertEqual(self.store.load("项目甲").server.password, "new-secret")

    def test_unknown_project_returns_blank(self) -> None:
        config = self.store.load("没配过的")
        self.assertEqual(config.name, "没配过的")
        self.assertEqual(config.frontends, [])
        self.assertEqual(config.databases, [])

    def test_illegal_names_are_rejected(self) -> None:
        for bad in ("../etc/passwd", "a/b", "", "  "):
            with self.assertRaises(ProjectConfigError):
                self.store.path_for(bad)

    def test_list_configured(self) -> None:
        self.store.save(self._sample("甲"))
        self.store.save(self._sample("乙"))
        self.assertEqual(sorted(self.store.list_configured()), ["乙", "甲"])

    def test_describe_has_no_password(self) -> None:
        text = describe(self._sample())
        self.assertNotIn("server-secret", text)
        self.assertNotIn("db-secret", text)
        self.assertNotIn("login-secret", text)
        self.assertIn("192.0.2.10", text)
        self.assertIn("app_db", text)
        # 账号名可以出现，密码不行
        self.assertIn("analyst", text)

    def test_multiple_databases_round_trip(self) -> None:
        config = self._sample()
        config.databases.append(
            DatabaseConfig(
                name="ClickHouse", type="clickhouse", host="192.0.2.21",
                port=8123, schema="chain", user="default", password="",
            )
        )
        self.store.save(config)
        loaded = self.store.load("项目甲")
        self.assertEqual(len(loaded.databases), 2)
        self.assertEqual(loaded.databases[1].type, "clickhouse")

    def test_login_credentials_round_trip(self) -> None:
        self.store.save(self._sample())
        raw = json.loads((self.root / "项目甲.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["login_user"], "analyst")
        self.assertEqual(raw["login_password"], "login-secret")
        self.assertEqual(raw["server"]["deployDir"], "/opt/app")
        # 旧的 e2e 字段不该再出现
        self.assertNotIn("e2e", raw)

    def test_login_password_is_masked(self) -> None:
        self.store.save(self._sample())
        masked = self.store.load("项目甲").to_dict(mask_password=True)
        self.assertEqual(masked["login_password"], MASK)

    def test_masked_login_password_keeps_previous(self) -> None:
        self.store.save(self._sample())
        config = self.store.load("项目甲")
        config.login_password = MASK
        self.store.save(config)
        self.assertEqual(self.store.load("项目甲").login_password, "login-secret")


if __name__ == "__main__":
    unittest.main()
