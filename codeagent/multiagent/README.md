# 原生多 Agent 的模块边界

任务只有一套：`codeagent/tasks/` 的 Task 表、依赖和任务 API。
本目录负责执行这些任务，不创建另一套任务定义。

| 模块 | 职责 |
| --- | --- |
| `tools.py` | delegate_task / delegate_batch / execute_plan 及执行管理工具的固定 schema |
| `adapter.py` | 接入原 Agent、事件、权限、工作目录与成员检查点；不实现模型客户端或缓存 |
| `coordinator.py` | 启动、分波执行、继续、阶段验证与最终交付 |
| `executions.py` | 执行日志、检查点、阶段与原 Task 状态的事务同步 |
| `workspaces.py` | 原生协作的共享/隔离工作目录、检查点与最终本地回写 |
| `leases.py` | 按真实项目路径协调直接写执行、初始快照和交付；跨会话、跨运行时生效 |
| `verification.py` | 原生验证授权、证据发布、失败上报 |
| `context.py` | 原始用户要求、已批准方案的持久化上下文与恢复合并 |
| `reports.py` | 结构化成员自述与运行时工具证据的明确区分 |
| `artifacts.py` | 新增缓存识别；保护已跟踪文件和初始基线文件 |
| `presentation.py` | 等待原因、前置依赖与未知操作恢复入口 |
| `maintenance.py` | 结束后的工作目录占用、可校验归档和显式清理 |
| `migration.py` | 早期原生 collaborations 数据的只读导入，不负责旧 TeamRun 迁移 |

共享能力归属：

- `tasks/execution.py`：任务执行配置校验，依赖/文件/资源冲突分波。
- `runtime/validation.py`：跨平台检查命令、取消、进程清理、日志和诊断。
- `worktrees/snapshots.py`：本地 Git 快照、操作锁；保留原 HEAD 和暂存区。
- `worktrees/files.py`、`errors.py`、`scopes.py`：文件路径与状态校验、通用错误、路径范围语义。
- `events/collaboration.py`：用于实时 UI 的精简快照；完整证据按需读取。
- `codeagent/web/`：HTTP、持久化宿主、Agent factory。
- `web/src/features/multiagent/`：前端执行图、成员链路、状态同步和展示规则。

依赖方向是 `multiagent → tasks / runtime / worktrees`。共享模块不能反向依赖
`legacy/teams`。Web 兼容接入层可以引用旧 Team，使历史会话仍可查看和继续。

原生协作保留固定工具 schema、三条执行路径与普通 Agent 的缓存实现。
全局要求和报告格式只加入成员任务消息，不改写普通 Agent 的系统前缀。
四个执行管理工具是否合并回委派入口属于后续接口设计。
