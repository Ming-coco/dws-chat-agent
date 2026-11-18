"""命令行入口。

  python -m e2e_runner discover --config jiangmen-uat        # 发现菜单
  python -m e2e_runner run      --config jiangmen-uat        # 跑体检
  python -m e2e_runner run      --config jiangmen-uat --json # 输出 JSON 给 agent 用

退出码：0 通过 / 1 有失败 / 2 配置错误 / 3 登录失败 / 4 菜单发现失败
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from .browser import BrowserSession
from .config import ConfigError, E2EConfig, load_config
from .detect import FAIL
from .discover import DiscoveryResult, MenuApiCollector, select_candidate
from .login import LoginError, login
from .navigate import PageResult, RunResult, collect_targets, visit
from .report import console_summary, to_dict, write_json, write_markdown

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs"
OUTPUT_ROOT = Path(__file__).resolve().parents[2] / "build" / "e2e-runs"


def _load(name: str) -> E2EConfig:
    path = Path(name)
    if not path.suffix:
        path = DEFAULT_CONFIG_DIR / f"{name}.json"
    return load_config(path)


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _slug(text: str) -> str:
    import re

    return re.sub(r'[\\/:*?"<>|\s]+', "-", text)[:30]


def _discover(session: BrowserSession, config: E2EConfig) -> DiscoveryResult:
    """登录并发现菜单。必须在登录前挂监听，否则漏掉首次请求。"""
    page = session.page
    assert page is not None
    collector = MenuApiCollector(page, config)
    login(page, config)
    page.wait_for_timeout(3500)
    return select_candidate(collector.candidates)


def cmd_discover(args: argparse.Namespace) -> int:
    """发现菜单并打印。接入新系统时用。"""
    try:
        config = _load(args.config)
    except ConfigError as error:
        print(f"配置错误: {error}", file=sys.stderr)
        return 2

    try:
        with BrowserSession(config) as session:
            result = _discover(session, config)
    except LoginError as error:
        print(f"登录失败: {error}", file=sys.stderr)
        return 3

    payload = result.to_dict()

    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"状态: {result.status}")
        print(f"说明: {result.message}\n")
        if result.candidates:
            print("候选来源:")
            for index, item in enumerate(result.candidates[:6]):
                mark = " ← 建议" if index == result.suggested else ""
                print(
                    f"  [{index}] {item.label}  "
                    f"一级 {item.top_count}，带路径 {item.url_count}，得分 {item.score}{mark}"
                )
        if result.menus:
            print(f"\n菜单树（{len(result.menus)} 个一级）：")
            _print_tree(result.menus)

    if args.out:
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已写入 {args.out}")

    # 需要人工确认时给不同退出码，agent 据此决定是否问人
    if result.status == "ok":
        return 0
    if result.status == "needs_confirmation":
        return 5
    return 4


def _print_tree(nodes, depth: int = 0) -> None:  # noqa: ANN001
    for node in nodes:
        indent = "  " * depth
        kind = "" if node.kind == "menu" else f" [{node.kind}]"
        path = f"  {node.full_path}" if node.full_path else ""
        print(f"{indent}- {node.name}{kind}{path}")
        if node.children:
            _print_tree(node.children, depth + 1)


def cmd_run(args: argparse.Namespace) -> int:
    """跑一轮体检。"""
    try:
        config = _load(args.config)
    except ConfigError as error:
        print(f"配置错误: {error}", file=sys.stderr)
        return 2

    stamp = _timestamp()
    run_dir = OUTPUT_ROOT / f"{stamp}-{_slug(config.name)}"
    shot_dir = run_dir / "shots"
    shot_dir.mkdir(parents=True, exist_ok=True)

    result = RunResult(
        name=config.name, started_at=datetime.now().isoformat(timespec="seconds")
    )
    started = time.time()

    try:
        with BrowserSession(config) as session:
            discovery = _discover(session, config)

            if discovery.status == "not_found":
                result.error = f"菜单发现失败: {discovery.message}"
            elif discovery.status == "needs_confirmation":
                result.error = (
                    f"菜单发现需要确认: {discovery.message}\n"
                    + json.dumps(discovery.to_dict()["candidates"], ensure_ascii=False)
                )
            else:
                result.menu_source = (
                    discovery.candidates[0].label if discovery.candidates else ""
                )
                result.menu_top_count = len(discovery.menus)

                targets = collect_targets(discovery.menus)
                if args.only:
                    targets = [
                        item for item in targets if args.only in item[0]
                    ]
                if not targets:
                    result.error = "没有可测页面（菜单里没有带路径的 menu 节点）"

                for display_name, path in targets:
                    try:
                        result.pages.append(
                            visit(session, config, display_name, path, shot_dir)
                        )
                    except Exception as error:  # noqa: BLE001 单页失败不影响后续
                        result.pages.append(
                            PageResult(
                                name=display_name,
                                path=path,
                                signal=_empty_signal(),
                                verdict=_manual_verdict(
                                    FAIL,
                                    f"采集异常: {type(error).__name__}",
                                    "unknown",
                                    [str(error)[:200]],
                                ),
                            )
                        )
    except LoginError as error:
        result.error = f"登录失败: {error}"
    except Exception as error:  # noqa: BLE001
        result.error = f"{type(error).__name__}: {error}"

    result.duration_ms = int((time.time() - started) * 1000)

    write_json(result, run_dir / "result.json")
    write_markdown(result, run_dir / "summary.md")

    if args.json:
        print(json.dumps(to_dict(result), ensure_ascii=False))
    else:
        print(console_summary(result))
        print(f"\n报告目录: {run_dir}")

    return 0 if result.overall != FAIL else 1


def _empty_signal():  # noqa: ANN202
    from .browser import PageSignal

    return PageSignal(url="")


def _manual_verdict(level: str, reason: str, layer: str, details: list[str]):  # noqa: ANN202
    from .detect import Verdict

    return Verdict(level=level, reason=reason, layer=layer, details=details)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="e2e_runner", description="部署后页面体检：登录 → 发现 → 遍历 → 归因"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    discover_parser = sub.add_parser("discover", help="发现菜单")
    discover_parser.add_argument("--config", required=True, help="配置名或路径")
    discover_parser.add_argument("--out", default="", help="结果写入文件")
    discover_parser.add_argument("--json", action="store_true", help="输出 JSON")
    discover_parser.set_defaults(func=cmd_discover)

    run_parser = sub.add_parser("run", help="执行体检")
    run_parser.add_argument("--config", required=True, help="配置名或路径")
    run_parser.add_argument("--only", default="", help="只测名字包含该关键字的页面")
    run_parser.add_argument("--json", action="store_true", help="输出 JSON 到 stdout")
    run_parser.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
