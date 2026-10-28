from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import timedelta, timezone as fixed_timezone, tzinfo
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import sandbox as sandbox_module
from .core import Contact


PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_PROJECT_DIR = PACKAGE_DIR.parents[1]
PROJECT_DIR = Path(
    os.environ.get(
        "DWS_CHAT_AGENT_HOME",
        SOURCE_PROJECT_DIR if (SOURCE_PROJECT_DIR / "config.json").is_file() else Path.cwd(),
    )
).expanduser().resolve()
WEB_DIR = PACKAGE_DIR / "web"
DEFAULT_CONFIG = PROJECT_DIR / "config.json"
DEFAULT_ENV = PROJECT_DIR / ".env"

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any]
    config_path: Path
    env_path: Path
    timezone: tzinfo
    contacts: tuple[Contact, ...]
    groups: tuple[Contact, ...]
    state_dir: Path
    workspace_root: Path
    worktree_root: Path
    sandbox_root: Path

    @property
    def mode(self) -> str:
        return str(self.raw.get("mode", "shadow"))

    @property
    def targets(self) -> tuple[Contact, ...]:
        """单聊联系人与群聊目标的并集：沙盒、面板等按「目标」处理的场景用它。"""
        return (*self.contacts, *self.groups)

    def contact_for(self, identity: str) -> Contact | None:
        for contact in (*self.contacts, *self.groups):
            if contact.identity == identity:
                return contact
        return None

    def group_for_conversation(self, conversation_id: str) -> Contact | None:
        for group in self.groups:
            if group.conversation_id == conversation_id:
                return group
        return None

    def workspace_for(self, contact: Contact | None) -> Path:
        """该联系人 agent 的实际工作目录（沙盒内的主项目）。"""
        if contact is None:
            return self.workspace_root
        return sandbox_module.primary_workspace(
            self.sandbox_root, self.workspace_root, contact.alias, contact.projects
        )

    @property
    def self_open_id(self) -> str:
        return str(self.raw["self"]["open_dingtalk_id"])

    @property
    def self_name(self) -> str:
        return str(self.raw["self"]["name"])

    @property
    def quiet_window(self) -> float:
        return float(self.raw.get("quiet_window_seconds", 20))

    @property
    def cooldown(self) -> float:
        return float(self.raw.get("human_cooldown_seconds", 600))


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not _ENV_KEY.fullmatch(key):
            raise ValueError(f"invalid .env entry at {path}:{line_number}")
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == '"':
            value = json.loads(value)
        values[key] = value
    return values


def _resolve_env(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_env(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_env(item, env) for item in value]
    if not isinstance(value, str):
        return value
    full_match = _ENV_REF.fullmatch(value)
    if full_match:
        key = full_match.group(1)
        if key not in env:
            raise ValueError(f"missing required environment variable: {key}")
        raw_value = env[key]
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError:
            return raw_value

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in env:
            raise ValueError(f"missing required environment variable: {key}")
        return env[key]

    return _ENV_REF.sub(replace, value)


def _contact_projects(item: Mapping[str, Any]) -> tuple[str, ...]:
    """读取联系人的项目列表；非法名字直接丢弃，避免带进沙盒路径。"""
    raw_projects = item.get("projects")
    if isinstance(raw_projects, str):
        raw_projects = [raw_projects]
    if not isinstance(raw_projects, list):
        return ()
    names: list[str] = []
    for entry in raw_projects:
        name = sandbox_module.normalize_project_name(entry)
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _text_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    items: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            continue
        text = entry.strip()
        if text and text not in items:
            items.append(text)
    return tuple(items)


def groups_from_raw(items: Any) -> tuple[Contact, ...]:
    """把配置里的 groups 列表转成群聊目标。

    群没有 userId，身份就是 openConversationId；项目列表语义与联系人一致，
    所以复用同一个 Contact 结构，沙盒那套可以原样工作。
    """
    if not isinstance(items, list):
        return ()
    groups: list[Contact] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        conversation_id = str(item.get("open_conversation_id") or "").strip()
        alias = str(item.get("alias") or "").strip()
        if not conversation_id or not alias:
            continue
        groups.append(
            Contact(
                alias=alias,
                display_name=str(item.get("display_name") or item.get("title") or alias),
                user_id="",
                open_dingtalk_id="",
                projects=_contact_projects(item),
                conversation_id=conversation_id,
                allowed_senders=_text_list(item.get("allowed_senders")),
            )
        )
    return tuple(groups)


def contacts_from_raw(items: Any) -> tuple[Contact, ...]:
    """把配置里的 contacts 列表转成 Contact 元组（含项目列表）。

    web 控制台保存后联系人要重启才生效，但沙盒刷新按钮需要按磁盘内容立刻
    对齐目录，所以这里提供一个与 load_settings 同规则的转换入口。
    """
    if not isinstance(items, list):
        return ()
    contacts: list[Contact] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        try:
            contacts.append(
                Contact(
                    alias=str(item["alias"]),
                    display_name=str(item["display_name"]),
                    user_id=str(item.get("user_id") or ""),
                    open_dingtalk_id=str(item["open_dingtalk_id"]),
                    projects=_contact_projects(item),
                )
            )
        except KeyError:
            continue
    return tuple(contacts)


def load_settings(path: Path, env_path: Path = DEFAULT_ENV) -> Settings:
    env = _read_env_file(env_path)
    env.update(os.environ)
    raw = _resolve_env(json.loads(path.read_text(encoding="utf-8")), env)
    timezone = _load_timezone(str(raw.get("timezone", "Asia/Shanghai")))
    contacts = contacts_from_raw(raw["contacts"])
    groups = groups_from_raw(raw.get("groups"))
    state_value = Path(str(raw.get("state_dir", "state")))
    state_dir = state_value if state_value.is_absolute() else path.parent / state_value
    workspace_root = Path(str(raw["workspace_root"])).expanduser().resolve()
    worktree_root = Path(str(raw["worktree_root"])).expanduser().resolve()
    sandbox_value = str(raw.get("sandbox_root") or "").strip()
    sandbox_root = (
        Path(sandbox_value).expanduser().resolve()
        if sandbox_value
        else (workspace_root.parent / ".agent-sandboxes").resolve()
    )
    return Settings(
        raw=raw,
        config_path=path.resolve(),
        env_path=env_path.resolve(),
        timezone=timezone,
        contacts=contacts,
        groups=groups,
        state_dir=state_dir.resolve(),
        workspace_root=workspace_root,
        worktree_root=worktree_root,
        sandbox_root=sandbox_root,
    )


def _load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Shanghai":
            return fixed_timezone(timedelta(hours=8), name)
        raise
