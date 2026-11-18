"""抓远端容器日志。

用途就一个：页面显示空数据时，判断是"真的没数据"还是"后端报错被吞了"。
后者光看页面看不出来，必须去日志里找。

连不上不阻断测试——抓日志是辅助证据，不是必要条件。
"""

from __future__ import annotations

import re
from functools import lru_cache

from .config import E2EConfig

# 单次抓取的日志行数上限，避免把整个日志拉下来
TAIL_LINES = 200
# 返回给报告的错误片段长度
MAX_EXCERPT = 600


def fetch_recent_errors(config: E2EConfig, page_url: str) -> str:
    """抓页面相关容器最近的错误日志。"""
    container = _resolve_container(config, page_url)
    if not container:
        return ""

    command = (
        f"docker logs --tail {TAIL_LINES} {container} 2>&1 "
        f"| grep -iE '{config.logs.error_pattern}' | tail -20"
    )
    output = _ssh_exec(config, command)
    if not output.strip():
        return ""
    return output.strip()[:MAX_EXCERPT]


def _resolve_container(config: E2EConfig, page_url: str) -> str:
    """按页面路径匹配容器名。配置里没有匹配项返回空串。"""
    best = ""
    best_length = 0
    for prefix, container in config.logs.container_map.items():
        if prefix and prefix in page_url and len(prefix) > best_length:
            best = container
            best_length = len(prefix)
    return best


@lru_cache(maxsize=8)
def _client(host: str, user: str, password: str):  # noqa: ANN202
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, timeout=15)
    return client


def _ssh_exec(config: E2EConfig, command: str) -> str:
    host = config.logs.ssh_host
    user = config.logs.ssh_user
    password = config.logs.ssh_password
    if not (host and user):
        return ""
    try:
        client = _client(host, user, password)
        _, stdout, _ = client.exec_command(command, timeout=30)
        return stdout.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001  日志抓不到不影响测试结论
        return ""
