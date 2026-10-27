from __future__ import annotations

import re
from typing import Any, Mapping


_PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")

STAGE_PROMPT_VARIABLES = frozenset(
    {
        "self_name", "agent_name", "worker_name", "contact_name",
        "contact_alias", "contact_user_id", "contact_open_dingtalk_id",
        "session_id", "prior_attempt",
        # project_root 是 agent 的 cwd（沙盒内主项目）；projects_json 只含
        # 该联系人自己负责的项目。刻意不暴露 workspace_root / worktree_root：
        # 它们是容器路径，写进提示词等于提示模型去浏览别的项目。
        "project_root", "projects_json", "recent_context_json",
        "current_messages_json", "active_tasks_json", "now",
    }
)
SUPPLEMENT_PROMPT_VARIABLES = frozenset(
    {
        "supplement_messages_json", "session_id", "contact_name",
        "contact_alias", "self_name",
    }
)


def render_prompt(template: str, variables: Mapping[str, Any]) -> str:
    missing = sorted(set(_PLACEHOLDER.findall(template)) - set(variables))
    if missing:
        raise ValueError(f"missing prompt variables: {', '.join(missing)}")
    return _PLACEHOLDER.sub(lambda match: str(variables[match.group(1)]), template)


def validate_template_variables(template: str, allowed: frozenset[str]) -> None:
    unknown = sorted(set(_PLACEHOLDER.findall(template)) - allowed)
    if unknown:
        raise ValueError(f"unsupported prompt variables: {', '.join(unknown)}")
