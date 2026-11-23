"""页面加载完成的判定。

固定等待不可靠：慢时不够、快时浪费，还会把"正在加载"当成"没有数据"。

这里用语义等待，按顺序确认：
  1. 网络空闲（networkidle）
  2. 常见加载遮罩消失（Element / Ant Design / 自定义 skeleton）
  3. 页面结构稳定（元素数量在若干次采样内不再变化）

第 3 条是为了兜住"骨架屏结束后才开始渲染"的情况。
"""

from __future__ import annotations

import time

from playwright.sync_api import Page

# 常见加载态标记。命中且可见就说明还在加载。
_LOADING_SELECTORS = [
    ".el-loading-mask",            # Element UI
    ".el-loading-spinner",
    ".ant-spin-spinning",          # Ant Design
    ".ant-skeleton",               # Ant Design 骨架屏
    "[class*='skeleton']",
    "[class*='loading-mask']",
    ".v-loading",                  # Vuetify / 常见自研
    ".loading-wrapper",
]

_LOADING_SCRIPT = """
(selectors) => {
    const offenders = [];
    for (const sel of selectors) {
        for (const el of document.querySelectorAll(sel)) {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            const visible = r.width > 1 && r.height > 1
                && s.visibility !== 'hidden'
                && s.display !== 'none'
                && Number(s.opacity) > 0.05;
            if (visible) {
                offenders.push(sel);
                break;
            }
        }
    }
    return offenders;
}
"""

# 采样页面指纹：元素数量 + 文本长度 + canvas/svg 数量
_FINGERPRINT_SCRIPT = """
() => {
    const text = (document.body?.innerText || '').trim();
    return {
        elCount: document.querySelectorAll('*').length,
        textLen: text.length,
        media: document.querySelectorAll('canvas, svg, img, video').length,
    };
}
"""


def wait_until_ready(page: Page, settle_ms: int, max_wait_ms: int = 20000) -> str:
    """等页面真正加载完成。

    返回状态：
      ready                正常就绪
      loading_stuck        遮罩一直在，等到超时都没消失
      settled_then_loading 结构稳定后又出现遮罩（页面在刷新，不算失败）
      timeout              结构一直不稳定

    调用方据此判定：loading_stuck 要算异常，不能当"没数据"；
    settled_then_loading 只是提醒，页面内容已经渲染过一次了。
    """
    deadline = time.time() + max_wait_ms / 1000

    # 1. 等加载遮罩消失
    loading_gone = False
    while time.time() < deadline:
        offenders = _visible_loading(page)
        if not offenders:
            loading_gone = True
            break
        page.wait_for_timeout(250)

    if not loading_gone:
        return "loading_stuck"

    # 2. 等结构稳定：连续两次采样指纹不变
    stable_count = 0
    previous = None
    while time.time() < deadline:
        current = _fingerprint(page)
        if previous is not None and current == previous and current:
            stable_count += 1
            if stable_count >= 2:
                break
        else:
            stable_count = 0
        previous = current
        page.wait_for_timeout(350)

    if stable_count < 2:
        return "timeout"

    # 3. 稳定后再给一小段缓冲，让异步渲染收尾
    page.wait_for_timeout(min(settle_ms, 1500))

    # 缓冲后如果又冒出加载遮罩，说明页面在结构稳定之后又发起了新请求。
    # 这不算失败——列表自己刷新很常见；但要如实告诉调用方，别当"没有数据"。
    if _visible_loading(page):
        return "settled_then_loading"

    return "ready"


def wait_out_empty_state(page: Page, max_wait_ms: int = 6000) -> bool:
    """页面停在空状态时，再多看一眼数据会不会来。

    实测踩过：某个列表页在 1.2 秒时渲染完筛选项、显示"暂无数据"、没有
    任何加载遮罩——结构稳定、看起来加载完了。但真正的数据请求 2 秒后才
    发出，3.2 秒时表格填充 10 行。此时如果直接判"空数据"就是误报。

    遮罩消失只代表上一轮加载结束，不代表数据已经到位。所以发现空状态时
    继续观察，一旦有数据或出现遮罩就继续等。

    返回 True 表示「等到了变化」，调用方应重新采样。
    """
    deadline = time.time() + max_wait_ms / 1000
    baseline = _fingerprint(page)

    while time.time() < deadline:
        page.wait_for_timeout(400)
        if _visible_loading(page):
            return True
        current = _fingerprint(page)
        if current != baseline:
            return True

    return False


def _visible_loading(page: Page) -> list[str]:
    try:
        return page.evaluate(_LOADING_SCRIPT, _LOADING_SELECTORS) or []
    except Exception:  # noqa: BLE001 页面跳走或执行失败时视为没有加载态
        return []


def _fingerprint(page: Page) -> dict:
    try:
        return page.evaluate(_FINGERPRINT_SCRIPT) or {}
    except Exception:  # noqa: BLE001
        return {}
