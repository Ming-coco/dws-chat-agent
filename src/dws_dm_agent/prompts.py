from __future__ import annotations

import re
from typing import Any, Mapping


_PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")

STAGE_PROMPT_VARIABLES = frozenset(
    {
        "self_name", "agent_name", "worker_name", "contact_name",
        "contact_alias", "contact_user_id", "session_id", "prior_attempt",
        "workspace_root", "worktree_root", "recent_context_json",
        "current_messages_json", "execution_domains_json",
        "reference_domains_json", "execution_mode", "execution_rule", "now",
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
