"""Standalone check: does the @-mention listener stay connected?

Runs the real `dws event consume user_im_message_receive_at` subscription
through AgentService._listen_mentions with no database and no agent, then
reports whether the process survived past the handshake. This also covers the
stdin pipe fix: the CLI treats stdin EOF as an exit signal, so a listener
started with a closed stdin would exit immediately after becoming ready.

Usage: python scripts/verify_mention_listener.py <config.json> [<env file>]
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dws_dm_agent.config import load_settings
from dws_dm_agent.service import AgentService

SECONDS = 25


async def main() -> int:
    settings = load_settings(
        Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None
    )
    print(f"groups={len(settings.groups)} targets={len(settings.targets)}")
    service = object.__new__(AgentService)
    service.settings = settings
    service.stop_event = asyncio.Event()
    service.listener_processes = set()
    service.queue = asyncio.Queue()
    service._ignored_conversations = set()

    task = asyncio.create_task(service._listen_mentions())
    started = time.monotonic()
    alive_samples = 0
    while time.monotonic() - started < SECONDS:
        await asyncio.sleep(1)
        if task.done():
            break
        alive_samples += 1

    still_running = not task.done()
    processes = len(service.listener_processes)
    service.stop_event.set()
    for process in list(service.listener_processes):
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass

    print(f"listener_alive_after_{SECONDS}s={still_running}")
    print(f"alive_samples={alive_samples} live_processes={processes}")
    if task.done() and not still_running:
        try:
            exc = task.exception()
        except Exception:  # noqa: BLE001
            exc = None
        if exc is not None:
            print(f"listener_raised={type(exc).__name__}: {exc}")
    return 0 if still_running else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
