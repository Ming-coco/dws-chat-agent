# E2E 页面体检工具

部署后自动验证 Web 系统是否正常。回答的问题不是"功能对不对"，而是
**"这次部署有没有把东西搞坏，坏在哪一层"**。

## 用法

```bash
export E2E_BASE_URL=https://webapp.example.com    # 被测系统地址
export E2E_USER=xxx                          # 账号
export E2E_PASS=xxx                          # 密码

python -m e2e_runner discover --config example-webapp   # 发现菜单
python -m e2e_runner run      --config example-webapp   # 跑体检
python -m e2e_runner run      --config example-webapp --json   # 给 agent 用
```

退出码：`0` 通过 / `1` 有失败 / `2` 配置错误 / `3` 登录失败 / `4` 发现失败 / `5` 需人工确认

## 三层归因

判定不只说"有问题"，还要说清在哪一层：

```
接口层：接口 200 且有列表数据？
    ↓ 有
渲染层：页面把数据画出来了吗？
    ↓ 没画           → FAIL  前端渲染问题（确定是 bug）

    ↓ 200 但空数据
服务层：后端日志里有异常吗？
    ↓ 有             → WARN  后端报错被吞了（最隐蔽）
    ↓ 无             → WARN  可能真的没数据，需业务确认
```

## 菜单怎么来

**不写死菜单树**。菜单不管来自代码、接口还是数据库，最终都要通过 HTTP
下发给前端，所以拦截响应比猜 DOM 更准。

自动从登录后的所有 XHR 响应里找「像菜单」的 JSON：有名称字段、有路径
字段、有层级。找到多个候选时按「带路径的节点数」打分挑最像的。

实测实测环境 一次就找到了 `getUserPermission`，10 个一级菜单、31 个带路径节点。

## 路径拼接的坑

完整路径 = **一级菜单 url + 叶子菜单 url**，中间层不参与。

因为前端实际是这么拼的：

```js
this.$router.push(menu.href + this.activeMenu.submenu)
```

逐层递归拼会出错，实测 404：

```
我的 /mine + 我的 /collections/index + 关注企业 /collections/index
  逐层拼   → /mine/collections/index/collections/index   ✗
  一级+叶子 → /mine/collections/index                     ✓
```

## 加载中不等于没数据

固定 `sleep` 会把"还在加载"当成"没有数据"。实测踩过：商机雷达页
`sleep(2500)` 后截图，表格还是空的，被判成渲染失败，实际再等一会就好了。

现在改成语义等待（`loading.py`）：

1. 等 loading 遮罩消失（Element / Ant Design / skeleton）
2. 等页面结构稳定（连续两次采样指纹不变）
3. 还看到遮罩就标记 `loading_stuck`，判定为 WARN 而不是"没数据"

## 表格行数不能只看框架类名

Element 用 `.el-table__row`，Ant 用 `.ant-table-row`，但有的系统（实测遇到）
`tr` 直接挂在 `table` 下没有 tbody，两个类名都匹配不到。

退回到数「所有含 td 的非表头 tr」。

## 图表页不是表格页

「风险监控」页接口返回 23 个请求，页面用 ECharts 和数字卡片展示，
表格 0 行。如果只判「有数据 + 表格 0 行 = 失败」就会误报。

所以加一条：有图表元素或页面文字够多，说明数据用别的方式展示出来了。

## 目录

```
src/e2e_runner/
├── cli.py        命令行入口
├── config.py     配置加载与校验
├── browser.py    浏览器启动 + 信号采集
├── loading.py    加载完成判定（语义等待）
├── login.py      登录（含协议勾选、双密码框等坑）
├── discover.py   菜单发现（拦截接口 + 打分选优）
├── navigate.py   遍历引擎
├── detect.py     三层归因判定
├── logs.py       远程抓容器日志（待接入）
└── report.py     报告输出
configs/
└── example-webapp.json
```

## 与 agent 的关系

这个包完全独立：不 import `dws_dm_agent` 的任何东西，可以单独执行。

agent 侧通过 `--json` 拿结构化结果，转成自然语言。

## 已知限制

- 验证码登录不支持
- 菜单写死在前端、没有接口的系统，需要改用手写配置
- 业务断言（"这个列表应该有数据"）不在范围内，只做技术层面的异常检测
- 远程日志抓取还没接（`logs.enabled` 默认 false）
