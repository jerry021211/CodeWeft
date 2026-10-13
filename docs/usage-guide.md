# CodeWeft 使用指南

[返回项目首页](../README.md) · [文档导航](README.md)

本文集中说明配置、工具、SDK 和运行行为。首次使用请先阅读 [快速开始](../README.md#快速开始)。除非另有说明，命令均在仓库根目录执行。

## 目录

- [当前结构](#当前结构)
- [Web 工作台](#web-工作台)
- [原生协作与先出方案](#原生协作与先出方案)
- [环境配置](#环境配置)
- [Python SDK](#python-sdk)
- [可选联网搜索](#可选联网搜索)
- [只读权限](#只读权限)
- [用户提问：ask_user](#用户提问ask_user)
- [规划能力：todo_write](#规划能力todo_write)
- [Task System：当前会话直接执行](#task-system当前会话直接执行)
- [子 Agent：subagent](#子-agentsubagent)
- [运行平台与命令 Shell](#运行平台与命令-shell)
- [运行时 System Prompt 组装](#运行时-system-prompt-组装)
- [错误恢复：Error Recovery](#错误恢复error-recovery)
- [按需能力：Skill Loading](#按需能力skill-loading)
- [长期记忆：Memory](#长期记忆memory)
- [上下文压缩与工具结果](#上下文压缩与工具结果)
- [接入外部 MCP 工具](#接入外部-mcp-工具)
- [Agent Team（历史兼容）](#agent-team)
- [运行测试](#运行测试)

## 当前结构

```text
codeagent/
  agent.py          # 核心 Agent 类和 loop
  config.py         # 从环境变量读取模型和运行配置
  anthropic_client.py  # Anthropic SDK 调用与 streaming
  providers.py        # OpenAI Chat Completions / Responses 协议适配
  multimodal.py       # 图片、PDF、文本和音频附件规范化
  speech.py           # 独立语音识别模型与输入处理
  models.py         # 模型响应结构
  messages.py       # message/tool_use 规范化
  tools/            # 工具定义与注册表
  permissions/      # 工具执行权限策略
  hooks/            # agent lifecycle hooks
  context/          # 上下文预算、压缩与 checkpoint 状态
  prompts/          # system prompt 动态组装
  skills/           # skill catalog 与按需加载
  memory/           # Markdown 长期记忆与模型选择
  tasks/            # 持久化 Task 领域模型
  multiagent/       # 原生委派、依赖计划执行、检查点、恢复与交付
  runtime/          # 取消、活动监测、运行数据目录与共享验证执行器
  legacy/teams/     # 旧 Team 的 Session、候选交付、调度与历史恢复兼容
  worktrees/        # 共享 Git 快照、路径范围与文件交付校验
  mcp/              # 外部 MCP Server 配置、连接与工具适配
  recovery/         # 分类、退避、fallback 与续写恢复
  events/           # 结构化运行事件与 Token 计量
  web/              # SQLite、FIFO 调度器与 FastAPI/SSE transport
```

说明：当前原生多 Agent 使用 `delegate_task`、`delegate_batch`、`execute_plan`，
由 `multiagent/coordinator.py` 调度原 Task。旧 Team 的 `TeamSupervisor` 在
`legacy/teams/supervisor.py`，保留旧会话的运行与恢复。新协作不依赖旧 Candidate 流程。
模块归属和兼容范围见 [多 Agent 目录整理](multiagent-layout.md)。

## Web 工作台

侧栏底部的 **设置 → 模型与能力** 可配置对话模型协议、服务地址与密钥，并独立配置语音识别和向量模型。
支持获取模型列表、保存后立即生效和重启恢复；首次启动 Web 也可以在页面完成模型配置。
详细用法见 [模型服务配置](model-services.md#在网页中配置)。

本机工作台面向单用户使用：左侧按项目管理会话，中间显示对话、
流式回复和 Agent 动作，右侧展示 Token、持久化任务、子 Agent、恢复记录、文件改动与
脱敏后的调试事件。消息、Run、审批、模型调用用量、事件流和 checkpoint 持久化在
CodeWeft 外部数据目录的 `state/state.db`。新建对话时可以从页面选择任意已有的本机项目
目录；每个对话永久绑定自己的工作区，后续执行使用该目录专属的 Agent 和工具状态。

安装并构建（Python 3.11+、Git，推荐 Node.js 22.12+；先按首页创建并激活虚拟环境）：

```powershell
python -m pip install -e ".[web]"
npm.cmd --prefix web ci
npm.cmd --prefix web run build
```

启动（只监听本机）：

```powershell
codeagent-web --workspace . --port 8765
```

然后访问 [http://127.0.0.1:8765](http://127.0.0.1:8765)，保存对话模型配置后，点击 **新建项目** 选择已有的本机目录。
点击输入框下方的 **Agent** 按钮打开执行方式菜单，选择 **Agent** 或 **只读**，也可在菜单内开启 **先出方案**。
macOS / Linux 将 `npm.cmd` 换成 `npm`。前端开发流程见 [Web 开发指南](../web/README.md)。

Web 运行时有以下边界：

- `--workspace` 是初始工作区，不是唯一可打开的项目，也不是运行时数据目录；数据位置由 `CODEAGENT_DATA_DIR` 或系统默认目录决定。
- 所有文件和搜索工具限制在当前对话绑定的工作区内，并防止符号链接逃逸。
- 根任务使用 FIFO 队列，默认最多 4 个会话并行；同一会话只能有一个排队或运行中的
  任务。`CODEAGENT_WEB_MAX_CONCURRENT_RUNS` 可设置正整数并发上限，设为 `1` 恢复
  串行执行。此上限不包含 Team 内部 worker；不同 Run 不共享可变的 Agent/Tool/CWD 状态。
- 等待回答、审批或模型重试仍占一个并发名额。多个会话可操作同一项目目录，但项目文件
  仍然共享，涉及重叠修改时应使用独立工作区/worktree。
- 工作区浏览 API 只列出本机目录名，不读取文件内容，并拒绝 UNC/网络路径。
- 危险操作通过页面审批；取消在模型调用、工具调用和退避等待之间的安全边界生效。
- SSE 事件带持久化序号，断线后可以继续回放；未完成 Run 在进程重启后标记为
  `interrupted`，不会盲目重放可能产生副作用的操作。
- Token 使用量来自 provider 返回的真实 usage，并按主模型、memory、context、
  子 Agent 等 `call_kind` 汇总；界面同时展示缓存读取量和缓存命中率，provider
  不返回 usage 时明确标记为不可用。
- 调试面板不会展示完整 system prompt 或隐藏推理内容，事件 payload 会截断和脱敏。

## 原生协作与先出方案

新建 Web 会话默认使用 **Agent**：你描述目标，由主 Agent 选择直接完成或按需委派成员，不需要开启旧 Team。
成员任务与右侧任务面板共用 Task 编号和依赖；对话中可查看成员执行链路、报告以及实际记录的验证结果。

| 执行入口 | 用途 | 项目文件 |
| --- | --- | --- |
| `delegate_task` | 一个边界明确的已有 Task | 成员在当前项目目录执行 |
| `delegate_batch` | 1–5 个已有 Task，按依赖和冲突安排并行 | 成员共享当前项目目录 |
| `execute_plan` | 1–32 个 Task 的持久依赖计划 | 有写任务时在受管理的协作目录执行，验证后回写；纯只读计划仍在当前项目 |

单任务和批量任务的文件修改不会因失败自动回滚；计划路径保留检查点、验证与交付记录，遇到冲突或未知结果时保留现场。
具体选择、验证与恢复方法见 [原生协作指南](collaboration.md)。历史普通会话会沿用检查点中的旧工具集合；需要当前委派入口时新建会话。

希望先审阅实施步骤时，在 **执行方式 → 先出方案** 中开启规划。Agent 调查项目并提交可预览的方案，
你可以要求修改、放弃，或 **批准并执行**。这与个人 `todo_write` 步骤清单、CLI 的 `--planning-mode` 配置是不同机制。
**只读** 限制工具副作用，批准方案不会解除只读权限；完整流程见 [先出方案](plan-mode.md)。

CLI 和基础 Python SDK 继续使用普通 Agent 的 `subagent`、Todo/Tasks 后端，不自动提供上述 Web 原生委派和方案审批流程。
旧 Team 的继续执行与恢复见本文末尾的 [历史兼容说明](#agent-team)。

## 环境配置

已支持按能力组合模型：对话模型负责 Agent、子 Agent、摘要和记忆；语音识别模型负责
音频转文字；现有 embedding 模型负责代码向量检索。三者可以使用不同服务商、地址和
密钥。Web 可添加附件，CLI 支持重复 `--attach PATH`。
配置、协议能力和完整示例见 [多接口与多模态模型组合](model-services.md)。

推理等级可通过 `.env` 的 `REASONING_EFFORT` 设置，也可以在输入框旁的“推理”下拉框
逐次选择。
CLI 也可使用 `codeagent --reasoning-effort max "分析这个项目"` 覆盖环境配置。
当前适配 DeepSeek 官方 Anthropic 兼容接口，优先读取 `/models` 的真实档位；
元数据不可用时，对已核实的 DeepSeek 模型使用官方定义的 `low`、`high`、`max`。
`none` 关闭思考，`default` 省略推理参数并使用官方默认（目前 DeepSeek 为 `high`）。
未知模型或未适配的供应商仅提供官方默认，显式传入不支持的档位会报错。

`POST /api/conversations/{conversation_id}/runs` 支持可选字段 `reasoningEffort`，例如：

```json
{"content": "分析并修复这个问题", "reasoningEffort": "max"}
```

省略或传 `null` 使用环境配置；显式传 `"default"` 覆盖环境配置、恢复官方默认。
选择会随消息和 Run metadata 持久化，并固定用于本次任务的模型调用、重试和子 Agent；
Team 成员继承启动任务的选择。摘要、记忆和搜索改写等辅助调用使用官方默认。
流式与非流式请求均发送官方 `thinking` / `output_config.effort` 字段。
推理等级与 `MAX_TOKENS` 独立；输出上限过低仍可能截断高强度推理的响应。

官方协议依据：[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)、
[模型元数据](https://api-docs.deepseek.com/api/list-models/)。

CLI、基础 SDK 和保留旧工具集合的 Web 会话支持同一 Run 内的读取工具与只读子 Agent 并行。默认每个 Run 最多
4 个并行读取、3 个子 Agent；另有进程级请求限额。写入、shell、MCP、`search_code`
和未声明并发合同的自定义工具仍串行。关闭 `CODEAGENT_PARALLEL_ENABLED` 可回退。

`search_code` 单独采用无时间上限策略：索引扫描、向量构建/查询和关键词改写不受工具期限、
模型响应期限或短 HTTP 超时限制，执行期间不累计本分支的活跃时间预算。手动取消、Token
预算和每次向量构建的块数限制仍有效；其他工具和主模型调用保留原有时限。
独立索引命令 `python -m codeagent.code_search index --workspace <目录>` 也默认不限时，
需要限制时可显式传入 `--timeout <秒数>`。修改后需重启 CodeWeft 后端生效，已有索引可继续复用。

旧的 `subagent(description)` 保持前台串行行为。独立调查可使用
`subagent(description, access="read_only")`，同轮多个调用可以同时执行；再传
`run_in_background=true` 可让主 Agent 继续独立读取。`subagent_result` 查询或有限等待，
`subagent_cancel` 停止单项；历史 Web 会话仍可查看对应的子 Agent 活动。

`access="read_only"` 的子 Agent 只有明确允许的读取和私有归档工具，不再委派。后台结果在安全边界通知主 Agent，
写入前和最终回答前收齐当前 Run 的后台任务。取消一个 child 不影响兄弟；进程重启后未完成
任务标记为 interrupted，不自动重放。多个会话或外部编辑器仍可能修改共享目录，这不是文件快照隔离。
配置和实现边界见 [普通 Agent 并行说明](parallel-execution.md)。

使用 CLI 或 SDK 时，可参考仓库的 `.env.example`，在**实际启动目录或其父目录**创建 `.env`，也可以在启动进程前导出环境变量。按你的模型服务填写：

```bash
MODEL_PROTOCOL=openai_chat
MODEL_ID=your-model-id
API_KEY=your-api-key
BASE_URL=https://your-provider.example/v1
STREAMING=true
CODEAGENT_PLANNING_MODE=auto
```

配置加载从当前工作目录向父目录查找，使用找到的第一个 `.env`；已导出的环境变量不会被该文件覆盖。
切换到其他项目再运行 `codeagent` 或 SDK 时，不会因为可编辑安装而自动读取 CodeWeft 安装仓库中的 `.env`。
Web、CLI 和 SDK 指向同一个 `CODEAGENT_DATA_DIR`（未设置时使用同一系统默认目录）时，可以共用 Web 已保存的模型设置；这些设置优先于对应环境变量。

主 Agent 和子 Agent 的每次逻辑执行固定最多 200 轮，第 185 轮调用模型前提醒收尾。暂停和恢复沿用已用轮数，不调用第 201 轮；第 200 轮已产生的工具调用仍完成结果保存。旧 `MAX_ITERATIONS` 配置不再覆盖该固定值。模型调用次数和工具调用次数仅统计，不设执行次数上限，旧 `CODEAGENT_RUN_MAX_MODEL_CALLS`、`CODEAGENT_RUN_MAX_TOOL_CALLS` 不再生效；活动时间预算、可选总 Token 预算、单次操作超时和循环检测继续生效。

一轮按主执行循环计数；摘要请求和同轮供应商重试计入模型调用统计，不额外增加轮数。收尾提醒作为运行时消息进入模型上下文，压缩后若提醒不在发送内容中会重新附加，再做完整请求预算校验。

## Python SDK

在 CodeWeft 仓库根目录安装核心包 `python -m pip install -e .`，再切换到目标项目目录运行下面的脚本。
若使用 `.env`，将它放在运行脚本时的当前目录或其父目录；安装仓库中的 `.env` 不会自动随项目切换生效，也可事先导出环境变量。
`EnvironmentConfig.from_env()` 按上文规则读取配置；指向同一运行时数据目录时可共用 Web 已保存的模型设置，且它们优先于对应环境变量。
此示例构造只读 Agent，调查当前目录并返回 `AgentResult`，会产生真实模型调用。

```python
from pathlib import Path

from codeagent import (
    Agent,
    EnvironmentConfig,
    PlanningBackend,
    TodoStore,
    create_default_hooks,
    create_default_registry,
)
from codeagent.tools.workspace import WorkspaceGuard

workspace = Path.cwd()
env = EnvironmentConfig.from_env()
todo_store = TodoStore()
agent = Agent(
    client=env.create_model_client(stream=False),
    tools=create_default_registry(
        workspace_guard=WorkspaceGuard(workspace),
        todo_store=todo_store,
    ),
    config=env.to_agent_config(planning_backend=PlanningBackend.TODO),
    hooks=create_default_hooks(workspace=workspace, todo_store=todo_store),
    read_only=True,
)
result = agent.run("梳理当前项目的入口、主要模块和测试方式，给出关键文件路径。")
print(result.final_text)
print(f"停止原因：{result.stop_reason}；执行轮数：{result.iterations}")
```

`result.messages` 保存本次 Agent 的消息历史，`result.usage` 提供用量汇总。`stop_reason` 是执行停止原因，不能单独证明代码或测试通过。
实例还可继续调用 `agent.run(...)`；普通 Agent 空闲时可用 `agent.set_read_only(False)` 允许按权限策略执行修改。
这个基础示例不创建 Web 会话、持久化协作任务或审批界面；检索、Skills、记忆、MCP 等能力需像 CLI 工厂一样按需装配。

### Hooks 与运行扩展

CLI 默认启用基础 hooks：

- `UserPromptSubmit`：记录工作目录
- `BeforeModelCall`：TODO 计划过久未更新时注入 reminder
- `PreToolUse`：权限检查和工具调用日志
- `PostToolUse`：大输出提醒
- `Stop`：工具调用次数统计

Hook 管理器区分两种调用方式：`observe(event, handler)` 注册观察者，由
`notify()` 逐个通知，返回值不改变流程；`register(event, handler)` 注册控制处理器，
由 `decide()` 按顺序执行，首个非 `None` 结果停止后续判断。旧 `trigger()` 保留为
`decide()` 的兼容入口，不会隐式通知观察者。观察者发生普通异常时，其余观察者仍会
执行，随后异常向调用方报告；允许降级的组件负责记录自己的异常，不能静默丢失证据。

Agent 在原有生命周期事件处先通知观察者，再运行原有控制链；只读/规划模式跳过
`BeforeModelCall` 控制链的既有规则保持不变。`PostToolUse` 仍表示原有的工具完成边界，
不覆盖每次重试或所有未执行的调用，完整事实记录应使用执行采集和批次结束边界。

CVM 阶段 1、2 的接入集中在 `codeagent/cvm/integration.py`：

| 观察事件 | 职责 |
| --- | --- |
| `UserInputAccepted` | 接收真实输入及其来源，更新任务记录 |
| `RunInputReady` | 将当前目标投影到上下文；无新输入的恢复也会执行 |
| `CollectRuntimeReminders` | 提交现有任务提醒，由 Agent 统一追加及压缩后补回 |
| `ToolBatchFinished` | 在父线程按请求、尝试顺序入账，早于结果的上下文投影 |
| `RunFinished` | 普通 `run()` 结束或异常退出时核对证据有效性 |

`CvmToolRecorder` 在每次实际工具尝试的前后采集独立事实包，保留重试和取消结果；
并行工作线程不直接通知 Hook 或修改父账本。每个 Agent 安装自己的 CVM 观察者，复制
Hook 时排除绑定父 Agent 的观察者。配置、检查点格式和提示内容保持阶段 1、2 的行为，
没有增加自动补验证或完成拦截；Team 的 CVM 仍保持关闭。

普通 Agent（含只读权限）和普通子 Agent 默认启用防循环与任务预算，Team 不在本次范围内。
保护状态独立于上下文压缩；Web checkpoint 保存状态，循环或预算停止会标记为失败，
CLI 单次执行返回非零退出码。规则、可调阈值、恢复语义和输入识别限制见
[防循环与执行预算](loop-guard.md)。

## 可选联网搜索

内置 `web_search` 工具使用 [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)，
返回标题、来源 URL 和摘要供 Agent 引用。无需安装额外搜索 SDK。
在启动目录的 `.env` 中配置后重启后端：

```dotenv
TAVILY_API_KEY=你的_Tavily_API_Key
CODEAGENT_WEB_SEARCH_ENABLED=false
CODEAGENT_WEB_SEARCH_TIMEOUT=20
```

- **Web**：输入框下方的「联网搜索：关 / 开」控制下一次请求。默认关闭；发送后保存选择，
  重新打开会话时恢复，运行中不能修改。未配置 Key 时按钮禁用并提示配置方法。
- **CLI**：`python -m codeagent --web-search "搜索 Python 最新发布信息"` 开启，
  `python -m codeagent --no-web-search "分析当前项目"` 关闭。省略参数时使用环境默认值。
- **API**：`POST /api/conversations/{id}/runs` 支持 `{"content":"搜索资料","webSearch":true}`；
  `false` 强制关闭，省略或 `null` 使用环境默认值。缺少 Key 时开启请求返回 422。
- **SDK**：`create_default_registry(web_search_config=env.web_search_config)`；也可从
  `codeagent.tools.web_search` 导入 `WebSearchConfig` 自行配置。

普通会话（含只读权限）均支持，普通子 Agent 继承父请求的选择；Team 会话暂不支持。
关闭时不注册搜索工具，也不会调用 Tavily。这个开关只控制内置搜索工具，并非整个进程的网络隔离：
模型 API、既有 shell/MCP 工具仍按原配置工作。搜索词会发送给 Tavily，Key 仅保存在服务端配置中。
每次搜索默认 10 条、最多 10 条，正文最多 48,000 字符，使用 basic 深度；网络错误、限流或额度不足会明确报告，
不自动重试收费请求。搜索结果作为外部参考资料处理。

## 只读权限

助手统一按用户请求回答或执行，不再区分 Code / Discuss 交互模式。提问、讨论、创作和
角色扮演默认在对话中交付；需要文件或代码产物时才操作工作区。

只读保护是独立的工具权限，不改变助手角色或任务：

- **Web**：在输入框下的“执行方式”菜单选择“只读”。权限随每次请求保存，重新打开会话时恢复最近一次选择；
  运行或排队期间不能修改当前请求的权限。
- **CLI**：`python -m codeagent --read-only "解释这个项目的架构"`；交互会话使用
  `/read-only on`、`/read-only off`，只读时提示符显示 `[read-only] >`。
- **SDK**：`Agent(..., read_only=True)`；普通 Agent 空闲时可调用 `agent.set_read_only(True/False)`。
- **API**：`POST /api/conversations/{id}/runs` 支持
  `{"content":"解释架构","readOnly":true}`，省略时为 `false`。

开启后，`PreToolUse` 在审批和执行前阻止文件/记忆写入、任务更新、委派、未知及外部 MCP
工具；自动记忆维护暂停，Team 不能启动或接管。读取、搜索、提问和上下文压缩仍可用。
Shell 仅允许单个已知只读命令及受支持的选项，不允许管道、重定向和脚本。
`git diff/log/show` 需要 `--no-ext-diff --no-textconv`。

正常新会话不注入模式通知。只读权限启用、改变或压缩丢失提醒时，追加简短的当前权限说明；
系统提示词和工具定义不因权限切换重建。历史消息保持完整，旧模式提醒被当前权限说明取代。
旧 API 请求 `mode: "discuss"` 兼容映射为只读；与 `readOnly: false` 冲突时拒绝请求。
旧消息的 `metadata.mode` 仅用于恢复权限，后续请求使用 `read_only` metadata。
SDK 的 `PromptMode.DISCUSS` 和旧 `/discuss`、`--discuss` 入口已移除，请使用上述权限接口。

这是 Agent 工具执行策略，不是操作系统沙箱。会话、事件和检查点仍会保存到运行时数据目录，
权限变更不影响其他会话或已启动的进程。旧模式的研究与实现背景见
[历史设计记录](discuss-mode.md)，该文不再代表当前接口。

## 用户提问：ask_user

CLI 和 Web 的主 Agent 可在需求、偏好或关键决策不明确时调用：

```json
{"question": "导出文件需要哪种格式？", "options": ["CSV", "JSON"]}
```

`options` 可省略，用户始终可以自由回答。调用会阻塞当前 Agent，直到收到非空回答；
等待没有自动超时，不会替用户选择答案或继续模型循环。当前 Run 占用一个并发名额，
其他名额可以继续执行其他会话；所有名额都在等待时，新任务排队。HTTP 服务仍可处理
回答和取消请求。多会话调度设计与验证见 [多会话并发说明](concurrent-sessions.md)。

- CLI 在终端显示问题，接受选项编号或自由文本；空输入继续等待，Ctrl+C 中止。
- Web 在聊天输入区上方显示问题和回答框，点击选项后仍需提交；可用“停止”取消等待。
  问题和回答保存在 SQLite，刷新页面可恢复；服务重启会中断 Run 并取消未回答问题。
- 只读权限下也可提问；Team 和独立子 Agent 工具池不新增交互入口。
- 回答作为本次 `ask_user` 的工具结果交回模型，随后继续同一个 Run。

SDK 通过 `create_default_registry(ask_user_fn=handler)` 注入同步回调，签名为
`handler(question: str, options: list[str]) -> str`；未注入时不暴露提问工具。
终端处理器可从 `codeagent.tools` 导入 `terminal_ask_user`。

Web 接口：`GET /api/runs/{run_id}/questions` 查看问题，
`POST /api/runs/{run_id}/questions/{question_id}/answer` 提交 `{"answer":"JSON"}`。
空回答返回 422；重复提交相同回答幂等，冲突回答或已取消问题返回 409。

## 规划能力：todo_write

CLI 的规划后端由 `CODEAGENT_PLANNING_MODE=auto|tasks|todo` 控制。`auto` 下交互式与单次 CLI 都使用个人 `todo_write`；显式选择 `tasks` 或在 auto 下传入 `--task-list` 可继续使用持久化 Task System。CLI、SDK 默认 TodoStore 在当前进程内维护状态。

这里的“规划后端”只决定步骤和任务如何记录，不会启用 Web 的“先出方案”或等待方案批准。

Web 新普通会话使用个人 `todo_write`，每次成功写入会把完整清单与 `todo.updated` 事件原子保存到 SQLite。刷新页面、后续轮次和进程重启后可恢复已保存清单；恢复清单不代表自动重跑中断任务。`GET /api/conversations/{id}/todos` 返回各 Agent 独立的清单，输入框上方展示主会话进度，任务面板还可查看成员步骤。

停止或中断执行后保留清单，界面停止转圈并显示运行已停止；未完成步骤不会自动标成完成。再次执行时，第一次模型请求即提供最新清单，要求对照用户新消息核实已有结果、更新计划后继续；改换目标时调整或清空旧清单。新一轮尚未更新旧清单时显示“等待核对上次计划”。仅询问进度不应自行恢复旧任务。

新 Web 会话的主 Agent 同时保留个人 todo 和原生协作的 Task 工具：todo 记录当前 Agent 的步骤，Task 记录成员分工、依赖与验证要求。原生成员不另建 todo 或任务列表，任务执行状态由协作执行器维护；完成个人步骤不等于成员任务已通过验证。
原生 Task 工具由 Web 适配器在 Agent 创建后安装；基础 SDK 构造器仍拒绝同时传入 `todo_write` 和 `TaskCreate` / `TaskUpdate`，自定义工具池应明确选择规划后端。

已有独立 Tasks 的历史会话保留原任务工具与依赖校验，避免隐藏旧依赖后无法继续执行。历史 Team 规划阶段使用 Tasks 与 TeamPlanSubmit；执行阶段 Lead/成员保留任务查询和角色工具，并可使用以 AgentSession 为归属的个人 todo。旧 Team 的个人步骤完成仍不能代替团队结果提交。

个人清单不读文件、不运行命令、不写项目文件。每项只有 `content`、`status`，按数组顺序展示，每次写入完整替换。每个 Agent 的清单独立；Team 成员的个人清单不携带负责人和依赖图。

TODO 项只有三个状态：

- `pending`：还没开始
- `in_progress`：正在做，最多只能有一个
- `completed`：已经完成

当默认工具池里存在 `todo_write` 时，`Agent` 会自动在 system prompt 后追加
规划规则：有多个需要持续跟踪的阶段时使用 `todo_write`，每项描述可验证的阶段结果；不按工具调用次数机械拆分，不为小修改强制建清单。实际完成后即时更新，最终回复前核对状态。

默认 hooks 还会注册 reminder：如果模型连续 3 轮没有更新 TODO，就会在下一次
模型调用前注入一条 `<reminder>...</reminder>` 消息，提醒它更新计划或确认
下一步。这个机制只影响对话上下文，不会替 Agent 执行任何实际动作。

CLI 会在 `todo_write` 更新计划时打印用户可见的任务表：

- 第一次创建计划时打印 `[todo created]` 和完整任务表。
- 任务状态或内容发生变化时打印 `[todo updated]`、变化项和当前任务表。
- 所有任务完成时打印 `[todo completed]`。
- Agent 停止时如果仍有未完成任务，打印一次 `[todo final]`。
- 如果模型提交的 TODO 和当前任务表完全相同，不重复打印。

任务表使用固定状态标记：

```text
[ ] pending
[>] in_progress
[x] completed
```

如果一个进程里创建多个 Agent 或 subagent，应为每个 Agent 创建独立的
`TodoStore`，并把同一个 store 同时传给 `create_default_registry()` 和
`create_default_hooks()`。这样每个 Agent 的 TODO 计划互不污染。

## Task System：当前会话直接执行

当前 Web 原生协作使用同一套 Task API，由 `delegate_task`、`delegate_batch`、`execute_plan` 执行已有 Task。
执行中的状态由运行时维护，主 Agent 不能提前把未通过检查的任务标成完成。详见 [原生协作指南](collaboration.md)。
下述“当前会话直接执行”适用于显式启用 Tasks 的 CLI 和保留旧工具集合的历史会话。

显式启用 Tasks 的 CLI 与已有独立 Tasks 的 Web 会话注册 `TaskCreate`、`TaskGet`、`TaskList`、`TaskUpdate`。Task 持久化在
CodeWeft 外部数据目录的 `state/state.db`，支持 TaskList、依赖、owner、原子认领和 Activity。
任务业务对象保持九字段：`id`、`subject`、`description`、`activeForm`、`owner`、
`status`、`blocks`、`blockedBy`、`metadata`；TaskList、revision 和时间戳位于
独立持久化外壳中。

`TaskList` 只返回 `id`、`subject`、`status`、`owner`、`blocks`、`blockedBy` 六个摘要字段，
用于浏览和选择任务；执行选中的任务前，用 `TaskGet(taskId)` 获取完整描述、验收条件和
metadata。`TaskGet` 返回完整九字段，`TaskList` 不携带描述、进度文案或 metadata。

Task 默认在当前 Conversation 中直接执行。开始 Ready 任务时用 `TaskUpdate` 设置
`in_progress`，完成代码和验证后设置 `completed`，不会为每个任务创建独立对话。
Web 历史 Tasks 会话的右侧“任务”页可查看 Ready、Blocked、进行中和已完成任务，也可以创建任务，
或把指定任务作为普通 Run 继续交给当前会话。

## 子 Agent：subagent

本节介绍 CLI、基础 SDK 和历史普通 Web 会话的委派工具。新 Web 会话使用 [原生协作](#原生协作与先出方案)，不同时提供两套委派入口。

`subagent(description)` 默认继承工具权限并前台串行执行；只有任务明确授权时才应修改文件。
`subagent(description, access="read_only")` 用于可并行的只读调查，支持 `run_in_background=true`。
这与给整个会话开启 `--read-only` 不同：会话只读权限会阻止委派。

`subagent` 是一个委派工具，由 `SubagentTool(spawn_fn=...)` 实现。工具层只保存 schema
和被注入的 `spawn_fn`，不 import `Agent`，因此不会形成循环依赖。`Agent` 默认
会给自身注入 `SubagentTool(spawn_fn=self._spawn_subagent)`。模型调用 `subagent` 时，父
Agent 会创建一个新的子 Agent：

- 子 Agent 使用全新的 `messages` 列表，只包含父 Agent 传入的子任务描述。
- 默认继承权限的子 Agent 跑自己的 agent loop，可承担独立且边界清晰的调查、实现、修复、重构或验证，
  并继续调用读文件、搜索、bash、写入、编辑、`todo_write` 等工具。
- 子 Agent 的工具表会移除 `subagent`，避免递归生成子 Agent。
- 父 Agent 的上下文只收到子 Agent 的最终文本结论，不接收其中间消息和工具历史。
- 子任务描述只有明确要求修改代码时，子 Agent 才会编辑文件。
- 父 Agent 负责持久化 Task 状态、最终集成和验证，不把整个模糊目标交给子 Agent。
- 子 Agent 内部使用流式模型请求；失败会作为失败事件和 `Error:` 工具结果返回，
  父 Agent 不应原样重复提交同一个失败任务。

CLI 默认会给子 Agent 创建独立的 `TodoStore`、默认工具池和默认 hooks；权限检查
仍通过 hooks 执行，因此子 Agent 不会绕过权限策略。代码中如需自定义子 Agent
环境，可在构造 `Agent` 时传入 `subagent_environment_factory`，固定返回子 Agent 的
`ToolRegistry`、`HookManager` 和 `ContextManager`。

CLI 会在进入和退出子 Agent 时输出显式标志：

```text
[subagent enter] ...
[subagent exit] returned to parent agent
```

可以用下面的命令测试一次子 Agent 调用：

```bash
python -m codeagent --no-stream "请必须调用 subagent 工具，让子 Agent 读取 README.md 并总结这个项目的用途；拿到子 Agent 结果后，再用一句话告诉我结论。"
```

## 运行平台与命令 Shell

进程首次创建 Agent 或命令工具时会检测宿主操作系统，并缓存检测结果：

- Windows 优先使用 PowerShell（`pwsh` 或 Windows PowerShell），不可用时回退到
  `cmd.exe`。
- Linux 和 macOS 优先使用 Bash，不可用时回退到 POSIX `sh`。
- 命令工具会显式调用检测到的 shell，不再依赖 `subprocess` 的隐式平台默认值。
- 当前操作系统、实际 shell 和对应命令风格会作为运行时提醒发送给模型，避免在
  Windows 生成 POSIX-only 命令，或在 Linux/macOS 生成 PowerShell、cmd 命令。

为了兼容现有工具协议，工具名仍为 `bash`，但其描述会标明当前实际使用的 shell。

## 运行时 System Prompt 组装

Agent 不再在 `agent.py` 里硬编码 todo、subagent、skill、memory 等 prompt 文案。
每次模型调用前会通过 `PromptRuntime` 运行时组装 system prompt：

```text
Agent 收集真实运行状态
-> PromptRuntime 按固定顺序选择当前能力需要的模板
-> 按 static/dynamic 分区和 budget 组装
-> 返回 system prompt + trace/hash
```

System prompt 的顺序是：

- `static`：稳定身份和执行规则。
- `dynamic`：工具、todo、subagent、skill、memory 指引等能力信息。
- system 尾层：当前日期、工作区、操作系统和 shell 等运行时事实。

这些 system 内容保持稳定顺序；工具循环只在历史消息尾部追加 assistant/tool result，
不再临时插入并删除 `<system-reminder>` 用户消息。LLM memory 每个外部用户回合只选择
一次，选中内容随该回合用户消息持久化。DeepSeek 的上下文缓存自动生效，不发送会被
忽略的 `cache_control`；命中率按 provider 返回的缓存读取 token / prompt 输入 token
计算。

内置模板在：

```text
codeagent/prompts/templates/
```

普通 Agent 的身份提示只维护在 `templates/identity.md`；子 Agent 使用
`templates/subagent.md`。不再通过 `SYSTEM_PROMPT` 环境变量重复配置身份提示。

项目只能通过一个追加文件提供项目级说明，不能覆盖 Root、Lead 或 Teammate 的
核心身份和安全规则：

```text
.prompts/project.md
```

`PROMPT_TEMPLATE_DIR` 是部署者显式配置的完整模板目录，只应指向受信任位置。

可配置项：

```bash
PROMPT_TEMPLATE_DIR=.prompts
SYSTEM_PROMPT_BUDGET_CHARS=120000
SYSTEM_PROMPT_STATIC_BUDGET_CHARS=50000
SYSTEM_PROMPT_DYNAMIC_BUDGET_CHARS=70000
SKILL_CATALOG_BUDGET_CHARS=12000
PROMPT_TRACE=false
```

打开 `PROMPT_TRACE=true` 后，CLI 会打印每次组装的 prompt hash、字符数和包含的
fragment，方便调试和复现。

## 错误恢复：Error Recovery

Agent 使用独立的 `RecoveryRuntime` 保护模型调用。它不是简单 `try/except`，
而是把异常或特殊 `stop_reason` 分类成 `RecoveryReason`，再根据当前
`RecoveryState` 做恢复决策。

覆盖的主要路径：

- `429 rate limit`：指数退避 + jitter 后重试，尊重 `Retry-After`。
- `529 overloaded`：指数退避；连续多次 overloaded 后可切换 `FALLBACK_MODEL_ID`。
- `timeout/network/5xx`：有限重试。
- `prompt too long/context length/413`：触发 `ContextManager.reactive_compact()` 后重试。
- `max_tokens`：第一次提升输出 token 上限；仍截断时追加 continuation prompt 续写。
- `401/403/invalid request/invalid model/schema error`：不可恢复，快速失败。

主模型调用使用完整 recovery；memory selection side-query 使用轻量 recovery，失败时返回空
memory context，不影响主任务。工具执行错误不进入 recovery，而是作为 `tool_result`
返回给模型自我修正。

常用配置：

```bash
RECOVERY_ENABLED=true
FALLBACK_MODEL_ID=
RECOVERY_TRACE=false
```

高级配置：

```bash
RECOVERY_MAX_RETRIES=10
RECOVERY_BASE_DELAY_MS=500
RECOVERY_MAX_DELAY_MS=32000
RECOVERY_JITTER_RATIO=0.25
RECOVERY_MAX_CONTINUATIONS=3
RECOVERY_ESCALATED_MAX_TOKENS=64000
RECOVERY_OVERLOAD_FALLBACK_AFTER=3
RECOVERY_SIDE_QUERY_MAX_RETRIES=2
```

## 按需能力：Skill Loading

默认启用两级 Skill Loading：

- 所有项目、工作区及 Team Worktree 共用同一份全局技能库，不扫描项目内的 `.skills`。
- 启动时扫描 `SKILLS_DIR` 指定的目录，默认是 `CODEAGENT_DATA_DIR/skills`。
- 相对路径统一相对于 CodeWeft 数据目录解析；也支持指定全局技能库的绝对路径。
- 每个 skill 放在独立目录中，并提供 `SKILL.md`。
- Agent 的 system prompt 只注入 skill catalog：名称、描述和适用场景。
- 完整 `SKILL.md` 不会常驻 system prompt；模型需要时调用 `load_skill(name)` 按需加载。
- `load_skill` 只能按已注册 skill 名称加载，不能传任意路径。

示例目录：

```text
<CODEAGENT_DATA_DIR>/skills/
  code-review/
    SKILL.md
  python-refactor/
    SKILL.md
  agent-harness/
    SKILL.md
```

`SKILL.md` 使用简单 frontmatter：

```markdown
---
name: python-refactor
description: Refactor Python code with type hints, docstrings, compatibility, and focused tests.
when_to_use: Use for Python refactors, type hints, docstrings, main guards, API cleanup, or behavior-preserving edits.
---

# Python Refactor Skill

...
```

全局技能库可存放以下 skill：

- `code-review`：代码审查、风险、测试缺口。
- `python-refactor`：Python 重构、类型标注、docstring、main guard。
- `agent-harness`：修改或解释本项目的 agent loop、tools、hooks、todo、subagent、skill loading。

可通过环境变量关闭或改目录：

```bash
ENABLE_SKILLS=true
SKILLS_DIR=skills
```

Windows 默认技能目录为 `%LOCALAPPDATA%\CodeAgent\data\skills`。
CLI 和 Web 使用相同的加载规则，切换项目不会切换技能目录。
升级旧配置时，将 `SKILLS_DIR=.skills` 改为 `SKILLS_DIR=skills`，并把原有技能目录
复制到全局技能库；程序不会自动导入新打开项目中的技能。

## 长期记忆：Memory

默认启用长期记忆，每个新用户回合执行一次选择，工具循环中不重复执行：

1. 以本轮明确请求构建查询；长请求保留头尾，简短续问可补充上一条用户意图。
2. 从完整 Markdown 记忆库的 SQLite FTS5 索引中按 BM25 召回候选，默认最多 50 条。
   中文使用相邻双字词项，代码符号保留完整形式及拆分词项。
3. 选择模型读取标题、描述及命中的正文片段，返回最多 5 个文件名，允许选空。
4. 校验候选白名单，重新读取 Markdown；选择期间发生变化或消失的记录跳过。
5. 按默认 60,000 字符预算注入完整记录，与当前用户请求一起进入历史。
   超预算的整条跳过，继续尝试后续记录。候选为空时不调用选择模型。

Markdown 是权威来源，`.retrieval-v1.sqlite3` 是可重建的派生索引。每次查询检查文件清单和
元数据，通常只重新读取变化文件；默认每 60 秒进行一次按需全文核对（不是后台定时器）。
索引不可用时降级到内存检索；普通会话只读权限、Team 只读期间及手动搜索均不写索引。
`memory.selection.completed` 事件分别记录候选、选中、注入和跳过原因，不包含完整查询或记忆正文。
具体行为、限制和离线数据见 [长期记忆改造说明](memory-retrieval.md)。

你的项目使用 `deepseek-v4-pro` 时，这个 side-query 也会走同一个 Anthropic-compatible
客户端和同一个 `MODEL_ID`，不会硬编码 Sonnet。

内置三个 memory 工具：

- `search_memory(query)`：按关键词搜索记忆摘要。
- `load_memory(name)`：按精确名称加载完整记忆。
- `remember(name, type, description, content)`：保存稳定、可复用的长期记忆。

记忆按项目保存在 `CODEAGENT_DATA_DIR/workspaces/<workspace-id>/memory/`，不会写入
用户 Git 工作区或 Team Worktree。每条记忆是一个 markdown 文件；只有存在记忆记录时
才生成 `MEMORY.md` 索引。

推荐记忆内容：

- 用户长期偏好，例如“回答时先给结论，再给关键理由”。
- 项目约定，例如“子 Agent 默认不能再委托子 Agent”。
- 重要决策，例如“memory 使用 markdown store，暂不引入向量库”。
- 可复用参考，例如“某类任务应优先加载某个 skill”。

不推荐保存：

- API key、token、密码等秘密。
- 当前任务的临时状态。
- 大段工具输出或大段代码。

普通单 Agent 默认拥有读写 memory 的工具；同步子 Agent 默认只读，确实需要时可通过
`MEMORY_ALLOW_SUBAGENT_WRITE=true` 开放。任一非终态 Agent Team 存在期间，同一项目的
Root、Lead 和 Teammate 全部只读；Team 进入终态后 Root 自动恢复写权限。Team 执行结果
不会在结束后被自动回填进 Memory。

可配置项：

```bash
CODEAGENT_DATA_DIR=                 # 留空时使用系统用户数据目录
ENABLE_MEMORY=true
MEMORY_DIR=.memory                 # 旧工作区 Memory 的一次性只读导入位置
MEMORY_MAX_ITEMS_IN_PROMPT=50
MEMORY_MAX_LOADED_ITEMS=5
MEMORY_SESSION_BUDGET_CHARS=60000
MEMORY_MAX_MEMORY_BYTES=50000
MEMORY_SELECTION_MODE=llm
MEMORY_RETRIEVAL_MODE=indexed       # indexed | legacy；legacy 保留旧排序候选作对照
MEMORY_INDEX_VERIFY_SECONDS=60     # 全文核对间隔；0 表示每次核对
MEMORY_AUTO_EXTRACT=false
MEMORY_EXTRACT_RECENT_MESSAGES=12
MEMORY_CONSOLIDATE_THRESHOLD=30
MEMORY_CONSOLIDATE_MODE=simple   # simple | model
MEMORY_ALLOW_SUBAGENT_WRITE=false
```

`MEMORY_AUTO_EXTRACT=true` 时，Agent 会在每轮结束后让模型从最近对话中抽取稳定记忆。
默认关闭，是为了避免把临时对话误写成长期状态。`MEMORY_CONSOLIDATE_MODE=model`
会在记忆数量超过阈值后让模型合并重复记忆；默认 `simple` 不调用合并模型。
`MEMORY.md` 人工目录在保存记忆时更新，回合结束不再为它重复扫描所有正文；检索索引按查询刷新。

可以用下面的 query 测试手动记忆：

```bash
python -m codeagent --no-stream "请记住：这个项目里解释代码时先讲调用链，再讲关键函数。保存成长期记忆，然后告诉我保存的 memory 名称。"
```

也可以测试按需读取：

```bash
python -m codeagent --no-stream "按照我之前记录过的项目讲解偏好，解释 codeagent/agent.py 的主循环。"
```

## 上下文压缩与工具结果

工具正文、元数据、归档和批次额度统一在 `codeagent/tools/output_limits.py` 定义。旧字符额度配置不再覆盖工具页面；不再提供环境变量、CLI 或设置页调节这些额度。完整额度与分页示例见 [工具输出策略](tool-output-policy.md)。

每条元数据最多 2,000 字符；每个模型响应对应的工具结果批次软上限为 300,000 字符。先保留完整技能正文和必要元数据，再为错误、显式读取及其他结果分配正文。缩页调用各工具自己的分页器，保留真实状态及恢复游标，不重跑工具。生成输出每次最多保存 32 MiB；超过后仍排空命令管道，明确标记原始输出不完整。

请求输入预算为 `I = W - O - S`：W 沿用现有窗口发现与配置，O 为实际 `max_tokens`，S 沿用各调用路径的既有余量（无余量配置时为 0）。估算输入达到 I 的 85% 时记录预警，90% 时主动整理，目标不高于 70%；整理未达目标但不超 I 时仍可发送。未知窗口不猜数值。自动整理关闭时仍检查硬预算。

摘要最终最多 16,000 字符，每次摘要调用固定预留 32,768 tokens。一次整理共用 180 秒截止时间，最多 16 个原始材料块，每块最多 32,000 估算 tokens。允许连续拆分大字段，不再使用有损首尾摘录；块与合并请求均检查摘要模型窗口。超长最终摘要仅重写一次，失败不截断提交。

候选仍遵守最近 12 条消息、最近 2 轮、最少 4 条消息、最多 200 条且 12 轮的合法边界。提交必须满足字符节省至少 `max(256, 5%)` 且输入 token 估算下降；原文进入不可变历史分段，检查点只在验证成功后更新。失败冷却保持 90 秒，请求前最多 3 次整理，供应商超窗恢复最多 1 次。

窗口、权限、执行超时、模型服务凭据和原有传输限制仍使用原项目机制。旧的 80k/12k/200k 工具额度、摘要 4k/8192/45 秒等配置键不再生效，不自动改写用户配置。

```bash
CONTEXT_COMPACT_MODE=model
SUMMARIZATION_MODEL_ID=your-summary-model
CONTEXT_TRANSCRIPT_DIR=.transcripts
CONTEXT_TOOL_OUTPUT_DIR=.task_outputs/tool-results
CONTEXT_WINDOW_TOKENS=0
CONTEXT_MODEL_WINDOWS_JSON={}
CONTEXT_SUMMARY_WINDOW_TOKENS=0
CONTEXT_READ_REFERENCE_ENABLED=true
```

权限策略参考 `s03_permission` 的三道闸门：

- 硬拒绝：`sudo`、`rm -rf /`、`shutdown` 等直接拒绝
- 需确认：`rm `、写入 `/etc/`、`chmod 777`、写工作区外文件
- 默认允许：普通读文件、搜索、工作区内写入和非危险命令

## 接入外部 MCP 工具

项目现在是一个轻量 MCP Host：现有 `bash`、文件读写等工具保持不变，外部 MCP
Server 提供的工具会额外注册到 Agent。首版只接入 MCP Tools，不处理 Resources、
Prompts 和 Sampling，保持边界简单。

1. 复制示例配置：

```powershell
Copy-Item mcp.json.example mcp.json
```

2. 把 `command` 和 `args` 改成你的 MCP Server 启动命令。配置格式与 Claude Code
常用的 `mcpServers` 格式一致：

```json
{
  "mcpServers": {
    "my-plugin": {
      "command": "python",
      "args": ["path/to/mcp_server.py"],
      "env": {
        "PLUGIN_API_KEY": "${PLUGIN_API_KEY}"
      }
    }
  }
}
```

远程 Streamable HTTP 服务也可以直接配置：

```json
{
  "mcpServers": {
    "remote-plugin": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {
        "Authorization": "Bearer ${PLUGIN_TOKEN}"
      }
    }
  }
}
```

3. 启动 CLI 或 Web，核对并信任服务配置。CLI 在交互终端中会先展示命令、参数、
工作目录、地址与凭据变量名，确认后才启动进程或建立远程连接。非交互 CLI 不会自动
授权未知服务；请先在交互终端或 Web 设置中批准。拒绝或尚未批准的服务会跳过，内置
工具和其他已信任服务仍可使用。外部工具名称会显示为
`mcp__服务名__工具名`，例如 `mcp__github__search_repositories`。每次执行外部 MCP
工具都会走现有的用户审批流程；没有 `mcp.json` 时 MCP 自动关闭，不影响任何内置功能。

Web 工作台的「设置 → MCP 插件」以服务列表展示连接状态、工具数量和错误提示。
点击「添加服务」可套用模板或填写本地命令、远程地址；凭据与超时放在高级设置中。
保存不会授权或启动服务。展开「查看配置」，核对信息后点击「信任此配置」，再点击
「连接」；也可让下一次任务自动连接已信任服务。失败时可单独重试。
已有服务可在详情里「编辑配置」；凭据字段留空会保留原值，明确勾选清除才会删除。
编辑提交也会检查配置版本，避免覆盖另一处刚修改的内容。

信任记录保存在应用运行目录的 `state/mcp-trust.db`，按项目和完整配置指纹绑定，
只保存哈希，不保存凭据原文。命令、参数、地址、凭据值或超时等配置变化需要重新确认；
配置中的 `trusted` / `approved` 字段没有授权作用。信任启动不等于批准工具调用，
现有调用审批与只读保护仍生效。只读模式下，已经明确获信任的服务仍可启动；这是工具
调用保护，不是对子进程的操作系统沙箱。

各服务独立初始化和清理，一个服务失效或超时不会撤销其他健康服务的工具。可在服务
配置里设置 `startup_timeout`（默认 15 秒，上限 300 秒）和 `call_timeout`（默认
60 秒，上限 3600 秒）；调用采用服务时限与本次执行剩余预算中的较小值。取消或超时
会取消本地 SDK 请求，不自动重放调用；远端可能已经执行操作，结果按未知处理，不能
把停止等待视作操作已撤销。并发会话共享连接，但不共享取消信号或调用预算。

保存、删除或更改信任后会刷新后续任务的配置，并停止旧连接接收新调用；旧 Agent
需要在下一轮任务重新加载工具。空闲旧连接立即开始关闭，有调用正在执行时等这些调用
完成、取消或达到原有时限后关闭，不必等应用退出。刷新设置不会重放调用，也不会中断
已接收调用的正常返回；已发送的远程操作仍可能继续执行。工具分页发现、完整参数定义、
结构化结果与说明文本、错误状态均保留；特殊字符或超长名称使用稳定短哈希消歧。
对话 checkpoint 会持久化当前 `tool_schema_hash`。恢复旧对话时如果发现工具定义已经
变化，系统提示会明确要求模型以本轮注册工具为准，忽略历史消息中过时的工具可用性判断。

如需把配置放到其他位置，可在 `.env` 中设置：

```bash
MCP_CONFIG=config/mcp.json
```

## Agent Team

本节保留旧 Team 的配置与恢复说明。新任务使用 **Agent** 按需协作；当前执行方式菜单只对已有团队显示 **历史团队**，不再提供新建 Team 的常规入口。
继续旧记录时，运行时仍使用以下配置，变更后需重启后端：

```dotenv
TEAM_RUNTIME_ENABLED=true
TEAM_WRITE_ENABLED=true
```

第一项启用旧团队运行时，第二项允许旧团队代码任务。旧流程由 Lead 提交团队方案，批准后创建成员、Attempt 和代码 Worktree；已有运行中的 Team 继续使用原记录。只读权限下不能启动或接管 Team，旧团队模式暂不支持内置联网搜索。

采用 `managed` 模式的历史 Team 在候选通过审查和验证后，由 Runtime 在内部集成工作区合并、执行组合验证，并在全部任务完成后进行本地交付。交付会核对用户同期修改，保留用户 HEAD 与暂存区，将成果作为未暂存修改写回。冲突或验证失败不会推进版本，也不会自动推送到远程仓库。

旧 Team 保留 `manual` 模式。配置开关、审批和本地交付不能替代项目自身的验证命令；具体工作流与失败恢复以 [Team 自动集成与本地交付](team-managed-integration.md) 和 [Team 协作边界](team-collaboration-boundaries.md) 为准。

Team 模型响应与调用期限分别由 `TEAM_MODEL_RESPONSE_TIMEOUT` 和 `TEAM_MODEL_CALL_TIMEOUT` 控制。模型超时后保留任务、工作区和安全上下文，未知写操作不自动重放；恢复仍需检查已有执行现场。

## 运行测试

可选的 CVM 验证状态与一次性提醒见 [验证监督：开启与调试](cvm-verification-guide.md)。该功能默认关闭，沿用现有 Hook、事件与 checkpoint，不增加自动测试或强制续轮。

单次运行：

```bash
python -m codeagent "读取 README.md，并用一句话总结这个项目"
```

不传 query 会进入交互模式：

```bash
python -m codeagent
```

```bash
python -m unittest discover -s tests
```
