# DWS Chat Agent configuration guide for coding agents

You are configuring DWS Chat Agent on the user's own computer. Complete the work instead of merely explaining commands, but preserve local data and stop for any choice that changes who may receive messages or whether messages are sent live.

Repository: `git@github.com:inoryirony/dws-chat-agent.git` (HTTPS fallback: `https://github.com/inoryirony/dws-chat-agent.git`)

## Non-negotiable safety

- Never print, paste into chat, commit, or copy elsewhere the contents of `.env`, login stores, tokens, cookies, private keys, or agent profile environment variables.
- Keep `.env` ignored and permission-restricted where the OS supports it. Private identities, contact IDs, internal domains, and machine paths belong there; `.env.example` must contain placeholders only.
- Do not overwrite an existing `.env`, `state/`, prompt, or configuration file. Inspect Git status first and preserve user changes.
- Do not switch from `shadow` to `live`, add monitored contacts, or send a test DingTalk message without the user's explicit approval.
- Do not reinstall or log out an already working DWS or agent CLI login. Reuse it and report authentication failures without exposing credentials.
- Use exact argument arrays for launchers. Never add `shell=true`, embed secrets in `command`, or download and execute an unreviewed wrapper.

## 1. Locate or install

If the repository already exists, use that checkout. Otherwise ask the user for the install directory if it is not obvious, then clone with their working Git transport. Do not create a nested duplicate checkout.

Confirm:

- Python 3.11 or newer
- Git
- a logged-in `dws` CLI
- at least one supported coding-agent launcher

Do not create a project virtual environment: the service uses the Python standard library only.

## 2. Detect available agent launchers

Inspect executable paths and version/help output without changing their configuration. Use this mapping:

| Launcher | Profile `command` | `protocol` | Notes |
| --- | --- | --- | --- |
| Codex | `["codex"]` | `codex-app-server` | Uses native app-server and `turn/steer` |
| Claude Code | `["claude"]` | `claude-stream-json` | Uses print mode with streaming JSON |
| Claude Code Router | `["ccr", "code"]` | `claude-stream-json` | Reuses the Claude protocol; CCR forwards its flags |
| Pi | `["pi"]` | `pi-rpc` | Uses native RPC; a bundled session extension bridges `escalate()` to stdio MCP |
| Oh My Pi | `["omp"]` | `pi-rpc` | Uses native RPC and the same bundled session extension |
| Other CLI | exact executable and fixed args | `custom-jsonl-v1` | Implement or use a reviewed thin wrapper following `docs/custom-agent-protocol.md` |

The launcher name and session protocol are independent. Do not implement a new adapter merely because the user invokes an existing protocol through a proxy command.

## 3. Gather only missing decisions

Inspect the existing `.env`, `config.json`, CLI logins, and repository state first. Ask only for values that cannot be discovered safely:

- the operator's DingTalk identity
- the explicit allowlist of people to monitor
- aggregate workspace and dedicated worktree roots
- which projects each monitored person is responsible for (directory names under the workspace root)
- active front and worker launchers/models
- whether this installation should remain in `shadow` or switch to `live`

Never echo discovered private values back. Confirm monitored people by display name, not by dumping IDs.

## 4. Configure

If `.env` is absent, copy `.env.example` to `.env`; never overwrite it. On macOS/Linux set mode `0600`. Fill private values in `.env` without displaying the file afterward.

If the service is already running and idle, open the gear on the localhost dashboard, or go directly to `http://127.0.0.1:8765/settings`. This separate page can safely edit public profile, workflow, automatic-message, prompt, and per-contact project assignment fields and applies them to subsequent sessions immediately. Project assignments take effect for agent working directories after the service restarts; the 刷新沙盒 button rebuilds the per-contact sandbox directories right away so a mistake shows up before the next message arrives. The page deliberately does not expose `.env`, contact IDs, provider options, or profile environment variables.

For an initial installation, or when the service cannot start, configure reusable profiles under `agents` and choose them from a preset under `workflows.presets`. Each profile must define:

```json
{
  "driver": "custom display family",
  "protocol": "one supported protocol",
  "command": ["executable", "fixed-subcommand"],
  "model": "model or empty string",
  "reasoning_effort": "medium or high",
  "read_only": true,
  "timeout_seconds": 0
}
```

The front profile must be `read_only: true`; the worker must be `read_only: false`. Set `timeout_seconds` to `0` for no time limit. Runtime write restrictions apply only to the front stage. The worker receives the original conversation and follows `prompts/worker.md`, the target repository's `AGENTS.md`, and the repository's normal Git/CI review policy. Escalation never grants write permission.

Every preset requires a worker. The front is optional: keep it for cheap direct replies and routing, or omit the `front` object to send every accepted message directly to the worker. Do not choose Pi, Codex, or Claude by role name alone; choose the launcher that best fits latency, reasoning, tool support, and local authentication.

When present, the front has exactly two output outcomes:

- write the final natural-language reply; or
- call the session-local zero-argument `escalate()` tool before writing any body.

`escalate()` accepts no reason, execution mode, permission, progress text, or context parameters. The service already forwards the complete request, recent conversation, and authorization boundary to the worker. Do not add JSON routing output or another decision schema.

Configure messages received during a run with `supplement_strategy: "steer"`; preserve `prompts/supplement.md` so new messages enter the live session instead of becoming detached development requests.

In the settings page, disable the automatic acknowledgement with `auto_messages.ack_enabled: false` (or clear its text). `auto_messages.progress_enabled` controls whether native thinking is shown, and `max_progress_updates` caps card updates. There is no configurable fixed interval or progress template. Disable DingTalk's message AI tag with `dws.ai_tag: false`; these settings apply to subsequent sessions.

Prompts are ordinary UTF-8 files. Edit them only when the user requests different behavior, and keep chat data inside the marked untrusted sections. The settings page must display the active profiles, launch commands, protocols, models, and prompt templates without displaying environment values. Theme selection and custom colors are browser-local and do not modify the Agent configuration.

The front escalation adapter is session-local and must never be installed into global agent settings:

- Codex receives an isolated per-session `CODEX_HOME` plus command-line MCP configuration.
- Claude receives a per-session `--mcp-config` with `--strict-mcp-config`.
- Pi/Oh My Pi receives the bundled extension only on that session; the extension calls the same stdio MCP server.
- A custom front adapter receives `DWS_ESCALATE_MCP_CONFIG`, `DWS_ESCALATE_PYTHON`, and `DWS_ESCALATE_MCP_SCRIPT`.

The stdio server exposes only `escalate` with an empty object input schema. Do not persist generated MCP files, auth links, or provider caches outside the temporary session directory.

## 5. Validate before starting

Run from the repository root:

```text
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 dm_agent.py doctor
```

On Windows use:

```bat
set PYTHONPATH=src
py -3 -m unittest discover -s tests -v
py -3 dm_agent.py doctor
```

Use `python` instead of `py -3` when that is the installed launcher. Fix every failed check. Do not claim readiness from executable discovery alone.

For a first installation, start in `shadow` mode and open `http://127.0.0.1:8765/`. Confirm:

- the settings page reports the intended workflow and agent commands
- all intended contacts, and no others, are monitored
- active and queued counts are visible
- recent request text is visible
- native thinking appears only when the selected model emits it
- a final natural-language reply replaces the thinking card
- expanding a record or prompt remains expanded across refreshes

Only after the user approves live operation may you set `DWS_CHAT_AGENT_MODE=live` and restart. Do not send a synthetic DingTalk message unless the user asks for one.

## 6. Existing installation or update

Before updating, inspect the current service status, active/queued work, Git status, and local configuration. Never restart while a conversation is active unless the user explicitly asks to interrupt it.

- Preserve `.env` and `state/` exactly.
- If tracked files have local changes, identify whether they are intentional configuration or unfinished code. Do not reset them. Commit, migrate, or ask the user before integrating upstream changes.
- Use a fast-forward update only when the checkout is clean and the branch relationship is clear; otherwise use a normal reviewable Git integration.
- Re-run the full tests and `doctor` after updating.
- For code updates, restart only after active and queued counts are both zero, then verify the dashboard and the current workflow on `/settings`. Agent settings saved through the settings page do not require a service restart.

Codex response-stream errors with `willRetry: true` are non-terminal notifications. Do not add an outer retry loop or fail the message immediately; the adapter waits for the final `turn/completed` status. A non-completed turn or a terminated process remains a real failure.

## Completion report

Report in plain language:

- whether the service is installed, updated, and/or running
- `shadow` or `live` mode
- active front and worker launcher/protocol/model, without credentials
- whether native thinking is enabled and which provider event is mapped
- monitored contact display names
- test and doctor results
- files changed and the Git commit, if any
- anything that still needs the user's decision

Do not include raw `.env` values, tokens, full authenticated commands, or private DingTalk IDs.
