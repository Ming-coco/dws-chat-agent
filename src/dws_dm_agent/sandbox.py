"""按联系人构建只包含「自己负责项目」的目录视图。

agent 的 cwd 不是安全边界（见 pi docs/security.md），所以这里用 Windows
目录联接（junction）给每个联系人搭一个沙盒目录：里面只挂载他负责的项目，
`ls ..` 只会看到自己的项目，看不到别人负责的目录。

三条硬约束：
1. 只删除自己创建的链接，绝不递归删除；普通目录一律不碰。
2. 目标目录不存在时只报错，不创建链接。
3. 所有项目名先做安全规范化，禁止路径分隔符、`..` 和 Windows 保留名。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

LOG = logging.getLogger(__name__)

# 链接状态：linked 表示已正确挂载，其余都需要用户处理。
LINK_OK = "linked"
LINK_ABSENT = "absent"      # 还没创建
LINK_MISSING = "missing"    # 目标目录不存在
LINK_STALE = "stale"        # 已有链接指向别处
LINK_BLOCKED = "blocked"    # 位置被普通文件/目录占用，或创建失败
LINK_INVALID = "invalid"    # 项目名不合法

_BROKEN = {LINK_MISSING, LINK_STALE, LINK_BLOCKED, LINK_INVALID}

_UNSAFE_ALIAS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_DASH_RUN = re.compile(r"-{2,}")
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True)
class ProjectLink:
    name: str
    target: str
    state: str
    detail: str = ""

    @property
    def healthy(self) -> bool:
        return self.state == LINK_OK

    @property
    def broken(self) -> bool:
        """还没创建的链接不算坏，只有指向错误或创建失败才算。"""
        return self.state in _BROKEN

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target": self.target,
            "state": self.state,
            "detail": self.detail,
            "healthy": self.healthy,
            "broken": self.broken,
        }


@dataclass(frozen=True)
class ContactSandbox:
    alias: str
    directory: str
    links: tuple[ProjectLink, ...]

    @property
    def healthy(self) -> bool:
        return not any(link.broken for link in self.links)

    def as_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "directory": self.directory,
            "healthy": self.healthy,
            "links": [link.as_dict() for link in self.links],
        }


@dataclass(frozen=True)
class SandboxReport:
    root: str
    contacts: tuple[ContactSandbox, ...]
    errors: tuple[str, ...]
    notes: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return not self.errors and all(item.healthy for item in self.contacts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "healthy": self.healthy,
            "errors": list(self.errors),
            "notes": list(self.notes),
            "contacts": [item.as_dict() for item in self.contacts],
        }


def slugify_alias(alias: str) -> str:
    """把联系人别名转成安全的目录名，并附短哈希保证唯一。"""
    text = str(alias or "")
    cleaned = _UNSAFE_ALIAS.sub("-", text).strip()
    cleaned = _DASH_RUN.sub("-", cleaned).strip(". -")
    if not cleaned:
        cleaned = "contact"
    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        cleaned = f"{cleaned}-contact"
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:60]}-{digest}"


def normalize_project_name(name: Any) -> str | None:
    """返回可安全用作单层目录名的项目名，不合法时返回 None。

    只接受字符串：`str(["a"])` 会得到 "['a']" 这种看着合法、实际是配置写错的
    名字，宁可在这里拒掉。
    """
    if not isinstance(name, str):
        return None
    value = name.strip()
    if not value or value in {".", ".."}:
        return None
    if any(char in value for char in '\\/:*?"<>|'):
        return None
    if any(ord(char) < 32 for char in value):
        return None
    if Path(value).name != value:
        return None
    if value.split(".")[0].upper() in _RESERVED_NAMES:
        return None
    return value


def contact_sandbox_dir(sandbox_root: Path, alias: str) -> Path:
    return sandbox_root / slugify_alias(alias)


def is_junction(path: Path) -> bool:
    """Windows junction 不是 symlink，必须看重解析点属性。"""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) and not os.path.islink(path)


def is_link(path: Path) -> bool:
    return is_junction(path) or os.path.islink(path)


def remove_link(path: Path) -> bool:
    """只摘掉链接本身，绝不触碰目标目录内容。"""
    if not os.path.lexists(path) or not is_link(path):
        return False
    try:
        os.rmdir(path)
        return True
    except OSError:
        try:
            os.unlink(path)
            return True
        except OSError as exc:  # pragma: no cover - 平台差异兜底
            LOG.warning("remove sandbox link failed path=%s error=%s", path, exc)
            return False


def create_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise OSError(detail or f"mklink exit {result.returncode}")
        return
    os.symlink(target, link, target_is_directory=True)


def _inspect_link(link: Path, target: Path) -> tuple[str, str]:
    if target.is_dir():
        target_state = LINK_OK
    else:
        target_state = LINK_MISSING
    if os.path.lexists(link):
        if not is_link(link):
            return LINK_BLOCKED, "该路径已被普通文件或目录占用"
        try:
            current = Path(link).resolve()
        except OSError:
            current = Path()
        if current != target.resolve():
            return LINK_STALE, f"当前指向 {current}"
        return target_state, "" if target_state == LINK_OK else "目标目录不存在"
    return target_state if target_state != LINK_OK else LINK_ABSENT, (
        "目标目录不存在" if target_state == LINK_MISSING else ""
    )


def sync_contact(
    sandbox_root: Path,
    workspace_root: Path,
    alias: str,
    projects: Sequence[str],
    *,
    apply: bool,
) -> ContactSandbox:
    """按项目列表对齐单个联系人的沙盒目录。apply=False 时只检查不落盘。"""
    directory = contact_sandbox_dir(sandbox_root, alias)
    links: list[ProjectLink] = []
    desired: dict[str, Path] = {}

    if apply:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ContactSandbox(
                alias=alias,
                directory=str(directory),
                links=(
                    ProjectLink(
                        "", "", LINK_BLOCKED, f"无法创建沙盒目录：{exc}"
                    ),
                ),
            )

    for raw_name in projects:
        name = normalize_project_name(raw_name)
        if name is None:
            links.append(
                ProjectLink(
                    str(raw_name), "", LINK_INVALID, "项目名含非法字符或路径分隔符"
                )
            )
            continue
        target = workspace_root / name
        desired[name] = target
        state, detail = _inspect_link(directory / name, target)
        if state == LINK_ABSENT and apply:
            try:
                create_link(directory / name, target)
                state, detail = LINK_OK, ""
            except OSError as exc:
                state, detail = LINK_BLOCKED, f"创建链接失败：{exc}"
        elif state == LINK_STALE and apply:
            # 指向别处的旧链接先摘掉再重建，不影响目标目录。
            if remove_link(directory / name):
                try:
                    create_link(directory / name, target)
                    state, detail = LINK_OK, ""
                except OSError as exc:
                    state, detail = LINK_BLOCKED, f"重建链接失败：{exc}"
            else:
                state, detail = LINK_BLOCKED, "无法移除旧链接"
        links.append(ProjectLink(name, str(target), state, detail))

    if apply and directory.is_dir():
        keep = set(desired)
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            if entry.name in keep:
                continue
            if is_link(entry):
                remove_link(entry)
            else:
                # 不是我们建的，保留并告警，避免误删用户文件。
                LOG.warning("sandbox keeps unexpected entry path=%s", entry)

    return ContactSandbox(alias=alias, directory=str(directory), links=tuple(links))


def rebuild(
    workspace_root: Path,
    sandbox_root: Path,
    assignments: Iterable[tuple[str, Sequence[str]]],
    *,
    apply: bool,
) -> SandboxReport:
    """按 (别名, 项目列表) 重建全部沙盒；apply=False 时只出报告。"""
    contacts: list[ContactSandbox] = []
    errors: list[str] = []
    notes: list[str] = []

    if apply:
        try:
            sandbox_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            errors.append(f"无法创建沙盒根目录 {sandbox_root}：{exc}")
            return SandboxReport(str(sandbox_root), (), tuple(errors))

    seen_dirs: dict[str, str] = {}
    for alias, projects in assignments:
        directory = contact_sandbox_dir(sandbox_root, alias)
        key = str(directory).lower()
        if key in seen_dirs:
            errors.append(f"联系人 {alias} 与 {seen_dirs[key]} 的沙盒目录重名")
            continue
        seen_dirs[key] = alias
        if not projects:
            # 仍然清一遍目录，避免旧链接留着误导；同时提示会退回完整工作区。
            contacts.append(
                sync_contact(sandbox_root, workspace_root, alias, (), apply=apply)
            )
            notes.append(
                f"{alias} 未分配项目，将直接使用完整工作目录（不做目录隔离）"
            )
            continue
        sandbox = sync_contact(
            sandbox_root, workspace_root, alias, projects, apply=apply
        )
        contacts.append(sandbox)
        for link in sandbox.links:
            if link.broken:
                errors.append(
                    f"{alias} / {link.name}：{link.detail or link.state}"
                )

    return SandboxReport(str(sandbox_root), tuple(contacts), tuple(errors), tuple(notes))


def primary_workspace(
    sandbox_root: Path,
    workspace_root: Path,
    alias: str,
    projects: Sequence[str],
) -> Path:
    """agent 的实际 cwd：沙盒里的第一个项目；没有项目时退回 workspace_root。"""
    valid = [name for name in (normalize_project_name(item) for item in projects) if name]
    if not valid:
        return workspace_root
    return contact_sandbox_dir(sandbox_root, alias) / valid[0]
