# 普通 Agent 并行执行

工具批次与单层只读子 Agent 支持并行，CLI、普通 Web 和 SDK 共用执行核心。Team 不使用这套子任务运行时。

## 使用

- 读取独立文件时，一次响应可包含多个 `read_file`、`glob`、`grep` 调用。执行器保留调用顺序，把连续安全操作组成并行组；写入、shell、压缩、交互及未声明并发资格的工具形成串行屏障。
- `subagent(description)` 保留原有前台串行权限和结果格式。
- `subagent(description, access="read_only")` 可同轮并行，父 Agent 收齐报告再继续。只读子助手不开放 shell、写入、MCP、共享任务更新、直接用户交互或再次委派。
- 加 `run_in_background=true` 立即返回 `subagent_id`。主 Agent 可继续独立读取；写入或未知副作用操作之前、最终回答之前，runtime 等待本 Run 子任务。关闭并行时拒绝后台请求。
- `subagent_result(subagent_id, wait=false, timeout_seconds=30)` 查询，最长一次等待 60 秒；结果包含有界报告与完整输出的 `output_handle`，可用 `load_tool_output` 分页读取。`subagent_cancel(subagent_id)` 只取消该子任务。
- 后台完成消息明确标为 runtime 数据，在完整工具批次之间注入一次。不会为启动调用追加第二份 `tool_result`。模型应等待必要结果并综合回答，而不是持续轮询。

## 配置

| 环境变量 | 默认 | 范围 |
| --- | --- | --- |
| `CODEAGENT_PARALLEL_ENABLED` | true | 普通 Agent 工具调度 |
| `CODEAGENT_PARALLEL_TOOLS` | 4 | 一个 Root 与全部 child 共用的读取额度 |
| `CODEAGENT_PARALLEL_SUBAGENTS` | 3 | 每 Run 的子 Agent worker |
| `CODEAGENT_SUBAGENT_QUEUE_LIMIT` | 16 | 每 Run 已接收但未结束的子任务上限；满时返回明确错误 |
| `CODEAGENT_MAX_CONCURRENT_MODELS` | 8 | 进程级实际 SDK 请求，包含摘要与搜索改写 |
| `CODEAGENT_MAX_CONCURRENT_SUBAGENTS` | 8 | 进程级活动子 Agent |
| `CODEAGENT_MAX_CONCURRENT_TOOLS` | 16 | 进程级并行读取 |

进程级限额在首次使用时读取，修改后重启进程。SDK 可通过 `AgentConfig(parallel=ParallelConfig(...))` 配置 Run 额度。

工具使用 `ToolDefinition(effect="read", reentrant=True)` 声明可并发资格；模型 schema 不包含这些内部属性。声明者必须保证工具没有共享可变状态、写操作或调用间顺序依赖。自定义 execution wrapper 默认不允许并发。`search_code` 内部更新索引且可能调用辅助模型，暂不进入并行读取组。

SDK 自定义 hooks 需要提供独立的 `subagent_environment_factory` 才能并行委派；未提供时只允许前台串行 child，拒绝后台启动，避免共享 hook 状态。并行 child 的 `client.fork()` 必须返回独立客户端 wrapper；底层线程安全的 HTTP 连接池可以共享。标准 CLI/Web 工厂已经创建独立子环境。

## 执行与恢复边界

父 Run worker、子 Agent worker、工具执行器不互相占用同一饱和池。队列有界，等待受取消与总预算约束。工具结果按原调用顺序归集，完成事件可交错。返回过的成功结果保留，未执行和执行中结果未知分别记录。

子 Agent 的上下文、归档、恢复状态、活动监控及使用量独立；总使用量向父汇总一次。审批使用执行者作用域绑定，终端输入集中处理。预算按实际活跃执行分支计时，某一子任务等待不会暂停其他工作。

取消为协作式：已开始的线程必须实际退出后才释放资源，模型等待由 SDK I/O 期限兜底；不把发出停止请求伪装为已结束。在途请求可能使最终用量超过触发停止时的阈值，按真实 usage 计量。

Web 在同一 SQLite 事务中保存子任务状态和事件，提供：

```text
GET  /api/runs/{run_id}/subagents
GET  /api/runs/{run_id}/subagents/{subagent_id}/result?offset=1&limit=200
POST /api/runs/{run_id}/subagents/{subagent_id}/cancel
```

任务查询和取消校验所属 Run，结果以有界分页返回。SSE 重连可重放状态，UI 提供结果与单项停止。服务启动将仍活动的子任务标记 `interrupted`；不恢复 worker、不自动重放操作。完成报告归档仍可查询。

后台任务属于当前 Run，不在父 Run 结束后继续工作。它不是常驻任务或自动唤醒机制。并行子 Agent 与主 Agent 使用同一工作区；本 Run 的写屏障不隔离其他会话或编辑器。并行写代码、worktree 整合、嵌套委派不在本次范围。

## 验证记录（2026-09-29）

- 后端完整回归：858 项，850 项通过、8 项跳过。跳过的 8 项中，2 项 Windows 真实进程终止测试受沙箱权限影响，已在沙箱外单独运行并通过；其余 6 项为原有条件跳过。
- 随后补充事件日志故障下的收尾测试，针对最新实现运行 `python -m unittest tests.test_parallel_execution -q`：20 项全部通过。覆盖实际重叠、顺序屏障、预算、队列、父子取消、用量归因、归档、重启恢复，以及真实 Scheduler/FastAPI 的结果与停止接口。
- `npm.cmd test --prefix web`：41 项全部通过，包含子任务交错事件、重复工具 ID、重连去重与结果状态。
- `npm.cmd run build --prefix web`：类型检查与生产构建通过；构建器仍报告单个产物大于 500 kB 的体积提示。
- `git diff --check` 通过。
- 合成 I/O 对照：4 个独立操作各等待 100 ms，串行 0.406 秒、并行 0.109 秒，结果一致。使用假模型，未进行真实模型并行质量或成本基准；该数字不代表真实任务固定提速。
