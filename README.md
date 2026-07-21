# DWS Chat Agent

A local DingTalk delegate that watches an explicit allowlist, answers direct messages, and can hand implementation work to a stronger coding agent. It supports Codex, Claude Code, and Pi behind one two-stage workflow.

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
  -> front agent (runtime read-only)
       -> reply / no reply
       -> worker agent (full runtime permissions, behavior constrained by prompt)
  -> verify code/delivery evidence
  -> re-read the conversation
  -> reply or attachment
```

Each front or worker run gets a fresh agent session. Messages that arrive while it is running are sent into that same live session instead of being silently detached:

- Codex: `codex app-server` with `turn/steer`
- Pi: native `pi --mode rpc` with `streamingBehavior: "steer"`; no plugin is required
- Claude Code: streaming JSON input/output

If native steering fails, the current run is stopped safely and restarted with the accumulated context. A manual operator reply always aborts agent handling and starts the configured cooldown.

## Repository layout

```text
agent_runtime.py             launchers, session protocols, profiles, and workflow preset
dm_agent.py                  DingTalk queue, context, safety, delivery, and audit orchestration
agent_core.py                deterministic gate and SQLite audit model
prompts/front.md             configurable front-agent prompt
prompts/worker.md            configurable worker-agent prompt
prompts/supplement.md        prompt used for messages received during a run
docs/agent-config.md         copy-paste setup guide for another coding agent
docs/custom-agent-protocol.md custom CLI stdin/stdout contract
config.json                  public profiles, active workflow, and runtime policy
dashboard.py/html            localhost monitoring console and HTTP API
settings.html                visual Agent and workflow configuration page
theme.js                     shared browser-local theme presets and custom colors
favicon.svg                  Metropolis-inspired page mark
start-macos.command          one-click macOS setup/start
start-windows.cmd            one-click Windows setup/start
manage.sh                    macOS LaunchAgent lifecycle
```

The main dashboard at [http://127.0.0.1:8765/](http://127.0.0.1:8765/) stays focused on active work, the queue, and the processing timeline. Use its gear button to open the separate settings page at [http://127.0.0.1:8765/settings](http://127.0.0.1:8765/settings). That page shows the current runtime flow, switches presets, edits Agent profiles and prompts, and offers Metropolis plus common editor themes and custom colors. Appearance is stored only in the current browser. Agent saves are revision-checked, schema-validated, blocked while work is active or queued, written atomically, and applied to subsequent sessions without restarting. The page never exposes or overwrites profile environment variables, provider options, auth files, contact IDs, or `.env` values.

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
python3 -m unittest -v
python3 dm_agent.py doctor
python3 dm_agent.py run --mode shadow --open-dashboard
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
          "ack": "收到，我在处理中。",
          "progress": "{{progress}}",
          "progress_enabled": true,
          "progress_interval_seconds": 180
        },
        "front": {"agent": "codex-front", "prompt": "prompts/front.md"},
        "worker": {"agent": "codex-worker", "prompt": "prompts/worker.md"}
      }
    }
  }
}
```

The included scaffold intentionally has only `front -> worker`. A profile separates the executable launcher from the session protocol:

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

The bundled protocols are `codex-app-server`, `claude-stream-json`, and `pi-rpc`. The current `ccr code` launcher forwards Claude Code flags, while Oh My Pi exposes native `--mode rpc`, so both reuse an existing protocol without a plugin. A completely independent CLI can use `custom-jsonl-v1`; its small stdin/stdout contract is documented in [docs/custom-agent-protocol.md](docs/custom-agent-protocol.md). An incompatible existing CLI only needs a thin wrapper that implements that contract. DingTalk orchestration does not change.

Future custom flows can be introduced as additional presets without turning the current pipeline into a general DAG engine.

Prompt text is stored in normal UTF-8 files and the active files are shown verbatim on the localhost settings page. A stage prompt may use `{{self_name}}`, `{{agent_name}}`, `{{worker_name}}`, `{{contact_name}}`, `{{contact_alias}}`, `{{contact_user_id}}`, `{{session_id}}`, `{{prior_attempt}}`, `{{workspace_root}}`, `{{worktree_root}}`, `{{recent_context_json}}`, `{{current_messages_json}}`, `{{execution_domains_json}}`, `{{reference_domains_json}}`, `{{execution_mode}}`, `{{execution_rule}}`, and `{{now}}`. The supplement prompt receives `{{supplement_messages_json}}`, `{{session_id}}`, contact identity fields, and `{{self_name}}`. Unknown or missing variables fail validation instead of silently producing a broken prompt.

Only the front profile must set `read_only: true`. The worker has normal coding-agent permissions; planning-only behavior for large changes is enforced in the worker prompt and requires the other person's explicit approval before implementation.

## Safety and delivery

- Incoming chat is untrusted. A small deterministic gate rejects obvious credential exfiltration and destructive instructions before spending model tokens; semantic decisions stay with the model.
- Large changes first produce a plan, impact scope, and validation plan. The worker may edit only after explicit approval in the conversation.
- Code work uses a dedicated Git worktree and requires a commit as a rollback point.
- Force pushes, destructive resets, branch deletion, irreversible production actions, and destructive database operations are prohibited by the preset prompt.
- Attachments are built only from Git-verified changed files; secrets, symlinks, path escapes, and oversized payloads are rejected.
- The service rechecks the conversation before acknowledgements, progress updates, final replies, and attachments.
- Automated DingTalk messages keep the platform AI tag enabled.

Operational state, logs, audit data, and temporary sessions live under `state/` and are not committed. `.env` and `.env.*` are ignored; `.env.example` is the only exception.
