"""单元测试：完整路径拼接。

真实数据来自江门 UAT 的菜单接口，路径规则来自实测（哪些能打开、哪些 404）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from e2e_runner.discover import _build_nodes, _join_path


def test_join_path() -> int:
    cases = [
        # 江门实测：一级 url + 叶子 url
        ("/mine", "/collections/index", "/mine/collections/index", "关注企业"),
        ("/mine", "/watch-list", "/mine/watch-list", "监控企业"),
        ("/mine", "/myList/index", "/mine/myList/index", "关注名单"),
        ("/mine", "/person/personCenter", "/mine/person/personCenter", "个人中心"),
        ("/fx-marketing-customer-front", "/marketing/index",
         "/fx-marketing-customer-front/marketing/index", "标签筛客"),
        ("/organization", "/role", "/organization/role", "角色管理"),
        # 子路径已含父路径时不重复拼
        ("/organization", "/organization/role", "/organization/role", "已完整"),
        # 边界
        ("", "/home-page", "/home-page", "父为空"),
        ("/a", "", "/a", "子为空"),
        ("/a", "https://x.com/y", "https://x.com/y", "外部链接"),
    ]
    failed = 0
    for parent, child, expected, note in cases:
        actual = _join_path(parent, child)
        if actual != expected:
            failed += 1
            print(f"✗ {note}: parent={parent!r} child={child!r}")
            print(f"   期望 {expected!r}，实际 {actual!r}")
        else:
            print(f"✓ {note}")
    return failed


def test_build_nodes() -> int:
    """三层嵌套时，叶子用的是「一级 + 叶子」，不是逐层拼。"""
    payload = [
        {
            "name": "我的",
            "url": "/mine",
            "type": "menu",
            "children": [
                {
                    "name": "我的",
                    "url": "/collections/index",
                    "type": "menu",
                    "children": [
                        {
                            "name": "关注企业",
                            "url": "/collections/index",
                            "type": "menu",
                            "children": [],
                        },
                        {
                            "name": "监控企业",
                            "url": "/watch-list",
                            "type": "menu",
                            "children": [],
                        },
                    ],
                },
            ],
        }
    ]
    nodes = _build_nodes(payload)
    # 按「树里的位置」取，而不是按名字（一级和二级都叫「我的」会互相覆盖）
    top = nodes[0]
    middle = top.children[0]
    leaves = {node.name: node.full_path for node in middle.children}

    failed = 0
    checks = [
        ("一级「我的」", top.full_path, "/mine"),
        ("二级「我的」", middle.full_path, "/mine/collections/index"),
        ("关注企业", leaves.get("关注企业"), "/mine/collections/index"),
        ("监控企业", leaves.get("监控企业"), "/mine/watch-list"),
    ]
    for label, actual, want in checks:
        if actual != want:
            failed += 1
            print(f"✗ {label}: 期望 {want!r}，实际 {actual!r}")
        else:
            print(f"✓ {label}: {actual}")

    if not failed:
        print("✓ 三层嵌套：叶子用一级+叶子拼接")
    return failed


def main() -> int:
    print("=== _join_path ===")
    failed = test_join_path()
    print("\n=== _build_nodes ===")
    failed += test_build_nodes()
    print()
    print("全部通过" if not failed else f"{failed} 个失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
