"""浏览器启动与页面采集。

复用本机 Edge，不下载自带 Chromium（内网友好）。
采集控制台错误、页面异常、接口状态，这些都是判定页面好坏的原料。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from .config import E2EConfig


@dataclass
class RequestRecord:
    """一次接口调用的记录。用于判断「接口给了数据但页面没渲染」。"""

    url: str
    status: int
    method: str
    # 响应体是否非空（只取前若干字节判断，不整份读，避免大响应拖慢）
    has_body: bool = False
    # 响应体里是否像"有数据"（JSON 数组非空或对象里有 list/records 之类的非空字段）
    likely_has_data: bool = False
    body_bytes: int = 0


@dataclass
class PageSignal:
    """一个页面访问过程中采集到的全部信号。"""

    url: str
    title: str = ""
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    bad_requests: list[RequestRecord] = field(default_factory=list)
    requests: list[RequestRecord] = field(default_factory=list)
    # 页面结构统计
    visible_elements: int = 0
    body_text_length: int = 0
    has_media: bool = False
    has_table: bool = False
    table_rows: int = 0
    empty_hint: bool = False
    load_timeout: bool = False
    # 加载态一直没消失（加载中就被采样了）
    still_loading: bool = False
    # 结构稳定后又出现加载遮罩（页面在刷新，内容已渲染过一次）
    settled_then_loading: bool = False
    screenshot: str = ""


class BrowserSession:
    """一次测试会话：启动浏览器 → 登录 → 遍历 → 关闭。"""

    def __init__(self, config: E2EConfig) -> None:
        self.config = config
        self._playwright: Any = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def __enter__(self) -> "BrowserSession":
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(
            executable_path=self.config.browser_executable,
            headless=self.config.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context_args: dict[str, Any] = {
            "viewport": {
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
            "ignore_https_errors": True,
        }
        if self.config.storage_state:
            from pathlib import Path

            if Path(self.config.storage_state).is_file():
                context_args["storage_state"] = self.config.storage_state
        self.context = self.browser.new_context(**context_args)
        self.page = self.context.new_page()
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
        finally:
            if self._playwright:
                self._playwright.stop()

    def new_page(self) -> Page:
        """新开一页，用于每页独立采集，互不污染。"""
        assert self.context is not None
        page = self.context.new_page()
        page.set_default_timeout(self.config.detect.page_timeout_ms)
        return page


def collect(page: Page, config: E2EConfig, capture_api: bool = True) -> PageSignal:
    """在页面加载过程中采集信号。

    用法：先 collect_attach 挂监听，再 goto，最后 collect_finish 收结果。
    这里做成一个 holder，因为监听必须早于导航。
    """
    raise NotImplementedError("用 SignalCollector")


class SignalCollector:
    """挂监听 → 导航 → 收结果。

    监听必须早于 goto，否则漏掉首屏请求。
    """

    def __init__(self, page: Page, config: E2EConfig) -> None:
        self.page = page
        self.config = config
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.requests: list[RequestRecord] = []
        self._ignore_console = config.ignore.console_patterns
        self._ignore_api = config.ignore.api_patterns
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)
        page.on("response", self._on_response)

    def _ignored_console(self, text: str) -> bool:
        return any(pattern in text for pattern in self._ignore_console)

    def _ignored_api(self, url: str) -> bool:
        return any(pattern in url for pattern in self._ignore_api)

    def _on_console(self, message: Any) -> None:
        if message.type != "error":
            return
        text = message.text or ""
        if self._ignored_console(text):
            return
        self.console_errors.append(text[:300])

    def _on_page_error(self, error: Any) -> None:
        self.page_errors.append(str(error)[:300])

    def _on_response(self, response: Any) -> None:
        try:
            url = response.url
        except Exception:  # noqa: BLE001
            return
        if self.config.detect.api_keyword not in url:
            return
        if self._ignored_api(url):
            return

        status = 0
        try:
            status = response.status
        except Exception:  # noqa: BLE001
            pass

        record = RequestRecord(
            url=url.split("?")[0][:200],
            status=status,
            method=response.request.method,
        )

        # 只在需要时读 body，且只读一次；大响应不整份拉
        try:
            if response.request.resource_type in ("xhr", "fetch"):
                record.has_body = True
                if _is_data_api(url):
                    body = response.body()
                    record.body_bytes = len(body)
                    record.likely_has_data = _looks_like_data(body)
        except Exception:  # noqa: BLE001  资源已释放或不可读，忽略
            pass

        self.requests.append(record)

    def bad_requests(self) -> list[RequestRecord]:
        return [
            record
            for record in self.requests
            if record.status >= self.config.detect.http_status_min
        ]

    def snapshot(self, url: str = "") -> PageSignal:
        """采集页面结构。导航完成、等待稳定后调用。"""
        page = self.page
        try:
            stats = page.evaluate(
                """() => {
                    const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0
                            && s.visibility !== 'hidden' && s.display !== 'none';
                    };
                    const all = [...document.querySelectorAll('*')];
                    const visible = all.filter(isVisible).length;
                    const text = (document.body?.innerText || '').trim();
                    const media = document.querySelectorAll('canvas, svg, img, video').length;
                    const tableEls = document.querySelectorAll('.el-table, .ant-table, table');

                    // 数数据行：不同框架结构差异很大。
                    // Element 用 .el-table__row，Ant 用 .ant-table-row，
                    // 但有的系统 tr 直接挂在 table 下、没有 tbody，
                    // 所以统一退回到「所有含内容的非表头 tr」。
                    let rows = document.querySelectorAll(
                        '.el-table__row, .ant-table-row'
                    ).length;
                    if (!rows) {
                        let counted = 0;
                        for (const tr of document.querySelectorAll('tr')) {
                            if (tr.querySelector('th')) continue;      // 表头
                            const cells = tr.querySelectorAll('td');
                            if (!cells.length) continue;
                            if ((tr.innerText || '').trim().length < 2) continue;
                            counted++;
                        }
                        rows = counted;
                    }

                    return {
                        visible,
                        textLength: text.length,
                        media,
                        tables: tableEls.length,
                        rows,
                        emptyHint: /暂无数据|暂无记录|没有找到|无数据|未查询到/.test(text),
                    };
                }"""
            )
        except Exception:  # noqa: BLE001  页面可能已跳走
            stats = {}

        return PageSignal(
            url=url or page.url,
            title=_safe_title(page),
            console_errors=list(self.console_errors),
            page_errors=list(self.page_errors),
            bad_requests=self.bad_requests(),
            requests=list(self.requests),
            visible_elements=int(stats.get("visible", 0)),
            body_text_length=int(stats.get("textLength", 0)),
            has_media=int(stats.get("media", 0)) > 0,
            has_table=int(stats.get("tables", 0)) > 0,
            table_rows=int(stats.get("rows", 0)),
            empty_hint=bool(stats.get("emptyHint", False)),
        )


def _safe_title(page: Page) -> str:
    try:
        return (page.title() or "")[:120]
    except Exception:  # noqa: BLE001
        return ""


_EMPTY_LIST = re.compile(r'"(list|records|rows|data|items)"\s*:\s*\[\s*\]')

# 这些接口名一看就不是业务数据接口，不该用来判断"有没有数据"
_DATA_IRRELEVANT = (
    "iftipswsw",
    "ifTipsPsw",
    "getUserPermission",
    "getMessageCount",
    "getUnreadCount",
    "heartbeat",
    "captcha",
    "config",
    "dict",
    "/user/info",
    "getUserInfo",
)


def _is_data_api(url: str) -> bool:
    """排除配置类、提示类接口。

    之前的实现把任何非空 JSON 都当成"有数据"，导致 ifTipsPsw（密码提示）
    这种接口也能触发"有数据但没渲染"的误报。
    """
    lowered = url.lower()
    return not any(key.lower() in lowered for key in _DATA_IRRELEVANT)


def _looks_like_data(body: bytes) -> bool:
    """判断响应体是否包含列表型业务数据。

    必须严格：只认明确的列表非空。宁可漏判（把有数据当没数据），
    也不要误判——误判会把正常的空列表页面报成"渲染失败"。
    """
    if not body or len(body) < 20:
        return False
    text = body[:8000].decode("utf-8", "replace")
    stripped = text.strip()

    if stripped in ("{}", "[]", "null"):
        return False

    # 明确的空列表信号
    if _EMPTY_LIST.search(stripped):
        normalized = stripped.replace(" ", "")
        if '"total":0' in normalized or '"totalcount":0' in normalized:
            return False
        # 有 list:[] 但没 total，也不当成有数据
        return False

    # 列表字段非空："list":[{...}] / "records":[{...}]
    if re.search(r'"(list|records|rows|items|data)"\s*:\s*\[\s*\{', stripped):
        return True
    # 顶层就是数组且元素是对象
    if stripped.startswith("[") and re.search(r"\[\s*\{", stripped):
        return True

    return False
