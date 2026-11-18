"""E2E 页面体检工具。

部署后自动验证 Web 系统是否正常：登录 → 发现菜单 → 逐页访问 → 三层归因 → 出报告。

与 dws-chat-agent 同仓但完全独立：不导入 agent 的任何模块，
可单独执行 `python -m e2e_runner run --config jiangmen-uat`。
"""

from __future__ import annotations

__version__ = "0.1.0"
