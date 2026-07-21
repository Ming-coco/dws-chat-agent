# DWS Chat Agent

A local DingTalk chat delegate built on DWS and Codex. It watches an explicit direct-message allowlist, lets a fast read-only model handle simple questions, and escalates work that needs code changes or external actions to a stronger worker model.

The service is designed to run on the operator's own machine and reuse the operator's existing DWS and Codex sessions. Personal identities, contact IDs, local paths, and internal domains live in an ignored `.env` file rather than in Git.

## What it does

- Batches consecutive messages after a configurable quiet window.
- Serializes work per conversation and limits global concurrency.
- Stops or replans when the other person sends a supplement while a model is running.
- Stops handling a conversation when the operator replies manually, then observes a cooldown before taking over again.
- Lets the model choose a complete reply, no reply, or escalation.
- Uses a read-only fast model for simple code lookup and a workspace-writing model for implementation work.
- Rechecks the conversation before acknowledgements, progress updates, final replies, and attachments.
- Marks automated DingTalk messages with the platform's AI tag.
- Records local audit data and exposes a read-only dashboard on `127.0.0.1`.

The current listener handles direct messages. Group-chat takeover on an explicit mention is planned but is not implemented yet.

## Requirements

- macOS with Python 3.11 or newer
- A working `dws` CLI login
- A working Codex CLI or bundled Codex binary
- Git repositories that the worker is allowed to inspect or modify

## Configure

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` with the local operator identity, direct-message allowlist, workspace paths, and allowed execution domains. JSON-valued variables such as `DWS_CHAT_AGENT_CONTACTS_JSON` must contain valid JSON on one line.

`.env` and other `.env.*` files are ignored by Git; `.env.example` is the only exception. Runtime tuning that is safe to publish remains in `config.json`.

Start in `shadow` mode until the configuration has been verified. Shadow mode performs the analysis but never sends a DingTalk message; use `live` only when the agent should reply.

## Verify and run

```bash
python3 -m unittest -v
python3 dm_agent.py doctor
python3 dm_agent.py probe-gate '收到'
python3 dm_agent.py run --mode shadow
```

For a per-user LaunchAgent:

```bash
./manage.sh start
./manage.sh status
./manage.sh logs
./manage.sh restart
./manage.sh stop
```

`manage.sh` generates the installed plist from the current checkout, so the repository does not contain machine-specific paths. Set `DWS_CHAT_AGENT_PYTHON` or `DWS_CHAT_AGENT_LAUNCHD_LABEL` before running it if the detected Python binary or default label should be overridden.

The local dashboard is available at [http://127.0.0.1:8765](http://127.0.0.1:8765) while the service is running.

## Safety and delivery

- Incoming chat text is untrusted input. The service rejects obvious credential exfiltration, destructive commands, and unauthorized external execution before starting a model.
- The worker uses a dedicated Git worktree and must create a commit as a rollback point for code changes.
- Force pushes, destructive resets, branch deletion, irreversible production actions, and destructive database operations are prohibited.
- A requested attachment is built only from Git-verified changed files; sensitive files, symlinks, path escapes, and oversized payloads are rejected.
- Final replies are sent only after the service checks for supplements and manual takeover again.

Operational state, logs, the audit database, and temporary Codex sessions are stored under `state/` and are not committed.
