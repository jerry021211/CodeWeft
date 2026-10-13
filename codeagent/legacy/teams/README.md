# 旧 Team 兼容实现

这里保留旧 TeamRun / AgentSession / Attempt / Candidate 流程。当前原生多 Agent
的入口和调度在 `codeagent/multiagent/`，不要在这个目录新增原生协作能力。

仍有实际调用方，不能直接删除的部分：

- `supervisor.py`：旧 Team 后台调度、暂停和恢复。
- `models.py`、`tasks.py`、`messages.py`：旧持久化数据的解析和约束。
- `lead.py`、`session.py`、`tools.py`、`tool_gate.py`：旧会话继续执行所需的工具和执行边界。
- `worktrees.py`、`candidates.py`、`managed_integration.py`、`integration.py`：旧 Attempt 的工作目录、候选验证、集成和交付。
- `lifecycle.py`、`coordination.py`、`integration_storage.py`、`observation.py`：由 Web repository 组合使用的旧流程存储与历史查询。

Web API、scheduler 和 factory 是兼容接入点，目前旧 HTTP 接口和历史会话仍然可用。
此次归档不修改数据库表、状态值、事件名、提示词或工具 schema，不自动恢复历史任务。
`web/storage.py` 内仍有旧 Team 的 repository 方法和建表逻辑，它们也仍在使用。

共享实现已移出：验证命令与诊断在 `runtime/validation.py`；Git 快照、路径规则、文件回写
校验在 `worktrees/`。Candidate 的权限和状态转换留在这里，不进入共享命令执行器。

后续只有在旧 API / 历史会话继续执行正式退役、并提供数据与工作现场迁移后，才可以删掉
这些兼容实现。移动目录不等于已经停止支持旧 Team。
