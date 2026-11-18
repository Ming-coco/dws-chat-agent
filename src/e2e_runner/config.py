"""配置加载与校验。

一个系统一份 JSON，放在 configs/ 下。凭据从环境变量读，不落明文。
配置校验失败要立刻报错——静默使用错误配置会让测试"通过"得毫无意义。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 配置里允许引用环境变量的写法：${VAR_NAME}
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """配置有问题。宁可停下来，也不要带着错配置跑出一份假报告。"""


@dataclass
class LoginConfig:
    url: str
    username: str
    password: str
    # 登录表单的步骤，按顺序执行。为空时用默认的「填账号 → 填密码 → 点登录」。
    steps: list[dict[str, Any]] = field(default_factory=list)
    # 登录成功的判定：URL 里不含某个路径
    success_url_not_contains: str = "/login"
    # 登录成功的判定：出现某个元素
    success_selector: str = ""


@dataclass
class DiscoverConfig:
    # auto: 自动发现；manual: 只用配置里的 menus
    mode: str = "auto"
    max_depth: int = 2
    # 自动发现后，这些名字不算菜单（展开/收起之类的开关）
    exclude_names: list[str] = field(default_factory=list)


@dataclass
class LogConfig:
    """服务端日志抓取。用来区分「真的没数据」和「后端报错但被吞了」。"""

    enabled: bool = False
    # 抓日志的方式：ssh（远程 docker logs）或 none
    via: str = "none"
    ssh_host: str = ""
    ssh_user: str = ""
    ssh_password: str = ""
    # 页面路径前缀 → 容器名。用于定位该页面调用的是哪个后端
    container_map: dict[str, str] = field(default_factory=dict)
    # 匹配报错的关键词
    error_pattern: str = "Exception|ERROR|Traceback|NullPointerException"


@dataclass
class DetectConfig:
    console_errors: bool = True
    page_errors: bool = True
    http_status_min: int = 400
    api_keyword: str = "/api/"
    # 判定白屏：可见元素少于这个数
    min_visible_elements: int = 5
    # 判定白屏：文本少于这个长度且没有图表
    min_body_text: int = 20
    # 单页最长等待
    page_timeout_ms: int = 30000
    # 网络空闲后的额外缓冲
    settle_ms: int = 2500


@dataclass
class IgnoreConfig:
    console_patterns: list[str] = field(default_factory=list)
    api_patterns: list[str] = field(default_factory=list)


@dataclass
class E2EConfig:
    name: str
    base_url: str
    login: LoginConfig
    browser_executable: str
    headless: bool = True
    viewport_width: int = 1600
    viewport_height: int = 1000
    storage_state: str = ""
    discover: DiscoverConfig = field(default_factory=DiscoverConfig)
    menus: list[dict[str, Any]] = field(default_factory=list)
    detect: DetectConfig = field(default_factory=DetectConfig)
    ignore: IgnoreConfig = field(default_factory=IgnoreConfig)
    logs: LogConfig = field(default_factory=LogConfig)
    dangerous_button_blacklist: list[str] = field(
        default_factory=lambda: ["删除", "批量删除", "提交", "确认提交", "发布", "下线", "禁用"]
    )
    config_path: Path | None = None


def _expand_env(value: Any) -> Any:
    """展开 ${VAR} 引用。变量不存在时返回空串，由后面的校验发现。"""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _require(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data or data[key] in ("", None):
        raise ConfigError(f"{where} 缺少必填项: {key}")
    return data[key]


def load_config(path: Path) -> E2EConfig:
    """读取并校验配置文件。"""
    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise ConfigError(f"配置文件不是合法 JSON: {path}: {error}") from error

    data = _expand_env(raw)

    name = _require(data, "name", "配置")
    base_url = _require(data, "baseUrl", "配置").rstrip("/")

    login_raw = data.get("login") or {}
    login = LoginConfig(
        url=login_raw.get("url", "/login"),
        username=login_raw.get("username", ""),
        password=login_raw.get("password", ""),
        steps=login_raw.get("steps") or [],
        success_url_not_contains=login_raw.get("successUrlNotContains", "/login"),
        success_selector=login_raw.get("successSelector", ""),
    )
    # 账号密码缺失是硬错误：没有凭据的登录必然失败，让他跑到一半再报不如现在报
    if not login.username:
        raise ConfigError("login.username 为空（检查环境变量是否设置）")
    if not login.password:
        raise ConfigError("login.password 为空（检查环境变量是否设置）")

    browser_raw = data.get("browser") or {}
    browser_path = browser_raw.get("executablePath") or ""
    if not browser_path:
        browser_path = find_browser()
    if not browser_path:
        raise ConfigError("找不到浏览器，请设置 browser.executablePath")

    discover_raw = data.get("discover") or {}
    discover = DiscoverConfig(
        mode=discover_raw.get("mode", "auto"),
        max_depth=int(discover_raw.get("maxDepth", 2)),
        exclude_names=discover_raw.get("excludeNames") or [],
    )

    detect_raw = data.get("detect") or {}
    detect = DetectConfig(
        console_errors=bool(detect_raw.get("consoleErrors", True)),
        page_errors=bool(detect_raw.get("pageErrors", True)),
        http_status_min=int(detect_raw.get("httpStatusMin", 400)),
        api_keyword=detect_raw.get("apiKeyword", "/api/"),
        min_visible_elements=int(detect_raw.get("minVisibleElements", 5)),
        min_body_text=int(detect_raw.get("minBodyText", 20)),
        page_timeout_ms=int(detect_raw.get("pageTimeoutMs", 30000)),
        settle_ms=int(detect_raw.get("settleMs", 2500)),
    )

    ignore_raw = data.get("ignore") or {}
    ignore = IgnoreConfig(
        console_patterns=ignore_raw.get("consolePatterns") or [],
        api_patterns=ignore_raw.get("apiPatterns") or [],
    )

    logs_raw = data.get("logs") or {}
    logs = LogConfig(
        enabled=bool(logs_raw.get("enabled", False)),
        via=logs_raw.get("via", "none"),
        ssh_host=logs_raw.get("sshHost", "") or (logs_raw.get("ssh") or {}).get("host", ""),
        ssh_user=logs_raw.get("sshUser", "") or (logs_raw.get("ssh") or {}).get("user", ""),
        ssh_password=logs_raw.get("sshPassword", "")
        or (logs_raw.get("ssh") or {}).get("password", ""),
        container_map=logs_raw.get("containerMap") or {},
        error_pattern=logs_raw.get("errorPattern", "Exception|ERROR|Traceback"),
    )

    return E2EConfig(
        name=name,
        base_url=base_url,
        login=login,
        browser_executable=browser_path,
        headless=bool(browser_raw.get("headless", True)),
        viewport_width=int((browser_raw.get("viewport") or {}).get("width", 1600)),
        viewport_height=int((browser_raw.get("viewport") or {}).get("height", 1000)),
        storage_state=data.get("storageState", ""),
        discover=discover,
        menus=data.get("menus") or [],
        detect=detect,
        ignore=ignore,
        logs=logs,
        dangerous_button_blacklist=data.get("dangerousButtonBlacklist")
        or ["删除", "批量删除", "提交", "确认提交", "发布", "下线", "禁用"],
        config_path=path,
    )


def find_browser() -> str:
    """找本机已安装的浏览器。

    不硬编码单一路径：不同机器装的浏览器不一样（Edge / Chrome / 绿色版 /
    32 位装在不同目录）。按常见位置依次探测，找到第一个可用的。

    配置里显式指定 browser.executablePath 时不会走到这里。
    """
    candidates = [
        # Edge
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        # Chrome
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        # Chromium
        os.path.expandvars(r"%LOCALAPPDATA%\Chromium\Application\chrome.exe"),
    ]
    # 环境变量优先，便于在 CI 或不同机器上覆盖
    override = os.environ.get("E2E_BROWSER_PATH", "").strip()
    if override:
        candidates.insert(0, override)

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return ""
