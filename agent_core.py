"""Compatibility imports; use :mod:`dws_dm_agent.core` in new code."""

from pathlib import Path
import sys

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dws_dm_agent.core import *  # noqa: F401,F403,E402
