# PROJECT D.I.A.M.O.N.D.

*DingTalk-Integrated Agent for Messaging, Orchestration & Networked Delegation*

[English](README.md) | [简体中文](README.zh-CN.md)

一个用于协调本地 AI 编码 Agent 的钉钉自主交互层。它只监听明确的白名单，能够直接回复私聊，也能把实施任务交给执行 Agent。前台和执行 Agent 均不绑定具体产品，支持 Codex、Claude Code、Pi 和轻量自定义适配器。

服务运行在操作者自己的电脑上，复用现有 DWS 和编码 Agent 登录。个人身份、联系人 ID、本地路径和内部域名保存在已忽略的 `.env` 文件中。

## 让 Agent 帮你配置

把下面这句话复制给目标电脑上的编码 Agent：

> 帮我安装并配置 DWS Chat Agent：https://raw.githubusercontent.com/inoryirony/dws-chat-agent/main/docs/agent-config.md

已经安装过？同一份指南也支持安全检查和更新：

> 帮我检查并更新 DWS Chat Agent：https://raw.githubusercontent.com/inoryirony/dws-chat-agent/main/docs/agent-config.md

## 运行流程

```text
钉钉事件
  -> 零 token 粗筛
  -> 按会话排队和串行锁
  -> 稳定并合并最近消息
  -> 可选前台 Agent（运行时只读）
       -> 自然语言直接回复
       -> 调用无参数 escalate()
  -> 执行 Agent（自然语言回复）
  -> 最终新鲜度检查
  -> 最终正文替换思考卡片
```

前台或执行 Agent 每次运行都创建全新会话。运行期间收到的新消息会进入同一个实时会话，不会静默脱离：

- Codex：`codex app-server` + `turn/steer`
- Pi：原生 `pi --mode rpc` + `streamingBehavior: "steer"`
- Claude Code：流式 JSON 输入输出

前台会话会得到一个本地 stdio MCP 服务，其中只有一个无参数工具 `escalate()`。Codex 和 Claude 通过原生 CLI 参数挂载；Pi 加载仓库内的轻量适配器并调用同一 MCP 服务。该过程不会修改全局 Agent 配置。

如果原生 steer 失败，当前运行会安全停止，并带着累积上下文重新启动。`human_cooldown_seconds` 大于零时，操作者手动回复会终止 Agent 并进入冷却期；设为 `0` 可关闭人工接管。

Agent 输出刻意不使用结构化格式。前台要么直接写最终自然语言正文，要么先调用 `escalate()`；执行 Agent 只写最终自然语言正文。系统没有输出 JSON Schema、路由对象、变更清单、本地 Git 校验或交付重规划层，仓库规则及其正常 Git/CI 审查流程始终是最终依据。

Provider 的重试通知不是终止结果。例如 Codex 流中的 `willRetry=true` 错误会被忽略，直到 `turn/completed` 给出最终状态，因此已经恢复的 TLS 或响应流中断不会丢弃最终答案。

## 仓库结构

```text
pyproject.toml               包元数据和 dws-chat-agent 命令入口
config.json                  当前公开 Agent、工作流和运行策略
config.example.json          可移植配置模板
dm_agent.py                  兼容现有安装的 CLI
agent_core.py                兼容旧本地脚本的导入层
agent_runtime.py             兼容旧本地脚本的导入层
dashboard.py                 兼容旧本地脚本的导入层
src/dws_dm_agent/
  __main__.py                python -m dws_dm_agent 入口
  config.py                  环境变量展开和配置校验
  core.py                    消息、粗筛和审计
  dws.py                     通过 DWS 查询历史和回复
  service.py                 串行队列、上下文、steer、接管和编排
  prompts.py                 提示词变量、渲染和校验
  dashboard.py               本地 API、审计快照和安全配置编辑器
  front_eval.py              前台路由实时回放工具
  runners/runtime.py         Codex、Claude、Pi 和自定义 JSONL 会话协议
  runners/escalate_mcp.py    会话内无参数 stdio MCP 服务
  runners/pi_escalate.ts     Pi 到 stdio MCP 的适配器
  web/                       仪表盘、设置、主题、动效和 favicon
tests/                       服务、仪表盘、MCP 和 runner 回归测试
evals/front-routing.json     前台路由回放样本
prompts/                     可编辑的前台、执行和补充消息规则
deploy/macos/                LaunchAgent 模板
scripts/manage.sh            macOS LaunchAgent 生命周期实现
start-macos.command          macOS 一键安装/启动
start-windows.cmd            Windows 一键安装/启动
```

顶层 Python 文件只是轻量兼容层。现有 LaunchAgent 和本地脚本可以继续工作，新代码则从 `dws_dm_agent` 包导入。运行配置、提示词、`.env`、审计状态和 worktree 均放在包外，更新不会覆盖操作者数据。

位于 [http://127.0.0.1:8765/](http://127.0.0.1:8765/) 的 **DIAMOND Network** 仪表盘用于查看运行、队列、通道和 diamond-unit 状态。Metropolis 保留卡片控制台布局，Diamond 在 [http://127.0.0.1:8765/diamond-preview](http://127.0.0.1:8765/diamond-preview) 提供战术拓扑视图。二者共享 PROJECT D.I.A.M.O.N.D. 词汇、实时数据、功能、配色和主题偏好。独立设置页 [http://127.0.0.1:8765/settings](http://127.0.0.1:8765/settings) 可切换主题、独立应用内置或自定义颜色、搜索钉钉用户并配置本地白名单，以及编辑 diamond-unit、工作流和提示词。外观只保存在当前浏览器。配置保存带版本检查和 Schema 校验；有活动或排队任务时禁止保存；写入是原子的，并会在后续会话中生效，无需重启。设置页不会暴露或覆盖 Agent 环境变量、Provider 选项、认证文件、联系人 ID 或 `.env` 内容。

## 环境要求

- Python 3.11 或更高版本
- 可用的 `dws` CLI 登录
- Git
- 当前工作流使用的 Codex、Claude Code 或 Pi 可执行文件

服务本身只使用 Python 标准库，不创建项目虚拟环境。

## 首次运行

### macOS

双击 `start-macos.command`。

首次运行会创建 `.env`，用 TextEdit 打开后停止。填写配置并再次双击，它会校验运行环境、安装或启动当前用户的 LaunchAgent，并打开 [http://127.0.0.1:8765](http://127.0.0.1:8765/)。

命令行生命周期：

```bash
./manage.sh doctor
./manage.sh start
./manage.sh status
./manage.sh logs
./manage.sh restart
./manage.sh stop
```

### Windows

双击 `start-windows.cmd`。

首次运行会创建 `.env`，用记事本打开后停止。配置完成后再次运行，控制台会保持打开并自动打开仪表盘。按 `Ctrl+C` 停止服务。

### 手动安装

```bash
cp .env.example .env
chmod 600 .env
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 dm_agent.py doctor
python3 dm_agent.py run --mode shadow --open-dashboard
```

包入口等价于：

```bash
PYTHONPATH=src python3 -m dws_dm_agent doctor
PYTHONPATH=src python3 -m dws_dm_agent run --mode shadow --open-dashboard
```

如果 `config.json` 和 `.env` 不在源码目录中，请设置 `DWS_CHAT_AGENT_HOME`。否则服务优先使用包含 `config.json` 的源码目录，再回退到当前目录。

使用当前已配置的真实前台模型回放仓库中的路由样本：

```bash
PYTHONPATH=src python3 -m dws_dm_agent.front_eval --concurrency 1
```

配置确认前请使用 `shadow` 模式。该模式执行分析和审计，但不会发送钉钉消息；需要真实回复时再把 `DWS_CHAT_AGENT_MODE` 切换为 `live`。

## Agent 与工作流配置

`config.json` 的 `agents` 保存可复用 Agent 配置，`workflows.presets` 保存预设流水线，`workflows.active` 选择当前预设。可以通过仪表盘齿轮打开 `/settings` 编辑安全公开字段，也可以在服务停止时直接修改文件。

```json
{
  "workflows": {
    "active": "codex-default",
    "presets": {
      "codex-default": {
        "supplement_strategy": "steer",
        "supplement_prompt": "prompts/supplement.md",
        "auto_messages": {
          "ack_enabled": true,
          "ack": "已收到消息，正在处理中。",
          "progress_enabled": true,
          "max_progress_updates": 60
        },
        "front": {
          "label": "快速前台",
          "agent": "pi-front",
          "prompt": "prompts/front.md"
        },
        "worker": {
          "label": "执行 Agent",
          "agent": "codex-worker",
          "prompt": "prompts/worker.md"
        }
      }
    }
  }
}
```

每个预设必须有 `worker`，`front` 可选。省略 `front` 后，原始会话会直接交给执行 Agent。Agent 配置会把可执行启动器和会话协议分开：

```json
{
  "ccr-worker": {
    "driver": "claude",
    "protocol": "claude-stream-json",
    "command": ["ccr", "code"],
    "model": "sonnet",
    "read_only": false,
    "timeout_seconds": 10800
  },
  "omp-worker": {
    "driver": "pi",
    "protocol": "pi-rpc",
    "command": ["omp"],
    "model": "gpt-5.6",
    "reasoning_effort": "high",
    "read_only": false,
    "timeout_seconds": 10800
  }
}
```

`command` 是精确参数数组，永远不会通过 shell 执行。因此 `ccr code` 代理启动器、绝对路径、Windows `.cmd` shim 或带固定参数的包装器都不需要额外转义。适配器会追加 `protocol` 所需参数。凭据应放在 `environment` 或进程登录中，绝不能写进 `command`。

`label` 是工作流阶段的展示名称；`options` 保存协议专用设置；`extra_args` 会追加到适配器必需参数之后。`timeout_seconds: 0` 表示 Agent 无截止时间。对 Pi 而言，`options.tools: null` 会省略 `--tools` 白名单并暴露内置工具，前台仍由 `read_only: true` 和提示词共同约束权限。

内置协议包括 `codex-app-server`、`claude-stream-json` 和 `pi-rpc`。当前 `ccr code` 启动器会转发 Claude Code 参数，Oh My Pi 则提供原生 `--mode rpc`。完全独立的 CLI 可以使用 `custom-jsonl-v1`，其简短 stdin/stdout 协议见 [docs/custom-agent-protocol.md](docs/custom-agent-protocol.md)。前台适配器通过 `DWS_ESCALATE_MCP_CONFIG` 获取当前会话 MCP 路径。

未来可以增加更多预设流程，无需把当前流水线扩展成通用 DAG 引擎。

提示词使用普通 UTF-8 文件，当前启用的文件会原样显示在本地设置页。阶段提示词可使用 `{{self_name}}`、`{{agent_name}}`、`{{worker_name}}`、`{{contact_name}}`、`{{contact_alias}}`、`{{contact_user_id}}`、`{{session_id}}`、`{{prior_attempt}}`、`{{workspace_root}}`、`{{worktree_root}}`、`{{recent_context_json}}`、`{{current_messages_json}}` 和 `{{now}}`。补充消息提示词可使用 `{{supplement_messages_json}}`、`{{session_id}}`、联系人身份字段和 `{{self_name}}`。

配置前台时必须设置 `read_only: true`。它可以使用 Provider 提供的工具完成范围明确的只读调查并直接回复，任何写入或外部副作用都必须升级。执行 Agent 拥有正常编码权限，并依据原始会话和仓库规则判断授权；升级本身不会授予写权限。

启用流式卡片后，服务会立即展示已接收状态。两个角色共用 Provider 无关的流式表面：Codex 映射 reasoning summary，Pi 映射原生 thinking，Claude 映射 thinking block，自定义适配器也可以输出 `thinking`。服务不会编造进度标签、固定进度间隔或心跳文本；模型没有原生 thinking 时也不会伪造进度。最终自然语言正文会先持久化到本地 outbox，临时失败后使用同一 send UUID 重试，并在最终新鲜度检查后替换思考卡片。

## 安全边界

- 小型确定性 gate 只丢弃空确认消息并限制消息长度；所有语义安全判断仍由模型完成。
- 仓库的 `AGENTS.md` 和编码 Agent 负责项目特定的修改、审查、测试和发布策略。
- 服务会在确认和最终回复前重新检查会话。
- 自动钉钉消息保留平台 AI 标记。

运行状态、日志、审计数据和临时会话保存在 `state/`，不会提交。`.env` 和 `.env.*` 均被忽略，只有 `.env.example` 例外。
