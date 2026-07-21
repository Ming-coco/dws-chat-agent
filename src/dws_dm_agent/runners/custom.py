from __future__ import annotations

from typing import Any


def command(profile: Any) -> list[str]:
    return [*profile.command, *profile.extra_args]
