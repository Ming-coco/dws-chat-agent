# 智管工时自动填写

工作日 17:40（周一到周五，节假日跳过，调休补班照常）按本机工作痕迹生成智管工时**草稿**，并把摘要和确认链接**发到你的钉钉邮箱**，你点链接确认后提交。

## 它怎么工作

1. 先看**钉钉日历**当天：整天或半天以上的「请假」直接跳过不填；有会议的按标题关键词挂到对应项目，写进该项目的日志内容里（挂不上的写进当天工时最多的项目），**不新增条目、不改变工时分配**。
2. **动态枚举** `D:\workspace` 下的一级子目录，挑出当天真有活动的那些——目录会不断增加，清单不写死（见下面「目录与归属」）。
3. 读每个目录当天的**手写工作日志**（`工作日志\YYYY-MM-DD-周X.md` 或 `YYYY-MM-DD_周X_工作日志.md`）。日志里如果有一个 `## 工时内容`（或「智管内容」「填写内容」）小节，就**原样**用它当工时正文——想精确控制提交上去的文字，写这一节即可；没有这一节才退回自动摘要。
4. 自动摘要**按仓库分组**列出当天的提交主题，例如 `zhike-cust-engine-cyl：[cyl] ChainNode 五个 mapper 迁移到 PostgreSQL。`。只有整天一个提交都没有、只剩文件改动时，才会退化成「改了 N 个文件」。
5. 提交**正文**里有「改之前 → 改之后」「提升 43 倍」这类结论的，会跟标题一起写成 `做了什么（优化了什么，提升多少）`，而不是只丢一个标题列表（见下面「写成什么样」）。
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
  "from": "someone@example.com",
  "to": ["someone@example.com"],
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

## 写成什么样

工时是写给人看的，光罗列「做了什么」等于没说。每条都要能回答**改了哪里**和**效果是什么**。

反面例子（只有动作，没有结论）：

```
fk-data：DataBIMonitorService 跑批性能优化。
```

正面例子（动作 + 优化点 + 数字）：

```
[fk-data] DataBIMonitorService 跑批性能优化（批量写：10000 行 392.7s → 9.1s（43x），
SQL 条数 10000 → 20；addEid：4 条 select max 由每页重跑改为整批取一次快照
（1096 次 → 4 次，1566ms → 523ms））。
```

要拿到这种效果，**提交信息里把结论写进正文**就够了，工具会自动配对：

- 标题写做了什么，正文的小标题写改在哪、正文的括注写数据。
- 带 `->`、`→`、`倍`、`%`、`ms`、`次`、`行` 这类结论的行会被挑出来，跟在标题后面。
- 折行的句子会先拼回整句再挑，所以「数字单独一行」也不会漏。
- 每个提交最多取 `max_highlights_per_commit`（默认 2）条，避免正文把工时撑爆。

没有正文、或者正文只有描述了没有数据时，就只写标题——不会瞎编。

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
| `D:\workspace\尽调项目` | 某租赁公司智能尽调（售前）（XXXX-00000000-000） |
| `D:\workspace\区域产业链通用`（及 `区域产业链sql更新`、`区域产业链交接信息`、`产业链数据加工`） | 某营销与风控数智化平台（CODE-0000-02，产品） |
| `D:\workspace\标准部署版本`、`D:\workspace\zhike标准产品版` | 技术产品架构V2.0（CODE-0000-03，产品） |
| `D:\workspace\dws-chat-agent` | 不单独记（工时并入当天主项目） |
| `D:\workspace\agent-radar` | 不单独记 |

两个目录共用同一个 `targetId` 时，工时和内容会合并成一条，不会重复占两个条目。

相关配置：

```json
"workspace_root": "D:\\workspace",
"discovery": { "enabled": true, "match_min_chars": 3, "ignore": ["dsh-acl-recovery"] },
"fallback_target": { "name": "技术产品架构V2.0", "targetType": "PRODUCT", "targetId": "77ba332d-..." }
```

- `discovery.ignore` 里的目录永远不单独记（名字归一化后比较）。
- `match_min_chars` 是自动匹配要求的最短公共片段，调大更保守。
- `max_repos_in_content`（默认 3）与 `max_subjects_per_repo`（默认 4）控制「列几个仓库、每个仓库列几条提交」。
- `max_worklog_headlines`（默认 5）、`max_worklog_points`（默认 3）、`max_worklog_point_len`（默认 90）控制手写日志摘要的篇幅；写了 `## 工时内容` 就不受这几个值影响。
- 新增项目：在 `mappings` 里加一条，`targetType` 取 `PROJECT` / `PRODUCT`，`targetId` 用填写对象 id；不想单独记就写 `SKIP`。
- `targetType` 写 `DAILY`（日常工作）不会被使用——本项目约定所有工时都挂真实项目/产品。

## 什么算「当天工作」

写入工时的依据有三种，都要求能说清「做了什么」：

1. **当天的 git 提交**——**只算你自己提交的**，别人的提交一律不算。按仓库分组，直接取提交主题。`Merge branch`、`index on …`、`On dev: …`、`能跑`、`WIP` 这类没有信息量的主题会被丢掉。
2. **手写日志**——`工作日志\` 下的 markdown。
3. **钉钉聊天**——当天**你自己发的**消息，用来补 git 说不出来的部分（协助排查、答疑、对接）。

只有文件 mtime 变了、但没有任何提交、手写日志和聊天的目录（切分支、批量解压、跑工具脚本都会这样），**不写进工时**，只在邮件里点名提示「只有文件改动、没有提交记录」。写「改了 34 个文件」既不能说明你做了什么，也没法用来考核，不如空着让你手动补。

`.idea`、`.acl-recovery`、`.pg-audit`、`node_modules`、`target`、`dist` 这类目录和 `*.class`、`*.log` 这类文件不计入文件数。

### 聊天怎么用

聊天只写进**已有条目的内容里**，不新增条目、不改工时分配——工时是按 git 提交和手写日志算的。

- 只取**你自己发的**消息，别人说的、机器人推的都不算。
- 群里能按关键词挂到项目的，写进那个项目；挂不上的和单聊，写进当天最重的那一条。
- 句子里**不出现人名**。写「在 深圳租赁、重庆产业链，跟进 API_ZD_URL、WQ20_USER_SCOPE_SWITCH 相关问题」，不写「跟邓南平聊了…」。人名对看工时的人没有信息量，还会把真正的内容挤掉。
- 应声（「好的」「收到」「是的」）、纯图片/文件名、随机 id 都会被过滤掉，剩下的技术词（配置名、服务名、类名、接口路径）才用来概括。
- 每个会话只留最后 2 条，避免刷屏的群把正文塞满。

聊天读不到（没登录、dws 不可用）只是返回空，绝不影响出草稿。

```json
"chat": {
  "enabled": true,
  "me": ["行弈-吴铭威"],
  "min_chars": 8,
  "max_messages_per_conversation": 2,
  "max_items": 4,
  "ignore_conversations": []
}
```

- `me` 是你在钉钉里的显示名，改昵称后要同步。留空则取全部消息（不建议）。
- `ignore_conversations` 里写群名片段，那些群不参与。

### 只看自己的提交

工时算的是「你做了什么」，所以 `git log` 会按作者过滤，别人的提交不进内容、也不进工作量统计。

- `config.json` 的 `git_authors` 决定哪些作者算你，匹配 `user.name <user.email>` 里的任意子串，大小写不敏感。默认 `["hangyi"]`。
- 没配 `git_authors` 时，退回读该仓库自己的 `user.email` / `user.name`；再取不到才用 `hangyi`。
- 被过滤掉的别人的提交会在 `cron.log` 里记一条 `[author] … 有 N 条别人的提交，不算进我的工时`，方便你核对是不是漏算了。

换机器或换了 git 身份时，记得把新名字加进 `git_authors`。

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
