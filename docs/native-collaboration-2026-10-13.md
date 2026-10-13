# 原生多 Agent：工具拆分与统一 Task

本轮在原生协作内核上完成工具拆分，任务定义与状态统一回原 Task 系统。原来的 `collaborate(action=...)` 不再注册；普通 Agent 不要求用户选择 Team 模式。

## 三条执行路径

| 入口 | 输入 | 工作方式 |
| --- | --- | --- |
| `delegate_task` | `taskId` | 一个边界明确的已有 Task，在当前项目目录执行、检查并返回报告 |
| `delegate_batch` | `taskIds`，1–5 项 | 按依赖、文件冲突、共享资源分波，在当前项目目录执行并聚合结果 |
| `execute_plan` | `taskIds`，1–32 项 | 持久依赖计划，在受管理的协作目录执行，分波验证，最后验证并回写本地 |

单任务和批量模式直接修改当前项目文件，失败不会自动回滚这些修改；需要统一隔离与交付时选择计划路径。

三条路径共享成员执行器与调度算法。单任务、批量任务也有检查点，但不要求 Git，不经过最终回写。显式指定 `isolation=isolated` 时使用 `execute_plan`，不会静默忽略隔离要求。

主 Agent 根据任务边界选择入口，运行时负责确定性约束：依赖通过后才能继续；声明的文件/资源冲突不并行；未知写入范围保守串行。默认最多 3 个并行成员。共享目录的写工具与不透明命令串行运行，模型调用可以并行。没有按前后端目录写死业务规则，也不声称是学习型动态负载均衡。

## 只有一套任务数据

任务创建、查看、修改仍是 `TaskCreate`、`TaskGet`、`TaskList`、`TaskUpdate`；依赖仍用 `blockedBy` / `addBlockedBy`。任务编号、标题、描述、负责人、状态来自原 `tasks` 表，前端任务面板直接读它。

成员执行配置存入现有 Task 的 `metadata.execution`：

```json
{
  "profile": "implement",
  "files": ["src/", "tests/"],
  "checks": [{"argv": ["python", "-m", "unittest"], "cwd": "."}],
  "resources": [],
  "isolation": "auto"
}
```

`profile` 支持 research / implement / review / verify；只读角色不能执行命令检查。`files` 是调度分工线索，不是权限授权或沙箱。源代码和文档不能被声明为排除项；生成缓存使用项目 ignore 规则。已删除旧 `artifacts` 排除机制，避免把源码漏出最终交付。

主 SQLite 数据库新增 `agent_executions` 和 `agent_root_recovery`，仅保存 Task ID、执行阶段、成员检查点、工作目录与交付日志。执行记录不存另一份任务定义；执行阶段与原 Task 状态在同一事务提交，异常时一起回滚。执行阶段用于区分“成员返回、检查通过、最终回写”，不代替 Task 的任务状态。

个人 `todo_write` 仍是当前 Agent 的步骤清单，沿用原有机制；成员调度只使用 Task，Worker 不另建任务或待办清单。

## 独立的管理工具

- `AgentRunStatus(executionId?)`：查询执行进度、成员报告、验证证据及阻塞原因。
- `AgentResumeRun(executionId, additionalTaskIds?)`：继续保留现场，可追加已有修正 Task。
- `AgentRecoverRun(executionId)`：仅处理操作结果未知的中断，使用现有授权通道核对现场，不盲目重放。
- `AgentCloseRun(executionId)`：用户放弃时关闭执行，取消未完成任务，保留文件。

普通错误交回主 Agent，通过 TaskGet/TaskUpdate 修正要求或验证命令后继续，不再要求用户给通用测试失败填人工说明。已接受成果只修改检查配置时可仅重验；需要改代码则新建修正 Task，通过 additionalTaskIds 接回同一次计划。运行中的任务由执行器维护状态，避免模型提前把未通过检查的任务标成完成。

停止会传递到成员和验证进程；刷新、重启不会自行恢复。明确继续后复用任务 ID、工作目录与检查点。同版本任务最多尝试 3 次，执行累计最多 96 次；修改未完成任务输入后保留历史次数，并清除过期模型检查点。

## 前端

对话里的进度区分别标注“单任务委派 / 批量委派 / 计划执行”，成员显示原 Task 编号和短标题。工具时间线使用明确中文名称。协作快照、成员标签和检查事件接入实时订阅与历史活动回放。

原任务面板保留依赖图，完整显示执行中、待执行、等待前置、需要处理、完成、取消；失败任务不会消失。继续协作任务时先查询执行记录，避免重复创建 Task 或绕过执行器手动完成。单任务、批量完成不会误称经过受管理的本地回写。

## 保留、删除与兼容

保留原 Task API、Task 表、任务活动流、依赖面板、Agent 主循环、模型客户端、上下文压缩与缓存组装。调度校验放到 `codeagent/tasks/execution.py`；`codeagent/multiagent/` 负责执行适配、并发、工作目录、恢复和验证。

删除本轮取代的 `multiagent/tool.py`、`multiagent/repository.py`、`multiagent/plans.py` 和 `prompts/templates/collaborate.md`。新入口在 `multiagent/tools.py`；计划校验移回 Task 模块。

旧原生 `collaborations/state.sqlite3` 首次使用新执行工具时只读导入：任务进入原 Task 表，依赖和成员检查点映射到新 Task 编号，旧成员事件身份与工作目录保留。导入事务化、可重复调用，不删除原始数据库，不自动启动旧任务。

历史 TeamRun/Candidate 兼容代码已集中到 `codeagent/legacy/teams/`，仍由旧 API、历史会话恢复和 repository 调用。新路径不生成这些记录，也不再从旧 Candidate / IntegrationService 借用验证和回写函数。共享验证归 `runtime/validation.py`，Git 快照和文件校验归 `worktrees/`；新原生重复任务系统已撤掉。旧普通会话继续按检查点保留原工具集合；新对话和此前原生协作会话使用拆分工具。

目录和依赖边界见 [原生协作模块说明](../codeagent/multiagent/README.md) 和 [旧 Team 兼容边界](../codeagent/legacy/teams/README.md)。删除了原目录中的旧实现和未使用的 `BackgroundTaskRunner` 别名；没有保留第二份转发模块。旧 Team 仍在使用的执行能力保留，数据库、现场和 HTTP 协议不迁移。

新工具集与旧 collaborate 的请求前缀必然不同，升级首次调用可能影响缓存命中。之后 schema 不随 Task 创建、成员数量或阶段切换；普通 Agent 既有工具与缓存路径保留。测试覆盖任务创建、委派前后系统前缀及工具 schema 稳定性。

## 本地文件

计划写任务从本地未提交文件快照开始，内部使用本地 Git 提交记录成果；不推送远程。共享协作目录按需使用隔离 worktree，成果真实合并后检查。最终将结果与当前本地文件合并预览、检查、逐文件回写，保留原 HEAD 和暂存区。同期用户改动、冲突或未知进程结果会保留现场而非覆盖；部分回写中断可恢复，不重复执行已完成成员。

## 验证

测试使用脚本化模型驱动真实 Agent 工具链，并使用临时目录中的 SQLite、Git 与子进程。覆盖真实双 Worker 并行、依赖锁定、原 Task 状态同步、事务回滚、停止续跑、检查修改与追加修复、旧数据导入、原目录/暂存区保护、实时事件与回放、普通 Agent 与缓存。

2026-10-13 验证结果：后端两组分别 79 / 156 项通过，扣除重复用例共 215 项；前端 109 项通过；TypeScript 与 Vite 构建通过。现有 bundle 超过 500 kB 的构建提示仍存在。

运行命令：

```text
python -X utf8 -m unittest tests.test_task_delegation tests.test_native_execution tests.test_native_adapter tests.test_native_collaboration tests.test_permissions_hooks tests.test_web_api
python -X utf8 -m unittest tests.test_tasks tests.test_personal_todos tests.test_prompts tests.test_plan_mode tests.test_prefix_cache tests.test_context_nonteam_regressions tests.test_web_scheduler tests.test_native_adapter tests.test_task_delegation tests.test_team_persistence tests.test_team_tasks
npm --prefix web test
npm --prefix web run build
```

没有调用付费模型重新执行用户的 StockLite/SeatFlow 长任务。自动化通过不能保证模型每次拆分都合理，真实项目仍需要端到端试用。
