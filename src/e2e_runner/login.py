"""登录模块。

按配置执行登录步骤。已知的坑都内建在这里：

- 密码框有两个时（一个隐藏），要取可见的那个
- 必须勾选用户协议才响应登录
- Element UI 的 checkbox 要点外层 .el-checkbox，不能点 input
- 有的系统 URL 不变，只能靠元素判断是否登录成功
"""

from __future__ import annotations

import time
from typing import Any

from playwright.sync_api import Page

from .config import E2EConfig


class LoginError(RuntimeError):
    """登录失败。登录不成功后面全部无意义，必须立刻停。"""


def _resolve_selector(page: Page, selector: str, prefer_visible: bool = True) -> Any:
    """取第一个可见的匹配元素。

    同一选择器可能命中多个（比如隐藏的密码框），取可见的那个。
    """
    locator = page.locator(selector)
    count = locator.count()
    if count == 0:
        return None
    if not prefer_visible:
        return locator.first
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:  # noqa: BLE001
            continue
    return locator.first


def login(page: Page, config: E2EConfig) -> str:
    """执行登录，返回登录后的 URL。

    优先用配置里的 steps；没有则用内置的默认流程。
    """
    login_config = config.login
    url = config.base_url + login_config.url

    page.goto(url, wait_until="domcontentloaded", timeout=config.detect.page_timeout_ms)
    _settle(page, 1200)

    if login_config.steps:
        _run_steps(page, login_config.steps, config)
    else:
        _default_flow(page, config)

    # 等跳转或等成功标志
    if not _wait_success(page, config):
        raise LoginError(
            f"登录后仍在 {page.url}，未跳转也未出现成功标志。"
            "检查账号密码，或配置 login.successUrlNotContains / successSelector。"
        )

    _settle(page, 800)
    return page.url


def _default_flow(page: Page, config: E2EConfig) -> None:
    """内置默认登录流程：账号 → 密码 → 勾协议 → 点登录。"""
    username_selectors = [
        'input[placeholder*="账号"]',
        'input[placeholder*="用户名"]',
        'input[placeholder*="手机号"]',
        'input[type="text"]',
    ]
    password_selectors = [
        'input[placeholder*="密码"]',
        'input[type="password"]',
    ]
    agree_selectors = [
        ".el-checkbox",
        ".ant-checkbox-wrapper",
        'input[type="checkbox"]',
    ]
    submit_selectors = [
        'button:has-text("登 录")',
        'button:has-text("登录")',
        ".el-button--primary",
        'button[type="submit"]',
    ]

    _fill_first(page, username_selectors, config.login.username, "账号")
    _fill_first(page, password_selectors, config.login.password, "密码")

    # 协议勾选：有就点，没有不算错
    for selector in agree_selectors:
        element = _resolve_selector(page, selector)
        if element is None:
            continue
        try:
            element.click(timeout=3000)
            _settle(page, 300)
        except Exception:  # noqa: BLE001
            continue
        break

    _click_first(page, submit_selectors, "登录")


def _fill_first(page: Page, selectors: list[str], value: str, label: str) -> None:
    for selector in selectors:
        element = _resolve_selector(page, selector)
        if element is None:
            continue
        try:
            element.fill(value, timeout=5000)
            return
        except Exception:  # noqa: BLE001
            continue
    raise LoginError(f"找不到{label}输入框，尝试过的选择器: {selectors}")


def _click_first(page: Page, selectors: list[str], label: str) -> None:
    for selector in selectors:
        element = _resolve_selector(page, selector)
        if element is None:
            continue
        try:
            element.click(timeout=5000)
            return
        except Exception:  # noqa: BLE001
            continue
    raise LoginError(f"找不到{label}按钮，尝试过的选择器: {selectors}")


def _run_steps(page: Page, steps: list[dict[str, Any]], config: E2EConfig) -> None:
    """按配置执行登录步骤。"""
    for step in steps:
        action = step.get("action", "")
        selector = step.get("selector", "")
        if action == "fill":
            value = step.get("value", "")
            if value == "$username":
                value = config.login.username
            elif value == "$password":
                value = config.login.password
            element = _resolve_selector(page, selector)
            if element is None:
                raise LoginError(f"步骤 fill 找不到元素: {selector}")
            element.fill(value, timeout=8000)
        elif action == "click":
            element = _resolve_selector(page, selector)
            if element is None:
                raise LoginError(f"步骤 click 找不到元素: {selector}")
            element.click(timeout=8000)
        elif action == "check":
            element = _resolve_selector(page, selector)
            if element is None:
                raise LoginError(f"步骤 check 找不到元素: {selector}")
            element.click(timeout=8000)
        elif action == "waitUrlNotContain":
            value = step.get("value", "/login")
            deadline = time.time() + (step.get("timeout", 20000) / 1000)
            while time.time() < deadline:
                if value not in page.url:
                    break
                _settle(page, 300)
        elif action == "wait":
            _settle(page, int(step.get("timeout", 1000)))
        else:
            raise LoginError(f"未知的登录步骤: {action}")


def _wait_success(page: Page, config: E2EConfig) -> bool:
    """等登录成功。

    两条判据任一满足即可：
      - URL 不再包含 successUrlNotContains
      - 出现了 successSelector 指定的元素
    """
    login_config = config.login
    deadline = time.time() + 25
    while time.time() < deadline:
        not_contains = login_config.success_url_not_contains
        url_ok = not_contains and not_contains not in page.url
        selector_ok = False
        if login_config.success_selector:
            try:
                selector_ok = page.locator(login_config.success_selector).count() > 0
            except Exception:  # noqa: BLE001
                selector_ok = False
        if url_ok or selector_ok:
            return True
        _settle(page, 400)
    return False


def _settle(page: Page, milliseconds: int) -> None:
    try:
        page.wait_for_timeout(milliseconds)
    except Exception:  # noqa: BLE001
        pass
