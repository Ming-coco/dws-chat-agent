"""遍历引擎：把页面一个个打开、采集、判定。

改动要点：菜单发现出来的节点自带完整路径，直接 goto 就能到页面，
不需要模拟点击。这比点菜单稳得多——不依赖菜单是否可见、是否需要展开。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

from .browser import BrowserSession, PageSignal, SignalCollector
from .config import E2EConfig
from .detect import FAIL, PASS, WARN, Verdict, judge
from .discover import MenuNode
from .loading import wait_until_ready


@dataclass
class PageResult:
    name: str
    path: str
    signal: PageSignal
    verdict: Verdict
    duration_ms: int = 0


@dataclass
class RunResult:
    name: str
    started_at: str
    duration_ms: int = 0
    pages: list[PageResult] = field(default_factory=list)
    error: str = ""
    # 菜单来源的证据链（哪个接口、一级菜单数）
    menu_source: str = ""
    menu_top_count: int = 0

    @property
    def passed(self) -> int:
        return sum(1 for page in self.pages if page.verdict.level == PASS)

    @property
    def warned(self) -> int:
        return sum(1 for page in self.pages if page.verdict.level == WARN)

    @property
    def failed(self) -> int:
        return sum(1 for page in self.pages if page.verdict.level == FAIL)

    @property
    def overall(self) -> str:
        if self.error:
            return FAIL
        if self.failed:
            return FAIL
        if self.warned:
            return WARN
        return PASS


def collect_targets(menus: list[MenuNode]) -> list[tuple[str, str]]:
    """把菜单树摊平成 (显示名, 完整 URL 路径) 列表。

    只收「叶子页面」：
      - type 必须是 menu
      - 必须带路径
      - 不能有可导航的子菜单（有子菜单的是容器，不是页面）

    容器菜单（营销、风控）本身不是页面，单独测它没有意义——
    它会显示为空壳，然后被误判成"空数据"。
    """
    targets: list[tuple[str, str]] = []

    def has_nav_child(node: MenuNode) -> bool:
        return any(
            child.kind == "menu" and child.full_path for child in node.children
        )

    def walk(nodes: list[MenuNode], prefix: str) -> None:
        for node in nodes:
            label = f"{prefix} / {node.name}" if prefix else node.name
            is_page = (
                node.kind == "menu"
                and bool(node.full_path)
                and not has_nav_child(node)
            )
            if is_page:
                targets.append((label, node.full_path))
            if node.children:
                walk(node.children, label)

    walk(menus, "")
    return targets


def visit(
    session: BrowserSession,
    config: E2EConfig,
    name: str,
    path: str,
    shot_dir: Path | None,
) -> PageResult:
    """打开一个页面，采集并判定。每页独立 page，互不污染。"""
    started = time.time()
    url = path if path.startswith("http") else config.base_url + path
    page: Page = session.new_page()
    collector = SignalCollector(page, config)
    load_timeout = False

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=config.detect.page_timeout_ms)
    except Exception as error:  # noqa: BLE001
        message = str(error)
        if "Timeout" in message or "timeout" in message:
            load_timeout = True
        else:
            page.close()
            return PageResult(
                name=name,
                path=url,
                signal=PageSignal(url=url),
                verdict=Verdict(
                    level=FAIL,
                    reason=f"打开失败: {type(error).__name__}",
                    layer="browser",
                    details=[message[:200]],
                ),
                duration_ms=int((time.time() - started) * 1000),
            )

    # 语义等待：等加载遮罩消失 + 结构稳定。固定 sleep 会把加载中当成没数据。
    ready_state = wait_until_ready(page, config.detect.settle_ms)

    signal = collector.snapshot(url)
    signal.load_timeout = load_timeout or ready_state == "loading_stuck"
    signal.still_loading = ready_state == "loading_stuck"

    if shot_dir is not None:
        screenshot = shot_dir / f"{_safe_name(name)}.png"
        try:
            page.screenshot(path=str(screenshot), full_page=True)
            signal.screenshot = str(screenshot)
        except Exception:  # noqa: BLE001
            pass

    backend_logs = _maybe_fetch_logs(config, url)
    verdict = judge(signal, config, backend_logs)

    page.close()
    return PageResult(
        name=name,
        path=url,
        signal=signal,
        verdict=verdict,
        duration_ms=int((time.time() - started) * 1000),
    )


def _maybe_fetch_logs(config: E2EConfig, url: str) -> str:
    """按需抓后端日志。抓不到不阻断测试。"""
    if not config.logs.enabled or config.logs.via != "ssh":
        return ""
    from .logs import fetch_recent_errors

    return fetch_recent_errors(config, url)


def _safe_name(name: str) -> str:
    import re

    return re.sub(r'[\\/:*?"<>|\s]+', "_", name)[:60]
