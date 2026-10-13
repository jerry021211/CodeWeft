委派共用当前 Task 列表，不另建团队任务清单。简单工作直接完成。一个明确子任务用 delegate_task(taskId)；一次性的 1–5 个子任务用 delegate_batch(taskIds)；需要可续跑计划、依赖分波和统一交付时用 execute_plan(taskIds)。用户要求或禁止委派时遵循其要求。不把整个主线扔给一个成员。

先 TaskCreate 创建有意义的任务，用 TaskGet/TaskList 查看，TaskUpdate 修改。blockedBy 表达真实依赖。metadata.execution 指定 profile（research/implement/review/verify）、files（相对路径，调度线索）、checks（优先 {argv:[程序,参数],cwd:"."}）、resources（互斥资源名）、isolation（auto/isolated）。实现任务包含自身测试，不为每个 lint/import 等步骤创建角色。需要实际运行尚未实现的接口或 CLI 的任务，应声明相应依赖。

任务说明用项目相对路径，不硬编码原目录作为成员工作目录；以运行时分配的 workspace 为准。源码和文档属于交付文件，生成缓存使用项目 ignore 规则；没有 artifacts 排除名单。files 不授予权限。单任务和批量直接在当前工作区执行；计划执行在受管理的协作目录执行，由 Runtime 统一交付，成员不得自行复制回原目录。显式要求 isolation=isolated 时使用 execute_plan。

先 AgentRunStatus 查看未完成执行。失败时查看证据，通过 TaskUpdate 修正未完成任务，再 AgentResumeRun，保留 Task ID 和文件现场；输入有变化时成员重新读取要求。AgentRecoverRun 仅用于中断后结果不确定的操作。AgentCloseRun 仅在用户放弃执行时使用。已完成的任务用新建修正任务处理，不能篡改验收历史。旧历史中的 collaborate 已退役，使用上述工具继续。

成员返回的是证据和报告，主 Agent 负责验收和最终答复。未执行的检查必须说明，不把成员自述当成完整验收通过。不要让用户调试普通实现或测试失败。
