# PROJECT D.I.A.M.O.N.D.

*DingTalk-Integrated Agent for Messaging, Orchestration & Networked Delegation*

[English](README.md) | [简体中文](README.zh-CN.md)

An autonomous DingTalk interface for coordinating local AI coding agents. It watches an explicit allowlist, answers direct messages, and can hand implementation work to a worker agent. It supports Codex, Claude Code, Pi, and thin custom adapters without fixing either role to one product.

The service runs on the operator's machine and reuses existing DWS and coding-agent logins. Personal identities, contact IDs, local paths, and internal domains stay in an ignored `.env` file.

## Let an agent configure it

Copy this sentence to a coding agent on the target computer:

> 帮我安装并配置 DWS Chat Agent：https://raw.githubusercontent.com/inoryirony/dws-chat-agent/main/docs/agent-config.md

Already installed? The same guide also supports safe inspection and updates:

> 帮我检查并更新 DWS Chat Agent：https://raw.githubusercontent.com/inoryirony/dws-chat-agent/main/docs/agent-config.md

## Runtime flow

```text
DingTalk event
  -> zero-token coarse gate
  -> per-conversation queue and serial lock
  -> stabilize and merge recent messages
  -> optional front agent (runtime read-only)
       -> natural-language reply
       -> zero-argument escalate()
  -> worker agent (natural-language reply)
  -> final freshness check
  -> final body replaces the thinking card
```

Each front or worker run gets a fresh agent session. Messages that arrive while it is running are sent into that same live session instead of being silently detached:

- Codex: `codex app-server` with `turn/steer`
- Pi: native `pi --mode rpc` with `streamingBehavior: "steer"`
- Claude Code: streaming JSON input/output

The front receives a session-local stdio MCP server with one zero-argument tool, `escalate()`. Codex and Claude mount it through their native CLI options. Pi loads the bundled thin adapter, which calls the same stdio MCP server. Nothing is added to global agent configuration.

If native steering fails, the current run is stopped safely and restarted with the accumulated context. When `human_cooldown_seconds` is greater than zero, a manual operator reply aborts agent handling and starts that cooldown; setting it to `0` disables manual takeover.

Agent output is deliberately unstructured. A front agent either writes the final natural-language body or calls `escalate()` before writing a body. A worker writes only the final natural-language body. There is no output JSON schema, route object, change manifest, local Git verification, or delivery replanning layer; repository rules and the repository's normal Git/CI review flow remain authoritative.

Provider retry notifications are not terminal results. In particular, a Codex `willRetry=true` stream error is ignored until `turn/completed` supplies the final status, so a recovered TLS or response-stream interruption does not discard a completed answer.

## Repository layout

```text
pyproject.toml               package metadata and dws-chat-agent console command
config.json                  current public profiles, workflow, and runtime policy
config.example.json          portable configuration scaffold
dm_agent.py                  compatibility CLI used by existing installations
agent_core.py                compatibility import for older local scripts
agent_runtime.py             compatibility import for older local scripts
dashboard.py                 compatibility import for older local scripts
src/dws_dm_agent/
  __main__.py                python -m dws_dm_agent entry point
  config.py                  environment expansion and validated settings model
  core.py                    messages, coarse gate, and audit
  dws.py                     history and replies through DWS
  service.py                 serial queues, context, steering, takeover, and orchestration
  prompts.py                 shared prompt variables, rendering, and validation
  dashboard.py               localhost API, audit snapshot, and safe config editor
  front_eval.py              live front-routing replay runner
  runners/runtime.py         Codex, Claude, Pi, and custom JSONL session protocols
  runners/escalate_mcp.py    session-local zero-argument stdio MCP server
  runners/pi_escalate.ts     Pi-to-stdio-MCP adapter
  web/                       dashboard, settings, themes, motion, and favicon
tests/                       service, dashboard, MCP, and runner regressions
evals/front-routing.json     checked-in front-routing replay cases
prompts/                     editable front, worker, and supplement business rules
deploy/macos/                LaunchAgent template
scripts/manage.sh            macOS LaunchAgent lifecycle implementation
start-macos.command          one-click macOS setup/start
start-windows.cmd            one-click Windows setup/start
```

The top-level Python files are deliberately thin compatibility seams. Existing
LaunchAgents and local scripts keep working while new code imports the
`dws_dm_agent` package. Runtime configuration, prompts, `.env`, audit state, and
worktrees remain outside the package so updates never overwrite operator data.

The **DIAMOND Network** dashboard at [http://127.0.0.1:8765/](http://127.0.0.1:8765/) stays focused on operations, the queue, channels, and diamond-unit status. Metropolis preserves the original card-console layout; Diamond uses the tactical topology view at [http://127.0.0.1:8765/diamond-preview](http://127.0.0.1:8765/diamond-preview). Both views share the PROJECT D.I.A.M.O.N.D. vocabulary, live data, functionality, color system, and theme preference. The separate settings page at [http://127.0.0.1:8765/settings](http://127.0.0.1:8765/settings) switches interface themes, independently applies built-in or user-created colors, searches DingTalk users for the local allowlist, and edits diamond-units, workflows, and prompts. Appearance is stored only in the current browser. Configuration saves are revision-checked, schema-validated, blocked while work is active or queued, written atomically, and applied to subsequent sessions without restarting. The page never exposes or overwrites profile environment variables, provider options, auth files, contact IDs, or `.env` values.

## Requirements

- Python 3.11 or newer
- A working `dws` CLI login
- Git
- The binaries used by the active workflow: Codex, Claude Code, or Pi

The service itself uses only the Python standard library; it does not create a project virtual environment.

## First run

### macOS

Double-click `start-macos.command`.

On the first run it creates `.env`, opens it in TextEdit, and stops. Fill in the values and double-click it again. It validates the runtime, installs/starts the per-user LaunchAgent, and opens [http://127.0.0.1:8765](http://127.0.0.1:8765).

Command-line lifecycle:

```bash
./manage.sh doctor
./manage.sh start
./manage.sh status
./manage.sh logs
./manage.sh restart
./manage.sh stop
```

### Windows

Double-click `start-windows.cmd`.

On the first run it creates `.env`, opens Notepad, and stops. After configuration, run it again. The console stays open while the agent runs and the dashboard opens automatically. Stop it with `Ctrl+C`.

### Manual setup

```bash
cp .env.example .env
chmod 600 .env
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 dm_agent.py doctor
python3 dm_agent.py run --mode shadow --open-dashboard
```

The packaged entry point is equivalent:

```bash
PYTHONPATH=src python3 -m dws_dm_agent doctor
PYTHONPATH=src python3 -m dws_dm_agent run --mode shadow --open-dashboard
```

Set `DWS_CHAT_AGENT_HOME` when `config.json` and `.env` live outside the source checkout. Otherwise the service uses the source project when it contains `config.json`, then falls back to the current directory.

Replay the checked-in front-routing cases against the configured live front model:

```bash
PYTHONPATH=src python3 -m dws_dm_agent.front_eval --concurrency 1
```

Start in `shadow` mode until configuration is verified. Shadow mode performs analysis and audit but does not send DingTalk messages. Switch `DWS_CHAT_AGENT_MODE` to `live` when it should reply.

## Agent and workflow configuration

`config.json` contains reusable profiles under `agents` and preset pipelines under `workflows.presets`. Use the dashboard gear to open `/settings` and edit the safe public fields, or change the file directly when the service is stopped. `workflows.active` selects a preset.

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

A preset always requires a `worker`; `front` is optional. Omitting `front` sends the original conversation directly to the worker. A profile separates the executable launcher from the session protocol:

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

`command` is an exact argument array and is never executed through a shell. Therefore a proxy launcher such as `ccr code`, an absolute executable path, a `.cmd` shim on Windows, or a wrapper with fixed arguments all work without quoting tricks. The adapter appends the flags required by `protocol`. Keep credentials in `environment` or the process login, never in `command`.

`label` is display text for a workflow stage. `options` contains protocol-specific settings, while `extra_args` appends literal CLI arguments after the adapter's required flags. `timeout_seconds: 0` means no agent deadline. For Pi, `options.tools: null` omits the `--tools` allowlist and exposes its built-in tools; the front prompt and `read_only: true` remain the authorization boundary.

The bundled protocols are `codex-app-server`, `claude-stream-json`, and `pi-rpc`. The current `ccr code` launcher forwards Claude Code flags, while Oh My Pi exposes native `--mode rpc`. A completely independent CLI can use `custom-jsonl-v1`; its small stdin/stdout contract is documented in [docs/custom-agent-protocol.md](docs/custom-agent-protocol.md). The front adapter receives the session-local MCP path in `DWS_ESCALATE_MCP_CONFIG`.

Future custom flows can be introduced as additional presets without turning the current pipeline into a general DAG engine.

Prompt text is stored in normal UTF-8 files and the active files are shown verbatim on the localhost settings page. A stage prompt may use `{{self_name}}`, `{{agent_name}}`, `{{worker_name}}`, `{{contact_name}}`, `{{contact_alias}}`, `{{contact_user_id}}`, `{{session_id}}`, `{{prior_attempt}}`, `{{workspace_root}}`, `{{worktree_root}}`, `{{recent_context_json}}`, `{{current_messages_json}}`, and `{{now}}`. The supplement prompt receives `{{supplement_messages_json}}`, `{{session_id}}`, contact identity fields, and `{{self_name}}`.

When configured, the front profile must set `read_only: true`. It may use the tools available to its provider for bounded read-only investigation, then answer directly; any write or external side effect must escalate. The worker has normal coding-agent permissions and judges authorization from the original conversation plus repository rules; escalation itself never grants write permission.

With streaming cards enabled, acceptance is shown immediately. Both roles use the same provider-neutral stream surface: Codex maps reasoning summaries, Pi maps native thinking, Claude maps thinking blocks, and custom adapters may emit `thinking`. The service does not invent progress labels, fixed progress intervals, or heartbeat text. Models that emit no native thinking produce no fabricated progress. The final natural-language body is persisted in the local outbox before delivery, retried with the same send UUID after transient failures, and replaces the thinking card after the final freshness check.

## Safety

- A small deterministic gate only drops empty acknowledgements and enforces the message-size limit; all semantic safety decisions stay with the model.
- Repository AGENTS.md files and the coding agent enforce project-specific change, review, test, and release policy.
- The service rechecks the conversation before acknowledgements and final replies.
- Automated DingTalk messages keep the platform AI tag enabled.

Operational state, logs, audit data, and temporary sessions live under `state/` and are not committed. `.env` and `.env.*` are ignored; `.env.example` is the only exception.
