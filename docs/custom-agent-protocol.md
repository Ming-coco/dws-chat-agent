# Custom agent JSONL protocol v1

Use `custom-jsonl-v1` when an agent CLI does not speak one of the bundled live-session protocols. The service starts one process for one workflow stage and exchanges one UTF-8 JSON object per line over stdin/stdout. Write human-readable logs to stderr; stdout is reserved for protocol messages.

Example profile:

```json
{
  "my-agent": {
    "driver": "custom",
    "protocol": "custom-jsonl-v1",
    "command": ["/absolute/path/my-agent", "serve", "--jsonl"],
    "model": "my-model",
    "reasoning_effort": "high",
    "read_only": false,
    "timeout_seconds": 10800,
    "environment": {"MY_AGENT_CONFIG": "/absolute/path/config.json"}
  }
}
```

`command` is passed directly to the operating system as an argument array. Shell expansion, pipes, redirects, and command substitution are intentionally unsupported. Put secrets in environment variables or the CLI's own login store, not in repository configuration.

## Start

The first input starts the session:

```json
{"id":1,"type":"start","protocolVersion":1,"sessionId":"dm-...","stage":"worker","cwd":"/workspace","prompt":"...","outputSchema":{},"readOnly":false,"model":"my-model","reasoningEffort":"high"}
```

The CLI must acknowledge it promptly:

```json
{"id":1,"type":"response","success":true}
```

`readOnly` is mandatory policy input. A custom CLI is responsible for actually restricting its tools when it is `true`; the service cannot impose a portable sandbox on an arbitrary executable.

## Progress and steering

The CLI may emit concrete progress at any time:

```json
{"type":"progress","text":"正在定位订单同步接口及相关测试"}
```

When a DingTalk message arrives during the run, the service sends it into the same process:

```json
{"id":2,"type":"steer","sessionId":"dm-...","message":"对方刚补充的新消息"}
```

The CLI acknowledges whether it accepted the message:

```json
{"id":2,"type":"response","success":true}
```

If steering fails or times out, the orchestrator safely stops the process and can restart a fresh session with accumulated context.

## Result, error, and abort

Finish with a JSON object that satisfies the stage's configured output schema:

```json
{"type":"result","result":{"action":"reply","reply":"已确认……"}}
```

Report a terminal error as:

```json
{"type":"error","message":"authentication expired"}
```

Human takeover or timeout sends:

```json
{"id":3,"type":"abort","sessionId":"dm-..."}
```

The CLI should stop work, acknowledge with `type=response`, and exit. The service will terminate the process tree if it does not exit within the grace period.
