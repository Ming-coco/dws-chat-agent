from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .config import DEFAULT_CONFIG, DEFAULT_ENV, load_settings
from .runners.runtime import AgentRuntime


def load_cases(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value:
        raise ValueError("front eval cases must be a non-empty array")
    cases: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("front eval case must be an object")
        case = dict(raw)
        name = str(case.get("name") or "")
        message = str(case.get("message") or "")
        route = str(case.get("expected_route") or "")
        if not name or name in names or not message or route not in {"reply", "worker"}:
            raise ValueError(f"invalid front eval case: {name or '(unnamed)'}")
        recent = case.get("recent_context", [])
        if not isinstance(recent, list) or any(
            not isinstance(item, Mapping) for item in recent
        ):
            raise ValueError(f"invalid recent_context in front eval case: {name}")
        names.add(name)
        cases.append(case)
    return cases


def matches_expected(reply: str, escalated: bool, case: Mapping[str, Any]) -> bool:
    route = "worker" if escalated else "reply"
    return route == case.get("expected_route") and (escalated or bool(reply.strip()))


async def _evaluate_case(
    runtime: AgentRuntime,
    settings: Any,
    case: Mapping[str, Any],
    index: int,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        session_id = f"front-eval-{index:02d}"
        now = datetime.now(settings.timezone)
        recent = [
            {
                "role": str(item.get("role") or "contact"),
                "sender": settings.self_name
                if item.get("role") == "agent"
                else "测试联系人",
                "time": str(item.get("time") or now.isoformat()),
                "message_id": f"{session_id}-history-{offset}",
                "conversation_id": "front-eval",
                "text": str(item.get("text") or ""),
            }
            for offset, item in enumerate(case.get("recent_context", []), 1)
        ]
        current = [
            {
                "role": "contact",
                "time": now.isoformat(),
                "message_id": f"{session_id}-message",
                "conversation_id": "front-eval",
                "text": str(case["message"]),
            }
        ]
        prompt = runtime.render(
            "front",
            {
                "self_name": settings.self_name,
                "agent_name": runtime.profile_name("front"),
                "worker_name": runtime.profile_name("worker"),
                "contact_name": "测试联系人",
                "contact_alias": "front-eval",
                "contact_user_id": "front-eval-user",
                "contact_open_dingtalk_id": "front-eval-open-id",
                "session_id": session_id,
                "prior_attempt": "无",
                "project_root": settings.workspace_root,
                "projects_json": "[]",
                "recent_context_json": json.dumps(
                    recent, ensure_ascii=False, indent=2
                ),
                "current_messages_json": json.dumps(
                    current, ensure_ascii=False, indent=2
                ),
                # 评测联系人不带事项记忆：注入空快照，保持提示词变量契约完整。
                "active_tasks_json": "[]",
                "now": now.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        started = time.monotonic()
        reply = ""
        escalated = False
        error = ""
        with tempfile.TemporaryDirectory(prefix=f"{session_id}-") as directory:
            session = runtime.open_session(
                "front", session_id, prompt, Path(directory)
            )
            try:
                await session.start()
                timeout = session.prepared.timeout_seconds
                await session.wait(timeout=timeout + 5 if timeout > 0 else None)
                if session.error:
                    raise RuntimeError(session.error)
                reply = session.result()
                escalated = session.escalated
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            finally:
                await session.close()
        return {
            "name": case["name"],
            "passed": not error and matches_expected(reply, escalated, case),
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": error,
            "expected": {
                "route": case["expected_route"],
            },
            "actual": {
                "route": "worker" if escalated else "reply",
                "reply": reply[:120],
            },
        }


async def evaluate(
    config_path: Path,
    env_path: Path,
    cases_path: Path,
    concurrency: int,
) -> list[dict[str, Any]]:
    settings = load_settings(config_path, env_path)
    runtime = AgentRuntime.from_config(
        settings.raw, settings.config_path, settings.workspace_root
    )
    cases = load_cases(cases_path)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    return await asyncio.gather(
        *(
            _evaluate_case(runtime, settings, case, index, semaphore)
            for index, case in enumerate(cases, 1)
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay front-agent routing cases")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CONFIG.parent / "evals" / "front-routing.json",
    )
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--validate-only", action="store_true")
    arguments = parser.parse_args()
    if arguments.validate_only:
        cases = load_cases(arguments.cases)
        print(json.dumps({"valid": True, "cases": len(cases)}, ensure_ascii=False))
        return 0
    results = asyncio.run(
        evaluate(
            arguments.config,
            arguments.env_file,
            arguments.cases,
            arguments.concurrency,
        )
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
