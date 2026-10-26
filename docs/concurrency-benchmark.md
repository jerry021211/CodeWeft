# 当前项目的并发测试

## 先明确测什么

Web 的 `RunScheduler` 默认同时执行 4 个主 Run，由 `CODEAGENT_WEB_MAX_CONCURRENT_RUNS` 控制。不同会话可以同时执行；同一会话只能存在一个 queued/running Run，重复提交返回 409。超过执行槽位的请求进入队列，HTTP 202 只表示接收成功，不表示任务完成。

建议分三层验证：

1. 正确性：槽位上限、排队、取消、关闭、同会话冲突、数据隔离。
2. 离线基准：用固定延迟和事件量测 Web API、调度器、SQLite、SSE 的处理开销。
3. 真实业务：相同任务材料、模型、工具和上下文长度下，测完整 Agent 耗时、首字延迟、限流、Token 用量与任务成功率。

主 Run 的槽位限制不等于整个系统的模型调用并发上限，子 Agent/Team 还有各自调度。多个普通会话共享项目文件，测试模型写入任务时应给每个会话独立的测试项目目录。

## 已提供的离线入口

在 `D:\new_codeAgent` 下，用已经安装项目及 Web 依赖的 Python 运行。不需要启动已有 Web 服务，不加载真实模型配置，不调用模型、MCP 或 shell 工具。

```powershell
python -m unittest discover -s tests -p test_web_concurrency.py -v
python -m unittest discover -s tests -p test_concurrent_resources.py -v
python -m unittest discover -s tests -p test_web_concurrency_eval.py -v

# 默认 4 个执行槽位，并发客户端逐级增加。
python -m evals.web_concurrency --workers 4 --concurrency 1,4,8,16 --requests 32 --delay 0.2

# 保持 8 个并发客户端，比较执行槽位数。
python -m evals.web_concurrency --workers 1,4,8 --concurrency 8 --requests 32 --delay 0.2
```

`--requests` 是每种组合的任务总数；每个任务创建独立会话。客户端采用闭环负载：一个任务收到完整 SSE 结束标记后，才提交下一个。`--delay` 是模拟 Agent 累计等待秒数，`--events` 是每个任务发出的文本事件数，默认 10；数据库写入、调度和读取另有开销。`--timeout` 包括接收、排队和 SSE 完成，默认 60 秒。

每次运行创建独立的 `eval-results/web-concurrency-<UTC时间>-<随机ID>/`，其中保存总报告 `report.json`、每组 `result.json`、SQLite 和测试工作区。运行不覆盖历史报告，也不修改正在使用的服务配置或数据库。出现超时、输出隔离错误或突破槽位上限时退出码为 1。

脚本通过 `httpx.ASGITransport` 运行真实 FastAPI 路由和中间件，注入真实 `RunScheduler` 与独立 SQLite，只替换 Agent。每个任务连接 SSE，检查结束标记、事件顺序、Run/会话归属、文本内容、最终消息与 checkpoint。它不验证真实 Agent 的工具隔离和模型正确性。

## 指标与读法

| 指标 | 含义 |
| --- | --- |
| `peak_active_agents` | 模拟 Agent.run 同时执行的峰值，应不超过 workers |
| `successful_runs_per_second` | 通过结果校验的任务数 / 整组测量时间 |
| `submit` | POST Run 从发起到响应的时间，含失败 HTTP 响应 |
| `completion` | 成功任务从 POST 发起到完整 SSE 结束的时间，包含排队 |
| `admission_to_agent` | POST 发起到 Agent.run 开始，包含 API 接收、排队和初始化，不是纯排队时间 |
| `agent` | 模拟 Agent.run 的时间，包括等待和事件持久化 |
| `observed_all` | 所有任务到校验结束或失败的时间，包含超时样本 |
| `failure_rate` | HTTP、任务、超时或数据校验失败数 / 提交任务数 |
| `cpu_seconds` | 测量窗口内当前进程 CPU 时间，包含压测客户端开销 |

报告提供最近秩 P50/P95/P99 和逐任务原始样本。成功延迟不混入失败样本，必须同时看失败率与 `observed_all`。没有成功样本时延迟为 null。会话预创建和应用启动不计入测量，任务结束后的结果检查会影响下一次提交及整组吞吐量。

32 次只是快速冒烟，P99 几乎是最大值，不能作为稳定容量结论。正式比较应固定机器负载和参数，先跑一轮预热，再将请求数提高到数百以上并重复 3 次。可以增加 `--events` 观察频繁事件写入带来的影响；例如 `--events 100 --requests 200`。

固定 workers=4 时，从 4 增加到 8、16 个客户端，通常主要增加等待；只有实测吞吐量提升、延迟与错误仍满足目标时，增加执行槽位才有意义。SQLite 当前使用一个连接和 RLock，即使开启 WAL，共享连接的操作仍被锁串行化；事件频繁写入值得重点测量，但仅凭代码不能断言它是实际瓶颈。

ASGITransport 在进程内缓冲 SSE，因此本脚本不能测真实网络连接容量、网络首字延迟、浏览器渲染、代理断连或 TCP 背压。固定等待模拟的是 I/O 等待型任务，不代表 CPU 密集任务、真实模型或完整 Agent 的吞吐量。当前机器一次加载 Uvicorn 时遇到了 Anaconda `_ctypes` DLL 权限错误；真实 HTTP 测试前需要使用能正常导入 Uvicorn 的 Python 环境。

## 真实模型测试步骤

### 已提供的真实模型批量入口

```powershell
python -m evals.web_concurrency_live --tasks 32 --workers 32 --model-slots 32 --timeout 240
```

该命令会产生模型调用费用。它读取当前环境和已保存的模型设置，使用真实 `WebAgentFactory`、模型客户端、文件读取工具、Web API、调度器与 SQLite。默认同时提交 32 个独立会话，每个任务分析一份当前项目源码副本中的指定文件。任务变体覆盖并发准入、取消、预算、调度、配置、事件、循环保护与测试，并分别要求解释流程、边界条件、状态隔离或测试用例。

`--workers` 控制主 Run 执行槽位；`--model-slots` 设置本测试进程的 `CODEAGENT_MAX_CONCURRENT_MODELS`。产品默认模型调用并发上限为 8，因此仅把主 Run 槽位提高到 32，不能保证同时发出 32 个模型请求。真实重叠峰值根据持久化的开始/结束时间计算，报告分别给出 `peak_running_runs` 和 `peak_model_calls`，后者是客户端调用区间，不能证明供应商内部同时计算了多少请求。

源码副本有独立 Git 边界，以免父仓库对 `eval-results` 的忽略规则影响搜索。测试使用独立数据库和运行时目录，关闭 Skills、Memory、MCP 配置、向量检索、LSP、摘要和第三方追踪，以只读方式运行。保留真实 Agent 的工具调用与流式模型请求，每次模型输出上限默认 4096 Token，恢复重试上限为 1，每个任务累计 Token 预算 100000，批次截止时间默认 240 秒。取消为协作式，截止时间之后仍可能有清理等待；这是一个受控短任务 profile，不等同于生产默认配置或长任务能力。

结果目录为 `eval-results/web-live-<UTC时间>-<随机ID>/`。`manifest.json` 保存模型名、配置、Git revision 和题目源文件哈希；`report.json` 保存终态、调用峰值、成功任务 P50/P95/P99、首个文本事件时间、用量和逐任务结果；`task-*.json` 保存题目与回答；`state.db` 保存事件和调用记录。首个文本事件是服务端模型事件时间，不是浏览器首字延迟。读取持久化事件来统计运行过程，任务结束后补读 SSE 验证结束标记；不模拟 32 个浏览器持续订阅连接。

成功检查包括：完成终态、实际读取工具、模型返回用量、答案包含本任务标记及指定文件/符号、事件归属、checkpoint 和 SSE 结束。它是结果完整性检查，不是对代码分析语义的全面评分；单批 32 个任务只能证明该负载下的一次运行情况，不能证明长期稳定容量。保存的 Token 分项包含缓存命中，不按未核实单价估算金额。

### 扩展到完整服务压测

1. 使用独立数据目录启动测试服务，执行槽位分别设为 1、2、4、8，启动端口与现有服务分开。
2. 为每个并发任务新建会话；先用不调用工具的固定短问答，再用固定只读检索任务，最后使用独立目录中的修改任务。保持模型、上下文长度、任务材料一致。
3. 先 GET `/api/health` 获取会话 Cookie，再 POST `/api/conversations` 创建会话，POST `/api/conversations/{id}/runs` 提交。保持 Cookie，订阅 `/api/runs/{run_id}/events` 直到 `stream.end`，校验终态及业务结果；不能将 202 算作任务成功。
4. 逐级增加客户端数，记录提交、排队、首次有效输出、完成时间、模型 429/超时/重试、Token 用量及费用，同时监测服务进程 CPU/内存和队列长度。等待回答与审批会占槽，自动压测材料应避免进入这些等待。
5. 预先定义可接受的 P95 完成时间和失败率。选出满足要求的并发后，持续运行 30～60 分钟，检查队列是否增长、内存是否持续上涨，以及停止提交后能否恢复。

以下只是启动配置示例，会话提交后的真实模型调用会计费；离线脚本不会执行这些步骤。

```powershell
$env:CODEAGENT_DATA_DIR = 'D:\new_codeAgent\eval-results\live-concurrency-runtime'
$env:CODEAGENT_WEB_MAX_CONCURRENT_RUNS = '4'
python -m codeagent.web.cli --workspace 'D:\new_codeAgent' --port 8766
```

独立数据目录不继承原数据目录保存的模型设置，需要通过测试实例设置页或相应环境变量配置模型。容量结论应注明测试机器、代码版本、槽位数、客户端数、任务类型和模型服务，不把离线任务/秒写成真实 AI 服务 QPS。
