# 智管工时自动填写

工作日 17:40（周一到周五，节假日跳过，调休补班照常）按本机工作痕迹生成智管工时**草稿**，你在页面上确认后提交。

## 它怎么工作

1. 先看**钉钉日历**当天：整天或半天以上的「请假」直接跳过不填；有会议的按标题关键词挂到对应项目，写进该项目的日志内容里（挂不上的写进当天工时最多的项目），**不新增条目、不改变工时分配**。
2. 读 `D:\workspace` 下每个项目目录当天的**手写工作日志**（`工作日志\YYYY-MM-DD-周X.md` 或 `YYYY-MM-DD_周X_工作日志.md`），从中取主题、主线、章节和要点作为填写内容。
3. 没有手写日志时，退化为 `git log` 当天提交 + 文件修改时间统计。
4. 按各项目当天的工作量把 7.5 小时分配下去（步长 0.5h）。
5. 调智管 MCP 的 `save_worklog_draft` 存成草稿，并把 `reviewUrl` 写进 `zhiguan_state\fill-log.jsonl`。

同一天已经有草稿或已提交记录时默认跳过，不会重复生成。

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

## 映射表

`config.json` 的 `mappings` 决定「哪个目录的活记到哪个填写对象」。已确认：

| 目录 | 填写对象 |
| --- | --- |
| `D:\workspace\深圳租赁` | 深圳市融资租赁2025年风控大数据预警系统升级（XYKJ-2025204P） |
| `D:\workspace\招银` | 招银金租2026年信创改造（售前）（XYKJ-20260115-466） |
| `D:\workspace\智客ai-尽调项目` | 长江联合租赁 2026Ai 智能尽调（售前）（XYKJ-20260723-1546） |
| `D:\workspace\重庆产业链通用` | 有数智客营销与风控数智化平台（YSCP-2026-02，产品） |
| `D:\workspace\dws-chat-agent` | 不单独记（工时并入当天主项目） |
| `D:\workspace\agent-radar` | 不单独记 |

新增项目：在 `mappings` 里加一条，`targetType` 取 `PROJECT` / `PRODUCT`，`targetId` 用填写对象 id；不想单独记就写 `SKIP`。
`targetType` 写 `DAILY`（日常工作）不会被使用——本项目约定所有工时都挂真实项目/产品。

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
```

## 计划任务

已注册 Windows 计划任务 `ZhiguanWorklogDaily`：

```
Task To Run:  cmd /c "D:\workspace\dws-chat-agent\zhiguan_state\run_worklog.cmd fill"
Schedule:     Daily, 17:40
Logon Mode:   Interactive only（登录状态下才跑）
```

任务本身是**每天**触发，由脚本判断要不要填：法定假日和普通周末跳过，调休补班的周末照常填。这样调休不用手动改任务。

查看 / 删除：

```powershell
schtasks /Query /TN "ZhiguanWorklogDaily" /FO LIST /V
schtasks /Delete /TN "ZhiguanWorklogDaily" /F
```

## 已知限制

- 只有 `worklog:read` 和 `worklog:draft` 权限，**不能直接提交**，必须你在 `reviewUrl` 页面点确认。
- 日历读的是你自己钉钉账号的日历：请假要建在日历上才会被识别（全天或 ≥4 小时的请假视为整天不在）。
- 只有登录状态才会触发（计划任务是 Interactive only）。
- 令牌 1 小时过期，靠 refresh_token 自动续；如果 refresh_token 也被吊销，需要重新授权。
