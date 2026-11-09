# 已知缺陷

记录已定位但暂不修复的问题。每条包含：现象、证据、根因、修复方向。

---

## 1. 流式卡片绑定失败，回复退化成纯文本并残留空白卡片

**状态**：已修复（2026-11-09）

**现象**：消息进来后先出现一张「正在处理」的卡片；agent 回复到达后，回复以**另一条普通文本消息**出现，
而原来那张卡片变成一条没有正文的残留（客户端上看是空白卡片）。

**证据**（`state/audit.sqlite3` 的 `outgoing` 表，控制台群 2026-11-09 两次回复）：

| 时间 | card_biz_id | card_flow_status | content_sha256 |
| --- | --- | --- | --- |
| 09:59:01 | —（纯文本） | — | `6eae1b63…` = 真实回复 |
| 09:59:02 | `transformer_card_8342…` | 5 | `87175b50…` |
| 10:09:53 | —（纯文本） | — | `95e5ddb5…` = 真实回复 |
| 10:09:55 | `transformer_card_ef19…` | 5 | `87175b50…` |

- 两条纯文本记录的哈希等于 `runs.handled` 里对应回复正文的规范化哈希 → **答案确实走了纯文本通道**。
- 两条卡片记录的哈希完全相同且不等于任何回复正文 → 里面写的是一个常量。对 `service.py` 全部字符串
  字面量做哈希比对，唯一命中：**`卡片识别失败，请等待普通文本回复。`**（[service.py:2706](../src/dws_dm_agent/service.py#L2706)）。

**根因**：

1. `dws chat +messages-send-card` 发送后没有返回 `message_id`（`_find_message_id` 已递归查过
   `openMessageId`/`messageId`/`message_id`/`msgId` 四种键名，确属响应里没有），于是
   [service.py:2665](../src/dws_dm_agent/service.py#L2665) 的 `if message_id:` 提前返回被跳过；
2. 代码退而求其次，轮询群历史并**用文本精确匹配**认领自己刚发的那张卡，匹配目标是
   `content + "\n\n处理编号：<uuid8>"`；
3. 钉钉回存的卡片消息文本不包含这段处理编号（历史里的卡片项 content 与该字符串对不上），
   四次重试（0.25/0.5/1/2s）全部失败，进入 [service.py:2705](../src/dws_dm_agent/service.py#L2705) 分支：
   把卡片收口成上面的失败提示、返回空串，让投递回退成纯文本。

**补充：真正致命的是这个兜底动作，而不是认领失败本身**（2026-11-09 复核时修正）。投递路径
（`_start_delivery_card`）创建卡片时写进去的**已经是最终答复正文**，带处理编号。认领失败后
原代码做了两件破坏性的事：

1. 用 `卡片识别失败，请等待普通文本回复。` 覆盖卡片 → 答复被从卡片里擦掉，群里留下一条
   看不懂的残骸（客户端上表现为空白/失败卡片）；
2. 返回空串让投递回退纯文本 → 同一段答复又以普通消息发了一遍。

而 `message_id` 认领失败其实**不影响交付**：卡片就是交付载体，`_dispatch_due_delivery`
随后会用完整正文把卡片更新成 `flow_status=3`；审计侧 `AuditStore.is_agent_outgoing` 对未绑定
的 outgoing 行本来就有「内容哈希 + 时间窗」兜底匹配。所以认领只该是尽力而为的补绑定。

**实测补充**：`dws chat +chat-messages` 回存的卡片消息**带 `messageAiSendFlag: "DWS"`**，
和 agent 发出的普通回复一致，而本人手打的消息没有这个标记。这条差异可以直接用来认领卡片，
比文本匹配稳。

**影响范围**：所有对话（不只是控制台群）。09:59 那次是 `pi-front` 直接回、10:09 那次是 `pi-worker` 回，
两条不同链路走的是同一失败分支 → 稳定复现，非偶发。只要开启 `dws.streaming_cards` 就会踩到。

**修复内容**（2026-11-09）：

- `_start_streaming_card` 不再把认领当成交付前提：认领失败只记一条 `unclaimed` 日志，
  卡片照常返回 `biz_id`，投递继续走卡片，**不再产生第二条纯文本**；
- 删掉「把卡片改写成失败提示」这一步，改为无论如何都把卡片内容收敛成不带处理编号的正文；
- 新增 `_pick_card_message`：先用处理编号做精确匹配，匹配不到再用 `ai_sent` 标记认领，
  且只在唯一命中时才绑定，避免把同时发出的别的消息错绑到这张卡上。

**连带修掉：一条回复写两行 `outgoing`**。`_start_streaming_card` 内部自行
`send_uuid = str(uuid.uuid4())` 新开一行记账，而投递行本来就有自己的 `send_uuid`，
于是同一段答复落了两行（实测 14:00 那次：`663545b9` 投递行 + `b820ae99` 卡片行）。
`mark_delivery_sent` 里还有一段按 `card_biz_id` 广播 `content_sha256` / `card_flow_status`
的补偿逻辑，把两行刷成一致，等于把这个重复盖住了。现在 `_start_delivery_card` 把投递行的
`send_uuid` 透传下去，卡片复用该行（`bind_delivery_card` + `update_streaming_card`），
一张卡只对应一行；ack 与失败通知仍然各开一行（它们本身就是独立对外消息）。
实测 14:11 那次一条回复只落一行 `ba59dcf8`，消息 id 直接绑在同一行。

**卡片没有 AI 角标**：`chat message send`（文本）有 `--ai-tag` 参数，
而 `chat message send-card` 的完整参数清单里**没有** `--ai-tag`，`+messages-send-card`
shortcut 也没有。数据层卡片仍带 `messageAiSendFlag: "DWS"`，自环过滤不受影响，
但客户端不渲染「AI 生成」角标。结果是混合状态更糟：**文本回复有角标、卡片回复没有**。

**当前决定**（2026-11-09）：`dws.streaming_cards` 置为 `false`，全部回复走纯文本，
每条都带官方 AI 标识，规避角标缺失。修复后的卡片代码保留不删，改回 `true` 即可恢复。
关闭卡片时补了 `_notify_terminal`：终态通知（处理失败、补充消息过多）原本只靠卡片下发，
卡片关掉后会变成静默失败，现在改为发一条带 AI 标识的文本。

---

## 2. agent 无法搜索钉钉群

**状态**：属于能力缺失，非缺陷（2026-11-09 记录）

**现象**：控制台群里让 agent 找「客户技术服务群」，它找不到，但该群确实存在。

**原因**：agent 只认识 `config.json` 里登记的 2 个群和 2 个单聊，且它的工具集（`pi-front`：
`read/grep/find/ls` + `escalate/jira/gitlab/browser/no_reply/task_*`）里没有任何钉钉查询能力。
`dws chat +chat-search` 能搜到该群（`cid-client-a`，7 人），但这条能力没有暴露给 agent。

**要用这个群**：把它加进 `config.json` 的 `groups[]`（需要人工确认后再加，
见 `docs/agent-config.md` 关于新增联系人的约定）。
