"""判定：给每个页面的采集信号定性。

三层归因，这是工具的核心价值——不只说"有问题"，还要说"问题在哪一层"。

  接口层：接口返回了什么？
      ↓ 200 + 有列表数据
  渲染层：页面把这些数据画出来了吗？
      ↓ 没画 → FAIL（前端渲染问题，确定是 bug）

      ↓ 200 + 空列表
  服务层：后端日志里有没有异常？
      ↓ 有异常 → WARN（后端报错被吞了，最隐蔽的问题）
      ↓ 无异常 → WARN（可能真的没数据，需要业务确认）

重要：判定「没数据」之前必须先排除「还在加载」。加载中的空列表不是问题。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .browser import PageSignal
from .config import E2EConfig

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class Verdict:
    level: str
    reason: str
    # 归因到哪一层：browser / frontend / backend / unknown
    layer: str = ""
    details: list[str] = field(default_factory=list)


def judge(signal: PageSignal, config: E2EConfig, backend_logs: str = "") -> Verdict:
    """给出这个页面的判定。"""

    # ---------- 0. 还在加载：不能判"没数据" ----------
    if signal.still_loading or signal.load_timeout:
        return Verdict(
            level=WARN,
            reason="加载未完成，页面可能还在请求数据",
            layer="browser",
            details=[
                "加载遮罩在等待时间内一直没有消失",
                "如果该页面正常就很慢，可调大 detect.pageTimeoutMs",
            ],
        )

    # ---------- 1. 硬失败：页面根本没起来 ----------
    if _is_blank(signal, config):
        return Verdict(
            level=FAIL,
            reason="页面白屏",
            layer="frontend",
            details=[
                f"可见元素 {signal.visible_elements} 个（阈值 {config.detect.min_visible_elements}）",
                f"文本长度 {signal.body_text_length}（阈值 {config.detect.min_body_text}）",
                f"图表元素: {'有' if signal.has_media else '无'}",
            ],
        )

    # ---------- 2. 前端报错 ----------
    if config.detect.page_errors and signal.page_errors:
        return Verdict(
            level=FAIL,
            reason=f"未捕获异常 {len(signal.page_errors)} 个",
            layer="frontend",
            details=signal.page_errors[:3],
        )

    if config.detect.console_errors and signal.console_errors:
        return Verdict(
            level=FAIL,
            reason=f"控制台错误 {len(signal.console_errors)} 个",
            layer="frontend",
            details=signal.console_errors[:3],
        )

    # ---------- 3. 接口失败 ----------
    if signal.bad_requests:
        bad = signal.bad_requests[:3]
        return Verdict(
            level=FAIL,
            reason=f"接口异常 {len(signal.bad_requests)} 个",
            layer="backend",
            details=[f"{item.status} {item.method} {item.url}" for item in bad],
        )

    # ---------- 4. 关键判断：接口有数据但页面没渲染 ----------
    data_requests = [item for item in signal.requests if item.likely_has_data]
    if data_requests and signal.has_table and signal.table_rows == 0:
        # 表格为空不代表渲染失败：数据可能用图表、统计数字、卡片展示了。
        # 实测「风险监控」页就是看板——接口返回数据，页面用 ECharts 和
        # 数字卡片展示，表格只是页面的一小块。只有页面既没图表、文字也很少，
        # 才能断定数据没渲染出来。
        if signal.has_media or signal.body_text_length > 200:
            return Verdict(
                level=PASS,
                reason="正常（数据以图表/统计形式展示）",
                details=[
                    f"接口 {len(signal.requests)} 个",
                    f"图表元素: {'有' if signal.has_media else '无'}",
                    f"页面文本 {signal.body_text_length} 字",
                ],
            )
        return Verdict(
            level=FAIL,
            reason="接口返回了列表数据，但页面没有任何内容渲染",
            layer="frontend",
            details=[
                f"有数据的接口 {len(data_requests)} 个，例如 {data_requests[0].url}",
                f"表格 0 行、无图表、文本仅 {signal.body_text_length} 字",
            ],
        )

    # ---------- 5. 一个接口都没有：页面可能没初始化 ----------
    if not signal.requests:
        return Verdict(
            level=WARN,
            reason="页面没有发起任何 XHR 请求",
            layer="unknown",
            details=[
                "可能：路由没匹配、页面是纯静态、或权限拦截没渲染内容",
                f"可见元素 {signal.visible_elements}，文本 {signal.body_text_length} 字",
            ],
        )

    # ---------- 6. 空数据 ----------
    if signal.empty_hint or (signal.has_table and signal.table_rows == 0):
        if backend_logs.strip():
            return Verdict(
                level=WARN,
                reason="页面显示空数据，但后端日志里有异常",
                layer="backend",
                details=[backend_logs.strip()[:400]],
            )
        return Verdict(
            level=WARN,
            reason="页面显示空数据（未发现异常）",
            layer="unknown",
            details=["如果业务上不该为空，需要人工确认数据是否正常"],
        )

    # ---------- 7. 通过 ----------
    return Verdict(
        level=PASS,
        reason="正常",
        details=[
            f"接口 {len(signal.requests)} 个",
            f"表格行 {signal.table_rows}" if signal.has_table else "无表格",
        ],
    )


def _is_blank(signal: PageSignal, config: E2EConfig) -> bool:
    """白屏判定。

    只看文本长度会误判图表页——产业全景那种页文本只有几十字，
    内容全在 canvas/svg 里，实际是正常的。
    """
    if signal.visible_elements < config.detect.min_visible_elements:
        return True
    if (
        signal.body_text_length < config.detect.min_body_text
        and not signal.has_media
        and not signal.has_table
    ):
        return True
    return False
