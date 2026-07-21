from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import timedelta, timezone as fixed_timezone, tzinfo
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    state_dir: Path
    workspace_root: Path
    worktree_root: Path

    @property
    def mode(self) -> str:
        return str(self.raw.get("mode", "shadow"))

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


def load_settings(path: Path, env_path: Path = DEFAULT_ENV) -> Settings:
    env = _read_env_file(env_path)
    env.update(os.environ)
    raw = _resolve_env(json.loads(path.read_text(encoding="utf-8")), env)
    timezone = _load_timezone(str(raw.get("timezone", "Asia/Shanghai")))
    contacts = tuple(
        Contact(
            alias=str(item["alias"]),
            display_name=str(item["display_name"]),
            user_id=str(item["user_id"]),
            open_dingtalk_id=str(item["open_dingtalk_id"]),
        )
        for item in raw["contacts"]
    )
    state_value = Path(str(raw.get("state_dir", "state")))
    state_dir = state_value if state_value.is_absolute() else path.parent / state_value
    workspace_root = Path(str(raw["workspace_root"])).expanduser().resolve()
    worktree_root = Path(str(raw["worktree_root"])).expanduser().resolve()
    return Settings(
        raw=raw,
        config_path=path.resolve(),
        env_path=env_path.resolve(),
        timezone=timezone,
        contacts=contacts,
        state_dir=state_dir.resolve(),
        workspace_root=workspace_root,
        worktree_root=worktree_root,
    )


def _load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Shanghai":
            return fixed_timezone(timedelta(hours=8), name)
        raise
