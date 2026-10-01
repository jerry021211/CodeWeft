# 普通 Agent 并行能力方案（调研稿）

实现已落地，使用方式、配置、边界和验证记录见 [普通 Agent 并行执行](parallel-execution.md)。下文保留调研时的设计依据。

日期：2026-09-29。分析对象：当前工作树，分支 `main`，基准提交 `a99a514f2d89a8066272b70f5546edd4023bc890`；包含现有未提交修改。本文是方案，未实现功能、未执行模型实验或测试。

建议实现两层能力：同一模型响应内的安全工具并行，以及同一 Run 内的子 Agent 并行。先完成前台并行，再加入后台任务生命周期。保持现有同步 Python Agent loop，通过有界线程执行器承载 I/O 并发。

范围包括 CLI、普通 Web、SDK 入口；不依赖 TeamSupervisor、Team Session、消息总线或候选合并流程。首版并行子 Agent 限制为只读、单层；已有可写子 Agent 保持前台串行行为。

## 调研依据

- Claude API 可在一次响应中返回多个 `tool_use`，实际执行顺序由客户端决定。所有调用必须有对应结果，并在下一条 user 消息中集中返回；未执行调用也要返回错误结果。这是协议能力，不等同于执行器已经并发。[官方工具并行文档](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use)
- Claude Code 通过 Agent 工具委派子任务；前台等待，后台可与主会话同时运行。当前文档允许嵌套，并提供深度、并发限制；后台审批可回到主会话。因此“不能嵌套、后台不能审批”不应当作当前结论。[官方子 Agent 文档](https://code.claude.com/docs/en/sub-agents)
- 子 Agent 通常使用独立历史，中间工具输出留在子上下文，仅最终报告返回父 Agent；fork 是继承父历史的另一种模式。SDK 提供父工具调用关联与子 Agent 恢复能力。[官方 SDK 文档](https://code.claude.com/docs/en/agent-sdk/subagents)
- 并行修改代码需要另外考虑文件隔离；Claude Code 提供 worktree 隔离。本项目首期不实现并行写入，后续单独定义基线、未提交文件处理和结果集成。[官方 worktree 文档](https://code.claude.com/docs/en/worktrees)

公开 Python SDK 中的 [client.py](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/client.py) 和 [query.py](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py) 可参考停止任务、控制响应关联及终态处理。其 [transport](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/transport/subprocess_cli.py) 包装 CLI 进程，不能据此推断每个子 Agent 的内部线程/进程模型。本次未在官方 Claude Code 仓库找到完整调度器源码；下文线程池、队列及存储方案属于本项目设计。

## 项目现状

| 位置 | 已确认行为 | 对方案的影响 |
| --- | --- | --- |
| `codeagent/web/scheduler.py:71` | 普通 Web 已有 FIFO 多会话调度，默认 4 个 Run 并发 | 保留外层调度，本次增加 Run 内并发 |
| `codeagent/agent.py:591`、`:607` | `_execute_tools` 同步遍历本轮工具 | 主要改造入口 |
| `codeagent/agent.py:748`、`:768` | `_spawn_subagent` 直接调用 `subagent.run` 等最终报告 | 多个子 Agent 目前也串行 |
| `codeagent/agent.py:827`、`:859` | 独立 Agent/context/输出目录，默认禁止再次委派 | 可复用上下文隔离 |
| `codeagent/cli.py:307`、`codeagent/web/factory.py:263` | 标准入口为子 Agent 创建新 registry/context/Todo | 改为统一执行环境构建合同 |
| `codeagent/tools/registry.py:122` | `copy_without` 保留原绑定 handler | 裸 SDK fallback 可能共享可变工具实例 |
| `codeagent/runtime/execution.py` | RunBudget 已加锁，主子共用总预算 | 改并发语义，不另造预算体系 |
| `codeagent/events/`、`codeagent/web/storage.py:7938` | 已有父子身份、持久化事件序号、SSE 回放 | 扩展普通子任务事件即可 |

`runtime/background.py` 当前是 TeamSupervisor，不把它当作普通后台子 Agent 执行器。普通 Task CRUD 是规划清单，也不承担 worker 的运行生命周期。

## 第一阶段：前台并行

**工具调度。** 新增 `runtime/tool_executor.py`，拆分准备、执行、归集。registry 持有内部执行属性（例如 `effect` 与 `reentrant`），不混入发给模型的 JSON schema。未声明或未经审计的工具默认独占。

| 工具类型 | 第一阶段策略 |
| --- | --- |
| `read_file`、`glob`、`grep`、归档读取 | 审计通过后，可在连续安全批次内并行 |
| `write_file`、`edit_file`、任意 `bash` | 独占执行，形成批次屏障 |
| `compact`、`ask_user`、Task/Todo 更新、memory 写入 | 串行控制操作 |
| MCP、自定义工具 | 默认独占，不能因名称或提示词像只读就放行 |
| `search_code` | 暂时独占；其索引、实例状态及辅助模型调用须先做并发适配 |
| 明确只读的子 Agent | 路由至独立子 Agent 执行器 |
| 继承现有写权限的子 Agent | 保持前台串行 |

按原工具顺序切分批次。例如 `read A → read B → edit C → read C` 执行为 `[read A || read B] → edit C → read C`，不跨写操作调换顺序。

协调线程负责权限/参数检查、预算预留、hooks 及结果归集；worker 只执行已获准的操作并产出结果。最终由协调线程按原 `tool_use` 顺序更新结果槽、记录上下文证据、归档、执行整批预算整理。下一轮模型请求只能看到完整配对的历史。自定义 hooks 保持串行调用；无法隔离的扩展不进入并行组。

**子 Agent 调度。** 新增 `runtime/subagents.py`，负责子执行 ID、状态、限额及回收。保留旧调用 `subagent(description)` 的前台语义与原权限行为；新增 `access="read_only"` 作为可并行的显式合同。只读由工具白名单和运行时检查落实，不能仅依赖任务描述。

首期只读子 Agent 不开放任意 shell、外部 MCP、共享 Task 更新及 memory 写入；工具范围内的检索/读取能力用于研究和审查。保持现有标准 child 不注册 `ask_user` 的限制，缺失信息由报告交回父 Agent；不直接照抄包含交互工具的 Discuss 白名单，避免多个 child 争用 CLI stdin。依赖任意命令或文件修改的验证仍由主 Agent 或现有前台串行子 Agent 完成。

同一响应内多个只读子 Agent 可重叠运行，父 Agent 在整批收齐后继续。模型仍负责拆分独立任务，runtime 负责执行约束。提示词应建议合并独立读取、只委派需要多步且能独立返回报告的工作，不为一个小读取额外创建 Agent。

**容量和线程。** 初始建议每 Run 最多 4 个安全工具、3 个子 Agent，同时设置进程级子 Agent/模型请求上限及有界队列；具体值通过基准调整。模型请求限额须覆盖主请求、子请求、摘要和搜索改写，避免外层 4 个 Run 将负载成倍放大。限额是本项目选择，不照搬 Claude Code 默认值。

父 Run worker、子 Agent executor、工具 executor 分开；等待子 Agent 的协调逻辑不得占满子 Agent 要使用的工具池。提交必须在分配额度后执行，取消排队任务不调用模型。关闭并行后走原串行路径。

## 第二阶段：后台执行与收集

在第一阶段执行器上增加以下工具合同，字段名可在实现时最终确定：

```text
subagent(description, access="read_only", run_in_background=true)
  -> subagent_id, status, output_handle

subagent_result(subagent_id, wait=false, timeout_seconds=...)
  -> 状态、报告或有界输出引用

subagent_cancel(subagent_id)
  -> 取消请求已登记 / 已结束
```

未提供新参数的旧调用保持兼容。首版后台仅接受明确只读任务；写入请求返回可操作错误，不静默提升权限。

状态至少包含 `queued / running / waiting_approval / cancelling / completed / failed / cancelled / interrupted`。状态更新及终态落库幂等，任务归属 Run；结果查询、取消和输出读取校验 Run/工作区归属。

新增普通 `subagent_runs` 表与输出索引，记录父 Run、父工具 ID、agent ID、状态、时间、结果路径、交付标记。Web 复用现有 SQLite；CLI/SDK 可用同合同的内存注册表与输出目录。首版不承诺进程重启后继续运行：未结束任务标记 `interrupted`，不自动重放。

后台工具先返回一次启动结果。子任务完成写入 completion queue；主线程只在完整工具批次结束、下一轮模型请求前等安全边界消费，作为明确标记的 runtime 通知注入，报告视为子任务数据。以子任务 ID/完成版本去重，禁止再次返回相同 `tool_use_id` 的第二份结果。

首版后台任务属于当前 Run。父 Agent 可以继续独立读取/分析，但在当前工作区执行写入或不明副作用操作之前，先收齐本 Run 的只读子任务，避免边读边改。父 Agent 准备最终回答时，runtime 必须等待仍需收集的子任务并安排汇总；禁止父 Run 已完成、SSE 已关闭后仍留活动子任务。这里的后台不是脱离 Run 的常驻任务。

这些约束只管理本 Run。现有其他会话或外部编辑器仍可能改动同一工作区；独立上下文不提供文件快照一致性。跨会话隔离和多方并行写入留待后续 worktree 方案。

## 两阶段都必须处理的执行合同

1. **环境隔离。** 每个 child 独立工具实例、Todo、ContextManager、RecoveryRuntime、activity 与日志身份。标准入口统一 factory；裸 SDK 需提供安全 clone/factory 合同，无法克隆的自定义有状态组件回退串行。特别检查 Bash 的 cwd、取消回调及 `bind_runtime`。
2. **取消。** 父取消传播给所有 child；child 本地取消不取消父/兄弟。先停派发，再等待实际退出、收尾，最后释放容量。`Future.cancel()` 不代表正在运行的线程已终止；模型和工具沿用协作取消并补必要 I/O 期限，超时仍不明的操作记录 unknown，不假称已停止。
3. **预算。** 保留 RunBudget 的锁和原子计数；将全局 `_paused` 改为执行者状态集合，至少一个执行者在工作时累计活跃墙钟时间。父等待 children 不重复计时，一名 child 等审批也不冻结其他人的执行时间。已有在途请求可能在总 token 阈值触发后返回，必须照实计量，不宣称绝对不会超额。
4. **Token 归因。** 当前 `Agent._make_result` 用共享总量前后差，并行会包含兄弟消耗；改为按 agent/call 记录，再汇总 Root 总量，避免子任务重复归因。
5. **审批。** 后端审批队列可共享，但每执行者使用独立 broker facade，绑定自己的 activity、取消信号、agent/tool ID。CLI stdin 集中处理。Web 审批展示子任务来源，禁止兄弟互相覆盖 broker 的活动指针。
6. **结果真实性。** 已完成结果保留；未开始明确未执行；已经发出但无法确认结果时标 unknown。单个普通错误不丢弃其他结果；根预算耗尽/父取消才触发整组停止。保留每个 tool_use 的唯一结果及终态事件。
7. **搜索服务。** `search_code` 对用户工作区只读，但内部更新索引并可请求模型。并行开放前，需隔离请求状态、为共享索引更新加工作区级同步或使用不可变快照，并将辅助调用计入限额/预算。
8. **前端与事件。** 复用现有子 Agent 面板，增加排队/运行/取消状态、耗时、结果和单项停止入口。事件按实际完成时间流出，历史按原调用顺序归集。持久化序号与结果交付不能依赖 worker 的完成顺序。

## 预计改动面

| 模块 | 改动 |
| --- | --- |
| `agent.py`、`tools/base.py`、`tools/registry.py` | 批次执行器接入、调度属性、稳定结果归集 |
| 新增 `runtime/tool_executor.py`、`runtime/subagents.py` | 工具调度、子执行器、配额及回收 |
| `tools/subagent.py`、子 Agent prompt | 向后兼容的新参数、只读合同、并行使用指引 |
| `cli.py`、`web/factory.py` | 统一独立子环境，显式注入运行时身份 |
| `runtime/cancellation.py`、`execution.py`、`permissions/broker.py` | 分层取消、按执行者计时、审批来源 |
| `events/models.py`、`anthropic_client.py` | per-agent usage、请求限流、独立 activity 绑定 |
| `web/storage.py`、`scheduler.py`、`api.py` | 第二阶段状态存储、终态收尾、结果和取消接口 |
| `web/src/store/runStore.ts`、`hooks/useRunEvents.ts`、现有子 Agent UI | 交错事件、后台状态、结果与停止入口 |
| `config.py`、`.env.example`、README | 开关、限额、串行回退与使用边界 |

## 验收

使用假模型和 Event/Barrier 验证真实重叠，不仅凭耗时判断；延迟工具基准另行测量。

- 两个安全工具/子 Agent 同时进入执行区；活跃数不超限，额外任务按有界策略排队或明确拒绝。
- 混合读写批次遵守屏障；父 Run worker 全部占用时子执行仍能推进。
- 乱序完成、单项失败、审批拒绝、批次取消后仍一一配对，成功结果不被覆盖。
- histories、Todo、输出目录、cwd、activity、取消信号、Token 归因不串扰。
- A 等审批、B 继续执行时预算时间推进；多 child 总用量只计一次。
- 后台启动只产生一份工具结果，完成通知只交付一次，父结束前收齐所需子任务。
- 单项取消不影响兄弟，重启标 interrupted，SSE 重连补全交错事件。
- 旧 `subagent(description)`、Discuss 只读、上下文归档、防循环与关闭并行后的串行行为保持可用。

重点回归现有 `test_agent.py`、`test_tool_cancellation.py`、`test_execution_budget.py`、`test_context_nonteam_regressions.py`、`test_web_concurrency.py`、`test_web_storage.py` 和前端事件测试；新增独立的工具/子 Agent 并行测试。真实模型基准比较串行、前台并行、后台并行的成功率、墙钟时间、总 Token 和峰值请求数，不预先承诺固定倍数提速。

建议以第一阶段作为首个交付，再完成第二阶段。首期不加入嵌套委派、对等消息、自动组队、脱离父 Run 的后台会话及并行写代码。
