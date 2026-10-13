# CodeWeft Web

本目录是 CodeWeft 的 React + TypeScript 前端。使用产品请从 [项目首页](../README.md) 开始；本文面向修改界面的开发者。
整体结构见 [架构说明](../docs/architecture.md)，提交流程见 [贡献指南](../CONTRIBUTING.md)。

## 本地开发

准备 Python 3.11+、Git，推荐 Node.js 22.12+。以下命令从仓库根目录执行，并假设已激活 Python 虚拟环境。
先安装并启动后端：

```powershell
python -m pip install -e ".[web]"
python -m codeagent.web.cli --workspace . --port 8765
```

另开一个终端，在仓库根目录安装前端依赖并启动 Vite：

```powershell
npm.cmd --prefix web ci
npm.cmd --prefix web run dev
```

打开 [http://127.0.0.1:5173](http://127.0.0.1:5173)。Vite 将 `/api` 代理到 `http://127.0.0.1:8765`，开发时无需先生成 `web/dist/`。
macOS / Linux 使用 `npm`。代理目标和固定开发端口在 [`vite.config.ts`](vite.config.ts) 中配置。
首次启动可以通过 **设置 → 模型与能力** 配置模型；设置表单将密钥提交给后端，读取配置时只返回密钥是否已保存，
普通运行界面使用脱敏的 `/api/runtime-config`，不从前端环境变量加载模型凭据。

## 检查与生产构建

```powershell
npm.cmd --prefix web test
npm.cmd --prefix web run typecheck
npm.cmd --prefix web run build
```

测试使用 Node 自带测试运行器；`typecheck` 运行 TypeScript 检查，`build` 包含类型检查并生成 `web/dist/`。
构建后由后端同源托管，访问 [http://127.0.0.1:8765](http://127.0.0.1:8765)。`dist/` 是生成产物，无需手工编辑。

## 代码入口

| 文件或目录 | 职责 |
| --- | --- |
| [`src/App.tsx`](src/App.tsx) | 页面状态、会话选择、设置路由和功能接入 |
| [`src/components/ChatWorkspace.tsx`](src/components/ChatWorkspace.tsx) | 对话、输入框与执行过程 |
| [`src/components/ComposerModeMenu.tsx`](src/components/ComposerModeMenu.tsx) | Agent / 只读执行方式，以及“先出方案” |
| [`src/components/PlanPreview.tsx`](src/components/PlanPreview.tsx) | 方案预览、版本和用户决策 |
| [`src/features/multiagent/`](src/features/multiagent/README.md) | 原生协作任务图、成员链路和状态同步 |
| [`src/components/InspectorPanel.tsx`](src/components/InspectorPanel.tsx) | 运行、任务和调试面板 |
| [`src/lib/api.ts`](src/lib/api.ts) / [`src/types/api.ts`](src/types/api.ts) | API 请求和传输类型 |
| [`src/hooks/useRunEvents.ts`](src/hooks/useRunEvents.ts) / [`src/store/runStore.ts`](src/store/runStore.ts) | SSE 订阅与运行状态 |
| [`src/styles.css`](src/styles.css) | 全局样式和主题 |
| [`tests/`](tests) | 界面行为、展示逻辑与契约测试 |

新任务使用 Agent 按需协作。`Team*` 组件用于历史 Team 的记录、继续执行和恢复；不要用原生协作执行阶段直接解释旧 Candidate / Attempt。

## 界面行为约定

下面保留维护界面时需要核对的交互与数据口径。

### 项目、会话与设置

左侧会话按完整工作区路径分组，点击项目标题可展开或收起。项目及组内会话按最近更新排序，
同名但不同路径的项目保持独立。收起状态保存在本机浏览器，桌面侧栏和移动抽屉共用；
搜索项目路径或会话时自动展开匹配结果，清空搜索后恢复收起状态。
顶部“新建项目”用于选择已有目录并创建首个会话；每个项目右侧的“＋”直接在该工作区新建会话。
新会话创建后自动选中并展开所属项目，创建期间禁用重复提交。

侧栏底部的“设置”统一管理通用外观、模型与能力、项目 MCP 插件和归档会话；
也可按 `Ctrl+,`（macOS 为 `Cmd+,`）打开。分类使用 `#settings/general`、
`#settings/models`、`#settings/mcp`、`#settings/archived` 地址，支持刷新与浏览器前进后退。
归档页显示各项目的归档会话，支持搜索、按项目筛选、查看聊天历史、恢复和永久删除。
恢复后会话回到原项目，“恢复并打开”可直接继续对话；永久删除需确认，工作区文件会保留。
归档列表从后端独立筛选并分页加载，不受普通列表默认 100 条的限制。

### 运行指标与上下文用量

右侧默认打开“运行”页，展示本轮耗时、模型调用、已执行工具及失败次数、工作区和会话 ID。
调用计数包含子 Agent；模型计数包括失败尝试和辅助模型调用，工具计数按已开始的调用 ID 去重。

“上下文用量”取最近一次主 Agent 的请求快照，与本轮累计 Token 消耗分别展示：

- 输入 Token 为本地保守估算，进度条包含输出预留，不代表 provider 实测输入。
- 窗口由后端用当前模型的服务地址和凭据自动查询模型列表中的 `context_window`，不需要手工配置，
  不用手工窗口覆盖接口结果。DeepSeek 的 `/anthropic` 地址使用同服务商的 `/models` 查询。
  官方旧别名 `deepseek-v4-flash` / `deepseek-v4-flash-vision-exp` 在列表未列出时，按
  [官方说明](https://api-docs.deepseek.com/updates/) 读取 `deepseek-flash` 的接口数据；此映射只适用于官方域名。
  成功结果缓存一小时、失败缓存一分钟，按接口、凭据和模型隔离；查询不产生模型生成调用。
  查询不到时显示具体原因和字符预算，不推测窗口，也不把输出上限或仅输入上限冒充总窗口。
  本地 SDK 测试/自定义客户端未实现查询时仍可使用其显式预算；辅助摘要的独立预算保持原机制。
- 同时展示字符压缩阈值、历史/请求消息数、本轮压缩次数、会话摘要版本和已折叠消息数。
- 工具结果裁剪量为当前请求保留的纯文本工具结果与原始历史的字符差，不跨请求累加，
  不包含已折叠到摘要中的历史、写工具参数裁剪或落盘前输出。
- SSE 实时更新并支持持久化事件回放；旧事件缺少的字段显示“不可用”。新字段需要重启后端后产生新请求。

费用暂不估算；项目尚无统一的模型定价与计费规则配置。


### Team Token 统计

以下口径用于历史 Team；新会话的成员协作展示由 `features/multiagent/` 负责。

运行面板的“团队累计”读取持久化模型调用记录，涵盖团队根 Run、后续主 Agent 轮次、成员调用，以及明确关联到团队的规划 Run。摘要、记忆等辅助调用和有 usage 的失败重试一并计入；同一调用不会因同时匹配多个归属条件而重复累计。单 Agent 模式仍显示“本轮累计”。

团队卡片按 Agent 展示输入、缓存读取、输出、调用次数与命中率；命中率是缓存读取输入之和除以总输入之和，不平均各次百分比。多个模型会显示模型列表。尚未返回或没有 usage 的调用会显示缺失数量，当前数值仅包含已知用量。前端结束事件不会覆盖已累计的逐次调用数据。

数据沿用团队快照轮询，即使规划 Run 的 SSE 已结束，成员用量也会继续刷新。旧数据库无需迁移，已保存且能明确归属团队的调用可直接重算；没有保存 usage 或关联关系的历史调用不会被猜测补齐。构建前端并重启后端后生效。
