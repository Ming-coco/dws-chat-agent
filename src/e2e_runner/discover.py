"""菜单发现：独立能力。

输入：一个已登录的页面
输出：菜单树 + 证据，或「需要确认」的结构化结果

它不负责测试页面，只负责搞清楚「这个系统有哪些页面可测」。

三级策略（逐级降级，每级都有证据）：
  1. 拦截接口 —— 菜单不管来自代码/接口/数据库，最终都要经过 HTTP 下发
  2. 读 DOM     —— 没有菜单接口时，从页面结构推断
  3. 人工确认   —— 以上都不确定时，把候选和证据一起交给人

绝不静默返回空结果：发现不到就报错，找不到就要求确认。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Page

from .config import E2EConfig

# 名称字段的候选名。不同系统叫法不同。
_NAME_KEYS = ("name", "menuName", "title", "menuTitle", "label", "text")
# 路径字段的候选名。有它才可能是可导航菜单。
_PATH_KEYS = ("url", "router", "path", "route", "href", "link")
# 类型字段：用来排除按钮权限
_TYPE_KEYS = ("type", "menuType", "nodeType", "category")
# 子节点字段
_CHILD_KEYS = ("children", "childList", "subList", "subMenu", "child", "items")

# 数量合理性：太像列表或太少的候选要打问号
_MIN_ITEMS = 3
_MAX_ITEMS = 60


@dataclass
class MenuNode:
    """菜单树节点。"""

    name: str
    url: str = ""
    # menu / button / other
    kind: str = "menu"
    children: list["MenuNode"] = field(default_factory=list)
    # 累加后的完整路径（父 url + 自身 url）
    full_path: str = ""
    # 来源证据：来自哪个接口的哪个字段
    evidence: str = ""

    def walk(self) -> list["MenuNode"]:
        nodes = [self]
        for child in self.children:
            nodes.extend(child.walk())
        return nodes

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "kind": self.kind,
            "fullPath": self.full_path,
            "evidence": self.evidence,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass
class Candidate:
    """一个候选菜单来源。"""

    source: str  # api | dom
    label: str  # 接口 URL 或 "DOM"
    nodes: list[MenuNode]
    # 顶层菜单数
    top_count: int
    # 有 url 的节点数
    url_count: int
    # 验证结果：ok / failed / skipped
    verified: str = "skipped"
    verify_detail: str = ""

    @property
    def score(self) -> int:
        """打分：有 URL 的越多越像菜单。

        真正的菜单一定有可导航路径；分类树、组织树通常没有。
        """
        score = self.url_count * 2 + self.top_count
        if self.verified == "ok":
            score += 50
        elif self.verified == "failed":
            score -= 100
        return score


@dataclass
class DiscoveryResult:
    """发现结果。status 决定调用方怎么处理。"""

    status: str  # ok | needs_confirmation | not_found
    menus: list[MenuNode] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    message: str = ""
    # 建议采用哪个候选
    suggested: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "suggested": self.suggested,
            "menuCount": len(self.menus),
            "menus": [node.to_dict() for node in self.menus],
            "candidates": [
                {
                    "source": item.source,
                    "label": item.label,
                    "topCount": item.top_count,
                    "urlCount": item.url_count,
                    "verified": item.verified,
                    "verifyDetail": item.verify_detail,
                    "score": item.score,
                    "sample": [node.name for node in item.nodes[:5]],
                }
                for item in self.candidates
            ],
        }


class MenuApiCollector:
    """挂监听，收集所有可能是菜单的接口响应。

    要在登录之前挂，否则漏掉登录后立即请求的菜单接口。
    """

    def __init__(self, page: Page, config: E2EConfig) -> None:
        self.page = page
        self.config = config
        self.candidates: list[Candidate] = []
        page.on("response", self._on_response)

    def _on_response(self, response: Any) -> None:  # noqa: ANN401
        try:
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            headers = response.headers or {}
            if "json" not in (headers.get("content-type", "")).lower():
                return
            body = response.body()
            if not (30 < len(body) < 3_000_000):
                return
            payload = json.loads(body.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return

        url = response.url.split("?")[0]
        for found in _extract_menu_lists(payload):
            nodes = _build_nodes(found)
            if len(nodes) < _MIN_ITEMS:
                continue
            url_count = sum(1 for node in nodes for _ in [0] if node.full_path and node.url)
            self.candidates.append(
                Candidate(
                    source="api",
                    label=url,
                    nodes=nodes,
                    top_count=len(nodes),
                    url_count=sum(
                        1 for node in nodes for item in node.walk() if item.url
                    ),
                )
            )


def _extract_menu_lists(payload: Any) -> list[list[dict]]:
    """在任意 JSON 里找到「像菜单列表」的数组。

    判据：数组元素是对象，且有名称字段 + （路径字段或子节点字段）。
    """
    results: list[list[dict]] = []

    def walk(node: Any, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(node, list):
            if node and all(isinstance(item, dict) for item in node[:5]):
                if _looks_like_menu_list(node):
                    results.append(node)
            for item in node:
                walk(item, depth + 1)
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(value, depth + 1)

    walk(payload, 0)
    return results


def _looks_like_menu_list(items: list[dict]) -> bool:
    """判断一个数组是否像菜单列表。"""
    first = items[0]
    keys = set(first.keys())
    has_name = any(key in keys for key in _NAME_KEYS)
    if not has_name:
        return False
    has_path = any(key in keys for key in _PATH_KEYS)
    has_children = any(key in keys for key in _CHILD_KEYS)
    return has_path or has_children


def _build_nodes(
    items: list[dict],
    depth: int = 0,
    parent_path: str = "",
    root_path: str = "",
) -> list[MenuNode]:
    """把接口数据转成节点树。

    root_path 是「一级菜单的路径」，所有后代都用它来拼完整路径——
    因为前端实际是「一级 href + 点击的二级 submenu」这样拼的，
    中间层不参与。
    """
    nodes: list[MenuNode] = []
    if depth > 8:
        return nodes
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _first_str(item, _NAME_KEYS)
        if not name:
            continue
        url = _first_str(item, _PATH_KEYS)
        kind = _first_str(item, _TYPE_KEYS).lower() or "menu"

        # 一级菜单：自己就是 root
        effective_root = root_path or url
        full = _join_path(effective_root, url) if effective_root else url

        node = MenuNode(
            name=name[:40],
            url=url[:200],
            kind=kind,
            full_path=full,
        )

        for child_key in _CHILD_KEYS:
            raw_children = item.get(child_key)
            if isinstance(raw_children, list) and raw_children:
                node.children = _build_nodes(
                    raw_children,
                    depth + 1,
                    parent_path=full,
                    root_path=effective_root,
                )
                break

        nodes.append(node)
    return nodes


def _first_str(item: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _join_path(parent: str, child: str) -> str:
    """拼出可访问的完整路径。

    实测结论（实测环境 验证）：完整路径 = 一级菜单 url + 叶子菜单 url。
    中间的层级不参与拼接——因为前端是这么拼的：

        this.$router.push(menu.href + this.activeMenu.submenu)
        // menu.href 是一级菜单，submenu 是当前选中的二级菜单

    逐层递归拼接会出错。实测：
        我的 /mine + 我的 /collections/index + 关注企业 /collections/index
        逐层拼 => /mine/collections/index/collections/index   ✗ 404
        一级+叶子 => /mine/collections/index                  ✓ 正确

    所以调用方应该传「一级菜单 url」作为 parent。本函数负责的只是
    把两段拼起来，并处理「子路径已含父路径」的情况。
    """
    if not child:
        return parent
    if child.startswith("http"):
        return child
    if not parent:
        return child if child.startswith("/") else "/" + child

    parent_norm = parent.rstrip("/").split("?")[0]
    child_path = child.split("?")[0]

    # 子路径已经包含父路径 → 自解释，不再拼接
    if child_path == parent_norm or child_path.startswith(parent_norm + "/"):
        return child

    if child.startswith("/"):
        return parent_norm + child
    return parent_norm + "/" + child


def select_candidate(candidates: list[Candidate]) -> DiscoveryResult:
    """从候选里挑出菜单，或给出需要确认的理由。"""
    if not candidates:
        return DiscoveryResult(
            status="not_found",
            message=(
                "没有找到像菜单的接口响应。可能原因：\n"
                "  1. 菜单写死在前端代码里（没有接口下发）\n"
                "  2. 菜单接口在登录之前就已经请求完成\n"
                "  3. 当前账号没有菜单权限\n"
                "可以改用 DOM 推断模式，或手工配置菜单。"
            ),
        )

    # 去掉完全重复的（同一接口可能被请求两次）
    unique: list[Candidate] = []
    seen: set[str] = set()
    for item in candidates:
        key = f"{item.label}|{item.top_count}|{item.url_count}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    ranked = sorted(unique, key=lambda item: item.score, reverse=True)

    if len(ranked) == 1:
        best = ranked[0]
        if best.url_count == 0:
            return DiscoveryResult(
                status="needs_confirmation",
                candidates=ranked,
                suggested=0,
                message=(
                    f"只找到 1 个候选（{best.label}），但它没有任何 URL 字段，"
                    "可能是分类树或组织树而不是菜单。请确认。"
                ),
            )
        return DiscoveryResult(
            status="ok",
            menus=best.nodes,
            candidates=ranked,
            suggested=0,
            message=f"从 {best.label} 发现 {best.top_count} 个一级菜单",
        )

    # 多个候选：比较得分
    best = ranked[0]
    second = ranked[1]
    if best.score >= second.score + 30:
        return DiscoveryResult(
            status="ok",
            menus=best.nodes,
            candidates=ranked,
            suggested=0,
            message=(
                f"有 {len(ranked)} 个候选，选得分最高的 {best.label}"
                f"（{best.top_count} 个一级菜单，{best.url_count} 个带 URL）"
            ),
        )

    return DiscoveryResult(
        status="needs_confirmation",
        candidates=ranked,
        suggested=0,
        message=(
            f"找到 {len(ranked)} 个候选，得分接近，无法确定哪个是菜单。"
            "请确认使用哪一个。"
        ),
    )
