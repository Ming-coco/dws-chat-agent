#!/usr/bin/env python3
"""Compatibility entry point for existing launchd and one-click installs."""

from pathlib import Path
import sys

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dws_dm_agent.service import *  # noqa: F401,F403,E402
from dws_dm_agent.service import (  # noqa: E402
    _find_dws_error_code,
    _load_timezone,
    main,
)


if __name__ == "__main__":
    raise SystemExit(main())
