# 智管工时自动填写

工作日 17:40（周一到周五，节假日跳过，调休补班照常）按本机工作痕迹生成智管工时**草稿**，并把摘要和确认链接**发到你的钉钉邮箱**，你点链接确认后提交。

## 它怎么工作

1. 先看**钉钉日历**当天：整天或半天以上的「请假」直接跳过不填；有会议的按标题关键词挂到对应项目，写进该项目的日志内容里（挂不上的写进当天工时最多的项目），**不新增条目、不改变工时分配**。
2. **动态枚举** `D:\workspace` 下的一级子目录，挑出当天真有活动的那些——目录会不断增加，清单不写死（见下面「目录与归属」）。
3. 读每个目录当天的**手写工作日志**（`工作日志\YYYY-MM-DD-周X.md` 或 `YYYY-MM-DD_周X_工作日志.md`）。日志里如果有一个 `## 工时内容`（或「智管内容」「填写内容」）小节，就**原样**用它当工时正文——想精确控制提交上去的文字，写这一节即可；没有这一节才退回「主题 / 当日主线 / 推进内容 / 要点」自动摘要。
4. 既没有手写日志、也没有工时段落时，退化为 `git log` 当天提交 + 文件修改时间统计。
5. 按各项目当天的工作量把 7.5 小时分配下去（步长 0.5h）。
6. 调智管 MCP 的 `save_worklog_draft` 存成草稿，并把 `reviewUrl` 写进 `zhiguan_state\fill-log.jsonl`。
7. 用 `dws mail message send` 把「日期 / 各项工时 / 合计 / 确认链接」发到你的钉钉邮箱。

同一天已经有草稿或已提交记录时默认跳过，不会重复生成。

## 邮件通知

`config.json` 的 `notify` 段控制发信：

```json
"notify": {
  "enabled": true,
  "channel": "mail",
  "from": "Ming-coco@users.noreply.github.com",
  "to": ["Ming-coco@users.noreply.github.com"],
  "cc": [],
  "mode": "always"
}
```

- **`cc` 留空**。收件人写一个邮箱就够了：`to` 和 `cc` 都填自己的邮箱时，每封邮件会投递两份，钉钉里就是两张一模一样的卡片。代码里也做了兜底，`cc` 中与 `to` 重复的地址会被丢掉。
- `mode: always` 每天都发（包括「请假跳过」「当天没有工作痕迹」）；`mode: draft_only` 只在真的生成了草稿时发。
- 正文按 markdown 渲染，**单个换行会被并成空格**，所以要靠空行分段——邮件里的空行不是笔误。
- 发信失败不会影响草稿写入，只在 `cron.log` 里记一条 `[notify] 发信失败`。
- 正文里的长横线（`—` `–`）会被服务端拆成连续减号，代码里已统一换成 `-`。
- 同一天重复跑但**内容没变**时不会重复发信：标题+正文的指纹记在 `fill-log.jsonl` 的 `fingerprint` 字段里，扫全量日志比对，一致就跳过；内容真变了（比如换了工时正文）才会再发一封。
- **一天最多一封**：当天的通知今天已经发过，就不再发，哪怕正文因为多提交了几个文件而变了措辞。确实要再发一封时显式加 `--renotify`。

验证发信链路（不会写草稿、不改 `fill-log.jsonl`）：

```powershell
python -m dws_dm_agent.tools.zhiguan_worklog fill --notify-test
```

同上。注意：Windows 上 `dws.cmd` 会把带换行的参数截断（正文只剩第一行），所以程序优先用 `node <...>/dingtalk-workspace-cli/bin/dws.js` 直调，只在找不到 node 时才退回 `cmd /c dws.cmd`。

## 工时内容怎么写

智管的工时内容要**精简**，一条问题一句话，不要把定位过程、日志原文、命令输出都贴进去。参考长度：

- 单个项目：**400~600 字**
- 多项目：每个项目 200~300 字

推荐结构（就三句）：

```
一句话说今天做了什么。
当日主线：一句话说为什么做、结论是什么。
① 问题一：现象 → 根因 → 怎么修（带 MR / commit）。
② 问题二：……
```

写长了会用 `max_content_len`（默认 1800）截断，而且改成在句号处断开，不会留半截句子。但截断只是兜底，别指望它——**该精简就精简**。

超过 `soft_content_len`（默认 800）不会报错、也不截断，只在日志里提示一句 `[content] 正文 N 字，超过精简建议值`，方便你回头把话说短。

`## 工时内容` 里写什么就提交什么，所以这一节直接决定最终文字；上面那些定位细节留在日志正文里给自己看即可。

## 工作日判断

- 节假日表来自 `https://timor.tech/api/holiday/year/<年>`，按年缓存在 `zhiguan_state\holidays.json`，当年表超过 45 天自动重拉（新年放假安排通常年底才公布）。
- 法定假日不填；**调休补班的周末照常填**。
- 接口挂了就用缓存，缓存也没有就退回「周一到周五」。

## 目录

| 路径 | 用途 |
| --- | --- |
| `zhiguan_state\config.json` | 配置：映射表、工时规则、日历开关、MCP 参数 |
| `zhiguan_state\token.json` | OAuth 令牌（自动刷新，含 refresh_token） |
| `zhiguan_state\holidays.json` | 节假日缓存 |
| `zhiguan_state\fill-log.jsonl` | 每次运行的记录与 reviewUrl |
| `zhiguan_state\cron.log` | 计划任务的 stdout/stderr |
| `zhiguan_state\run_worklog.cmd` | 计划任务实际调用的入口 |
| `src\dws_dm_agent\tools\zhiguan_worklog.py` | 主程序 |

## 目录与归属

目录清单**不是写死的**。每次运行都会重新枚举 `workspace_root`（默认 `D:\workspace`）下的所有一级子目录，
再挑出当天真有活动的目录。工作目录以后会增加，不需要改代码；只有「新目录归到哪个项目」这件事需要一次确认。

判定顺序：

1. **显式映射**——`config.json` 的 `mappings` 里有这个目录，直接用里面人工确认过的 `targetId`。
2. **按名字归并到已有映射**——新目录名里含某个映射的名字或关键词（如 `区域产业链sql更新` → 区域产业链），就并到那条映射上。
3. **按名字自动匹配智管可填对象**——目录名和当天可填的项目/产品名、客户名、项目编号有足够长的公共片段就自动挂上去；两个项目打分并列时视为说明不了归属，**不猜**。
4. **兜底**——都匹配不上的目录，当天如果还有其他已映射项目，工时并到最重的那一项；如果当天**只有**这些目录在动，整体挂到 `fallback_target`，照样出草稿，邮件里点名是哪些目录。

已确认的映射：

| 目录 | 填写对象 |
| --- | --- |
| `D:\workspace\客户 B` | 某融资租赁风控预警系统升级（XXXX-0000000P） |
| `D:\workspace\客户 A` | 某信创改造项目（XXXX-00000000-000） |
| `D:\workspace\某平台ai-尽调项目` | 某租赁公司智能尽调（售前）（XXXX-00000000-000） |
| `D:\workspace\区域产业链通用`（及 `区域产业链sql更新`、`区域产业链交接信息`、`产业链数据加工`） | 某营销与风控数智化平台（CODE-0000-02，产品） |
| `D:\workspace\标准部署版本` | 技术产品架构V2.0（CODE-0000-03，产品） |
| `D:\workspace\dws-chat-agent` | 不单独记（工时并入当天主项目） |
| `D:\workspace\agent-radar` | 不单独记 |

相关配置：

```json
"workspace_root": "D:\\workspace",
"discovery": { "enabled": true, "match_min_chars": 3, "ignore": ["dsh-acl-recovery"] },
"fallback_target": { "name": "技术产品架构V2.0", "targetType": "PRODUCT", "targetId": "77ba332d-..." }
```

- `discovery.ignore` 里的目录永远不单独记（名字归一化后比较）。
- `match_min_chars` 是自动匹配要求的最短公共片段，调大更保守。
- `max_worklog_headlines`（默认 5）、`max_worklog_points`（默认 3）、`max_worklog_point_len`（默认 90）控制自动摘要的篇幅；写了 `## 工时内容` 就不受这几个值影响。
- 新增项目：在 `mappings` 里加一条，`targetType` 取 `PROJECT` / `PRODUCT`，`targetId` 用填写对象 id；不想单独记就写 `SKIP`。
- `targetType` 写 `DAILY`（日常工作）不会被使用——本项目约定所有工时都挂真实项目/产品。

## 手动运行

```powershell
$env:PYTHONPATH="D:\workspace\dws-chat-agent\src"
$env:PYTHONIOENCODING="utf-8"
cd D:\workspace\dws-chat-agent

# 看今天会填什么，不写草稿
python -m dws_dm_agent.tools.zhiguan_worklog fill --dry-run

# 真的生成草稿
python -m dws_dm_agent.tools.zhiguan_worklog fill

# 补某几天
python -m dws_dm_agent.tools.zhiguan_worklog fill --date 2026-11-03 --date 2026-11-04

# 覆盖重算（只覆盖本工具自己创建的草稿）
python -m dws_dm_agent.tools.zhiguan_worklog fill --date 2026-11-05 --force

# 检查令牌
python -m dws_dm_agent.tools.zhiguan_worklog check

# 只发一封测试邮件，验证通知链路
python -m dws_dm_agent.tools.zhiguan_worklog fill --notify-test

# 照常填但这次不发信
python -m dws_dm_agent.tools.zhiguan_worklog fill --no-notify

# 今天已经发过邮件，确实要再发一封
python -m dws_dm_agent.tools.zhiguan_worklog fill --force --renotify
```

## 计划任务

已注册 Windows 计划任务 `ZhiguanWorklogDaily`：

```
Task To Run:  cmd.exe /c ""D:\workspace\dws-chat-agent\zhiguan_state\run_worklog.cmd" fill --force"
Schedule:     Daily, 17:40
Logon Mode:   Interactive only（登录状态下才跑）
```

任务本身是**每天**触发，由脚本判断要不要填：法定假日和普通周末跳过，调休补班的周末照常填。这样调休不用手动改任务。

带 `--force` 是为了「生成的草稿你改了之后，第二天重跑不会因为『已有草稿』而报错跳过」——它只覆盖本工具自己创建的草稿，遇到你已在页面上提交的记录仍然跳过。
（`--force` 只在同一天重复运行时才有区别，正常一天跑一次的行为不变。）

查看 / 删除：

```powershell
schtasks /Query /TN "ZhiguanWorklogDaily" /FO LIST /V
schtasks /Delete /TN "ZhiguanWorklogDaily" /F
```

## 已知限制

- 只有 `worklog:read` 和 `worklog:draft` 权限，**不能直接提交**，必须你在 `reviewUrl` 页面点确认。
- 日历读的是你自己钉钉账号的日历：请假要建在日历上才会被识别（全天或 ≥4 小时的请假视为整天不在）。
- 只有登录状态才会触发（计划任务是 Interactive only）；机器关机的那天不会补跑，需要手动 `--date` 补。
- 令牌 1 小时过期，靠 refresh_token 自动续；如果 refresh_token 也被吊销，需要重新授权。
