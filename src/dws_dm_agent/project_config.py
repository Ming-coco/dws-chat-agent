"""项目配置的读写。

每个项目一个独立文件，放在 state/projects/<项目名>.json。
存的是「agent 执行任务时需要知道的连接信息」：服务器、数据库、前端地址。

密码处理（重要）：
  - 文件权限 0600，只有当前用户能读
  - 对外返回时密码替换成掩码，不回传明文
  - 保存时如果收到掩码，说明用户没改密码，保留原值

这样即使页面被截图、或者 agent 把配置打出来，看到的也只是掩码。
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 前端回显用的掩码。收到这个值表示「密码没改」。
MASK = "********"

# 项目名不能带路径分隔符，避免写到目录外面去
_SAFE_NAME = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff .\-]{0,60}$")


class ProjectConfigError(ValueError):
    """项目配置不合法。"""


@dataclass
class DatabaseConfig:
    name: str = ""
    type: str = "mysql"
    host: str = ""
    port: int = 3306
    schema: str = ""
    user: str = ""
    password: str = ""


@dataclass
class ServerConfig:
    host: str = ""
    port: int = 22
    user: str = ""
    password: str = ""
    deploy_dir: str = ""


@dataclass
class ProjectConfig:
    name: str = ""
    frontend: str = ""
    note: str = ""
    # 登录被测系统的账号。密码和服务器密码一样只回传掩码。
    login_user: str = ""
    login_password: str = ""
    server: ServerConfig = field(default_factory=ServerConfig)
    databases: list[DatabaseConfig] = field(default_factory=list)

    def to_dict(self, *, mask_password: bool = True) -> dict[str, Any]:
        """转成前端直接可用的形状。

        键名用驼峰：界面上的 deployDir / e2e 与这里一一对应，
        免得读回来的配置填不进表单（下划线和驼峰对不上，值一直是空）。
        """
        data = asdict(self)
        server = data.get("server") or {}
        server["deployDir"] = server.pop("deploy_dir", "")
        data.pop("e2e_config", None)
        if mask_password and data.get("login_password"):
            data["login_password"] = MASK
        if mask_password:
            if server.get("password"):
                server["password"] = MASK
            for database in data.get("databases") or []:
                if database.get("password"):
                    database["password"] = MASK
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectConfig":
        server_raw = data.get("server") or {}
        databases_raw = data.get("databases") or []
        return cls(
            name=str(data.get("name") or ""),
            frontend=str(data.get("frontend") or ""),
            note=str(data.get("note") or ""),
            login_user=str(data.get("login_user") or ""),
            login_password=str(data.get("login_password") or ""),
            server=ServerConfig(
                host=str(server_raw.get("host") or ""),
                port=_to_int(server_raw.get("port"), 22),
                user=str(server_raw.get("user") or ""),
                password=str(server_raw.get("password") or ""),
                deploy_dir=str(server_raw.get("deployDir") or server_raw.get("deploy_dir") or ""),
            ),
            databases=[
                DatabaseConfig(
                    name=str(item.get("name") or ""),
                    type=str(item.get("type") or "mysql"),
                    host=str(item.get("host") or ""),
                    port=_to_int(item.get("port"), 3306),
                    schema=str(item.get("schema") or ""),
                    user=str(item.get("user") or ""),
                    password=str(item.get("password") or ""),
                )
                for item in databases_raw
                if isinstance(item, dict)
            ],
        )


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ProjectStore:
    """项目配置存储。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, name: str) -> Path:
        if not _SAFE_NAME.match(name or ""):
            raise ProjectConfigError(f"项目名不合法: {name!r}")
        return self.root / f"{name}.json"

    def exists(self, name: str) -> bool:
        try:
            return self.path_for(name).is_file()
        except ProjectConfigError:
            return False

    def list_configured(self) -> list[str]:
        """已经有配置文件的项目名。"""
        names: list[str] = []
        for path in sorted(self.root.glob("*.json")):
            names.append(path.stem)
        return names

    def load(self, name: str) -> ProjectConfig:
        path = self.path_for(name)
        if not path.is_file():
            # 没配过就返回一个空壳，界面上直接填
            return ProjectConfig(name=name)
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise ProjectConfigError(f"读取失败 {path.name}: {error}") from error
        config = ProjectConfig.from_dict(data)
        config.name = name
        return config

    def save(self, config: ProjectConfig) -> Path:
        if not config.name:
            raise ProjectConfigError("缺少项目名")
        path = self.path_for(config.name)

        # 掩码表示用户没改密码，沿用文件里已有的值
        previous = self.load(config.name)
        if config.server.password == MASK:
            config.server.password = previous.server.password
        if config.login_password == MASK:
            config.login_password = previous.login_password
        for index, database in enumerate(config.databases):
            if database.password != MASK:
                continue
            if index < len(previous.databases):
                database.password = previous.databases[index].password

        payload = config.to_dict(mask_password=False)

        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _restrict_to_owner(path)
        return path


def _restrict_to_owner(path: Path) -> None:
    """把文件限制成只有当前用户能读写。

    文件里有明文密码，权限没管住等于没做隔离。

    Windows 上 os.chmod 只动只读位，真正的访问控制要走 ACL：
    去掉继承、清空已有授权、只给当前用户完全控制。
    Linux 上 chmod 600 就够了。
    """
    if os.name == "nt":
        # 必须是「计算机名\用户名」的完整形式。
        # 只传 USERNAME 时 icacls 会记成 "YS-PC-1138\:(F)"，冒号前为空，
        # 等于授权给了解析不出来的账户——结果文件谁都读不了（实测过）。
        user = os.environ.get("USERNAME") or ""
        computer = os.environ.get("COMPUTERNAME") or ""
        if not user:
            return
        principal = f"{computer}\\{user}" if computer else user

        # 顺序很重要：先授权再断继承。
        # 反过来的话，/inheritance:r 执行完那一瞬间文件变成「谁都不能访问」，
        # 万一后面的授权失败，文件就永久锁死。
        if not _icacls(["icacls", str(path), "/grant:r", f"{principal}:(F)"]):
            return
        _icacls(["icacls", str(path), "/inheritance:r"])
        return
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

def _icacls(args: list[str]) -> bool:
    """执行 icacls，返回是否成功。失败不抛异常——权限问题不该让保存整体失败。"""
    try:
        completed = subprocess.run(
            args, capture_output=True, timeout=15, check=False
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False



def describe(config: ProjectConfig) -> str:
    """给 agent 看的一句话摘要。不包含任何密码。"""
    parts: list[str] = []
    if config.frontend:
        label = f"前端 {config.frontend}"
        if config.login_user:
            label += f"（账号 {config.login_user}）"
        parts.append(label)
    if config.server.host:
        parts.append(f"服务器 {config.server.user}@{config.server.host}:{config.server.port}")
    for database in config.databases:
        label = f"{database.type} {database.host}:{database.port}/{database.schema}"
        if database.name:
            label = f"{database.name}（{label}）"
        parts.append(label)
    return "；".join(parts) if parts else "（未配置连接信息）"
