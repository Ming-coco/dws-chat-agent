# DWS Agent 响应时延与流式卡片方案

> 下文的时延数据和问题清单是改造前快照。当前已落地：`quiet_window=5s`、收到即建卡、Pi DeepSeek V4 Flash 前台直接流式文本、Codex 工具事件安全进度、长工具每 5 秒心跳，以及 16 条前台历史路由回放。worker 的结构化最终结果和原始思考仍不流出。

## 结论

当前慢点不在钉钉发送，而在发送前的编排和两段 Agent 串行执行：

- 最近成功样本中，简单问题从消息到最终回复中位约 `66.3s`。
- 升级 worker 的任务从消息到最终回复中位约 `282.7s`。
- worker 流程中，模型启动前中位约 `23.1s`，front 跑完并发出首条 ACK 还需约 `26.7s`，ACK 后 worker 中位再运行约 `225.8s`。
- 最终结果从 Agent 完成到钉钉发送，中位仅约 `2.1s`。

因此应先缩短首个可见反馈，再单独优化真实完成时间。流式卡片能明显改善“看起来更快”，但不会自动缩短 worker 的工具执行时间。

## 数据口径

数据来自本机 `state/audit.sqlite3`，统计窗口为 2026-07-23 至 2026-07-30，范围是 `status=sent` 的 Agent 运行：

| 流程 | 样本 | 模型前等待中位数 | Agent 中位数 | 首条 ACK 中位数 | 最终回复中位数 |
|---|---:|---:|---:|---:|---:|
| front 直接回复 | 12 | 22.4s | 37.8s | 无 | 66.3s |
| front + worker | 31 | 23.1s | 258.6s | 51.0s | 282.7s |

worker 样本的阶段拆分：

| 阶段 | 中位数 |
|---|---:|
| 消息到 front 启动 | 23.1s |
| front 启动到 ACK | 26.7s |
| ACK 到 worker 完成 | 225.8s |
| worker 完成到钉钉发送 | 2.1s |

模型前等待平均值被一个约 47.8 分钟的排队长尾拉高；这是同会话严格串行和全局并发槽产生的长尾，不代表普通请求。

## 当前完整流程

```text
DingTalk 私聊事件
  -> 每联系人 dws event consume
  -> 解析、去重、写入 events
  -> 按 conversation_id 入队
  -> 固定等待 quiet_window_seconds=20
  -> 获取全局并发槽（当前最多 2 个会话）
  -> 拉取最多 80 条历史并稳定消息批次
  -> 人工接管检查 / 确定性 gate / 频率保护
  -> 新建 Codex front session
       -> terra / medium / read-only
       -> 直接回复，或升级 worker
  -> 若升级：再次检查会话新鲜度
  -> 发送“已收到消息，正在处理中”
  -> 新建 Codex worker session
       -> sol / high / 完整工具权限
       -> 每 5 秒轮询一次会话历史
       -> 每 180 秒最多发送一次已有 progress
  -> 解析结构化结果
  -> 核验代码、提交、分支和附件
  -> 再次拉取历史做 freshness 检查
  -> 发送最终文本或附件
```

## 根因

1. 首条反馈发得太晚

   `20s` 安静窗口之后才启动 front；ACK 又只在 front 判断需要 worker 后发送。当前 worker 请求的首条 ACK 中位为 `51s`。

2. front 和 worker 完全串行

   front 的时间不能被 worker 复用。升级任务先付出一次 `terra/medium`，再从头启动 `sol/high`。

3. front 本身不轻

   当前 front prompt 约 `9.3KB`，还要求先加载写私聊回复技能；真实 front 运行中位约 `38s`，明显高于空提示的模型协议耗时。

4. worker 才是真实完成时间的主体

   worker 需要读代码、查日志、调用工具、等待测试或发布，ACK 后中位仍需约 `226s`。只换消息载体无法消除这段时间。

5. 当前不是真正的流式输出

   Codex 的 `item/agentMessage/delta` 和 Pi 的 `message_update/text_delta` 都没有进入钉钉发送链路。现有 progress 只读取已完成的 Agent 文本，而且间隔为 `180s`。

6. 历史轮询放大长尾

   活跃 session 每 `5s` 拉一次历史；后台还会每分钟为全部联系人做历史补偿。当前一次健康历史查询约 `0.5s`，但日志中已有 `NETWORK_ERROR`、`RATE_LIMIT_ERROR` 和认证失败。失败或超时会推迟完成检测，并增加 DWS 压力。

## Pi Agent 验证

Pi 已经接入，不需要新增适配器：

- 本机版本：`pi 0.80.3`
- 协议：原生 `pi --mode rpc`
- 当前默认 provider/model：`ccr-internal/deepseek-v4-flash`
- `pi-default` workflow 已存在
- Pi/Codex/Claude runner 协议测试：16 个通过

单样本 A/B：

| 探针 | Codex | Pi |
|---|---:|---:|
| 极简 RPC 总时长 | 12.57s | 12.09s |
| 当前完整 front prompt | 22.50s | 6.66s |

完整 front prompt 样本中，两者都返回了合法的 `route=reply/action=reply`。Pi 适合继续作为 front 候选，但一条样本不能证明复杂上下文、worker 路由和安全拒绝质量。切换前应跑现有 front holdout，worker 暂时保留 Codex。

## 推荐改造

### 第一阶段：先让用户在 5～10 秒内看到反馈

1. 将 `quiet_window_seconds` 从 `20` 降到 `5`。
2. 通过 DWS 现有命令创建一张流式卡片：

   ```text
   dws chat message send-card --receiver <openDingTalkId>
   dws chat message update-card --biz-id <bizId> --content <text> --flow-status <status>
   ```

3. 卡片在 gate 和人工接管检查通过后、front 启动前创建。
4. 阶段变化时更新同一张卡片：

   ```text
   1 PROCESSING  已收到，正在理解问题
   4 EXECUTING   已确认任务，正在执行
   2 INPUTTING   正在整理结果
   3 FINISH      最终回复
   5 ERROR       处理失败或已转人工
   ```

最后一次更新必须是 `3 FINISH`；异常路径必须收口为 `5 ERROR`，不能遗留永久“生成中”的卡片。

不要把模型的原始 token 直接写进卡片。当前模型输出是内部结构化 JSON，包含 `handled`、`reason`、变更证据等字段；直接流式转发会泄露内部字段并产生残缺 JSON。卡片只流式展示阶段状态和可公开的 commentary，最终 reply 在结构化结果解析完成后一次写入。

### 第二阶段：减少 front 时间

1. 用现有 holdout 对 `pi-front` 与 `codex-front` 做准确率、安全和耗时对比。
2. 先灰度成 `pi-front + codex-worker`，不直接切换完整 `pi-default`。
3. 若 Pi 质量达标，再评估把 front 的强制技能加载改成少量内嵌回复规则。

这一步预计能减少十几到几十秒，但不会消除 worker 的数分钟执行时间。

### 第三阶段：降低长尾

1. 将 progress 更新节流到 `15～30s`，只在内容变化时更新同一张卡片。
2. 合并活跃会话轮询与后台历史补偿，避免对每个联系人固定每分钟拉取 80 条历史。
3. 为以下指标单独记时，避免继续只看整段 `runs`：

   - event received
   - queue acquired
   - first history complete
   - front started/completed
   - card created
   - worker started/completed
   - final card updated

## 验收标准

- 首张卡片 p50 小于 `8s`，p90 小于 `15s`。
- front 直接回复 p50 小于 `30s`。
- worker 流程不再额外等待 20 秒合并窗口。
- 任意成功、失败、人工接管和服务重启路径都不会遗留“生成中”卡片。
- 补充消息仍进入当前 session 的 steer 流程，不创建重复任务。
- 人工回复后停止更新卡片，并明确结束为转人工或取消状态。
- 不向卡片流式输出内部 JSON、工具参数、凭据或未完成的审计字段。
