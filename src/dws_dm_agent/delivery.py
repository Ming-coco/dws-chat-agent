from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Sequence

from .core import ChangeEvidence


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


_SENSITIVE_DELIVERY_NAMES = {"playwright.env", "id_rsa", "id_ed25519"}
_SENSITIVE_DELIVERY_STEMS = {"credential", "credentials", "secret", "secrets", "token", "tokens"}


def _is_sensitive_delivery_file(path: Path) -> bool:
    name = path.name.lower()
    return (
        name == ".env"
        or name.startswith(".env.")
        or name in _SENSITIVE_DELIVERY_NAMES
        or path.suffix.lower() in {".pem", ".key"}
        or name.split(".", 1)[0] in _SENSITIVE_DELIVERY_STEMS
    )


def build_change_archive(
    changes: Sequence[ChangeEvidence], destination: Path, max_bytes: int = 50 * 1024 * 1024
) -> Path:
    """Create a bounded archive containing only declared, verified changed files."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[Path, str]] = []
    total_bytes = 0
    for change in changes:
        if not change.verified:
            raise ValueError("cannot attach unverified code changes")
        root = Path(change.worktree).resolve()
        repo = Path(change.repo).name or root.name
        for relative_text in change.files:
            relative = Path(relative_text)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe attachment path: {relative_text}")
            source = root / relative
            resolved = source.resolve()
            if source.is_symlink() or not _is_beneath(resolved, root) or not resolved.is_file():
                raise ValueError(f"attachment is not a regular worktree file: {relative_text}")
            normalized = relative.as_posix()
            if _is_sensitive_delivery_file(relative):
                raise ValueError(f"sensitive file cannot be attached: {relative_text}")
            total_bytes += resolved.stat().st_size
            if total_bytes > max_bytes:
                raise ValueError("attachment exceeds size limit")
            entries.append((resolved, f"{repo}/{normalized}"))
    if not entries:
        raise ValueError("no verified changed files to attach")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, archive_name in entries:
            archive.write(source, archive_name)
    return destination
