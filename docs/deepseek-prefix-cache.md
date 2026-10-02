# DeepSeek 前缀稳定性改造

本轮完成请求组装、提醒、稳定提示词、压力清理与 checkpoint 改造。原始消息、Agent 工具循环、权限执行边界、恢复策略和硬预算检查继续使用原有实现。重复文件读取引用尚未实现：应在真实费用与质量对照完成后再进入第二阶段。

修改入口：

| 文件 | 修改 |
| --- | --- |
| `codeagent/agent.py`、`prompts/runtime.py` | 提示词快照、追加提醒、显式刷新、主/辅助观测绑定 |
| `codeagent/anthropic_client.py`、`context/observation.py` | 最终 SDK 参数指纹、能力判断、分片事件 |
| `codeagent/context/manager.py`、`context/models.py` | 稳定清理视图、软边界、checkpoint 状态 |
| `codeagent/config.py`、`.env.example` | 三项可配置缓存策略参数 |
| `codeagent/hooks/defaults.py`、`hooks/manager.py` | 实时权限配置指纹 |
| `codeagent/tools/registry.py`、`tools/__init__.py`、`mcp/router.py` | 独立请求工具指纹、确定性 MCP 注册 |
| `tests/test_prefix_cache.py`、`tests/test_loop_guard.py` | 新行为覆盖及旧纠偏断言迁移 |
| `tests/test_anthropic_client.py`、`test_context_nonteam_regressions.py`、`test_mcp.py` | 观测事件、准备状态、工具顺序回归 |
| `tests/test_team_worktrees.py` | 一项异步测试改为有截止时间的等待，保留原状态断言 |
| `evals/prefix_cache_replay.py` | 不付费的固定输入路径对照 |

## 请求观测

`request.observed` 位于 `AnthropicModelClient` 调用 SDK 前，包括普通 `messages.create`、流式 `messages.stream` 和省略输出限制时的 `post` 路径。观察包含 SDK 的 `extra_body` 推理配置。此处记录的是**客户端 SDK 参数**，不是 HTTP 编码后的字节，更不是服务端缓存键；SDK 内部自动重试也不是独立可观测的模型调用。

- SHA-256 使用 UTF-8、JSON 对象键排序、紧凑分隔符；数组保持实际顺序。
- system、整个有序 tools、各 message、其他请求配置和整个请求分别记录 hash、字符数和字节数。
- 记录 `first_difference`、`first_changed_message`（从 0 开始）、`append_only`、`unchanged`、`history_rewritten`、`changed_components`。
- 携带模型、`call_kind`、`summary_revision`、`history_generation`、`prompt_revision` 和 `rewrite_reasons`。无法确定消息改写原因时显式记录 `unexpected_message_prefix_change`。
- 超过 100 条消息时，后续指纹以 `request.messages_observed` 分片记录，通过 `call_id`、请求 hash 和偏移关联，避免事件大小和数组裁剪限制吞掉记录。
- 只新增 hash、长度和必要元数据日志；正文只存在于既有历史/客户端快照中。未增加完整提示词或消息日志。
- main、context_summary、memory_selection、memory_maintenance 按调用类型使用独立基线。子 Agent 和不同会话拥有独立状态；辅助调用不操作主会话提醒。
- 自定义 fake/SDK-neutral client 的观测标注为 `model_client_parameters`，不会冒充最终 Anthropic SDK 参数。

usage 仍来自 provider 的 Anthropic-compatible 返回值。保持四个互斥口径：普通输入、缓存写入输入、缓存读取输入、输出。总输入仅相加一次；汇总命中率为缓存读取输入总和 / 总输入总和，而不是每次百分比的平均值。没有 usage 时不推测真实用量或费用。

## 提示词和运行时提醒

`RuntimeState.prompt_snapshot` 保存 system、片段 trace、内容指纹、配置指纹及快照版本。相同配置复用相同 system。首次组装的日期属于会话快照；之后日期变化以运行时消息追加，跨日恢复也不会仅因日期重写 system。

配置身份覆盖模式、实际有序工具定义、提示词预算、工作区/平台、模型/输出限制/推理设置、技能和记忆目录、项目与模板规则、内置权限策略。规则内容仍在发送前核验，安全变更自动刷新；不会为了缓存忽略刚修改的安全规则。规则文件读取不会直接改变请求，只有内容发生变化才刷新组装。

显式刷新接口：`Agent.refresh_project_rules()`；自定义安全/权限 Hook 可调用 `Agent.invalidate_prompt("safety_rules_changed")`。内置 `PermissionPolicy` 的规则变动自动识别。权限 Hook 每次工具执行仍实时检查，缓存不参与授权判断。恢复重试同样重查当前模式、规则与工具配置。

Code / Discuss 共用 `single_agent` 提示词配置身份（快照版本 2），因此在两者之间切换
不会重建 system。固定模板同时定义两种模式，Code 专属片段带条件范围。当前模式通过
历史尾部的运行时通知传递，首次请求、切换或通知被压缩移除时追加；通知仍可见则不重复。
Team / Subagent 的角色模板保持独立。旧版快照升级重建一次，后端 hook 仍实时决定工具权限。

loop guard 的新增、更新、解除通过独立 `source="runtime"` 用户消息表达。其 hash 去重同时检查提醒是否仍在实际请求视图中；压缩移除后可以重新注入。提醒位于完整工具结果之后，原消息从不被改写。预检压缩若移除必要的纠偏或当前日期提醒，会追加并重新执行完整预算检查。停止条件、计数和 `feedback_sent()` 的时机继续使用原循环保护机制。

工具注册表仍按注册顺序发送；新增 `tool_request_hash()` 用于请求观测和提示词生命周期，反映这个顺序。原 `tool_schema_hash()` 保留排序后的语义指纹，兼容原有调用者和检索评测。MCP 发现结果在注册时按公开工具名确定性排列，避免远端分页顺序改变请求。旧 checkpoint 的排序型工具 hash 可能触发一次受控刷新。

## 清理策略与默认值依据

本地配置复核：主模型与摘要模型为 `deepseek-v4-flash`，输出上限 8,000，旧字符软触发值为 300,000。生产窗口仍来自现有 `/models` 解析；未知窗口不会根据名称猜测。

截至 2026-10-02，官方[模型文档](https://api-docs.deepseek.com/quick_start/pricing/)列出 1M 窗口，且旧 Flash 名称由当前 Flash 服务。这里不把网页数字硬编码为所有模型的运行窗口。官方[缓存说明](https://api-docs.deepseek.com/guides/kv_cache/)强调前缀匹配、缓存持久化单位与尽力命中，客户端快照不能保证服务端缓存仍存在。

| 设置 | 默认 | 行为 |
| --- | --- | --- |
| `CONTEXT_CACHE_POLICY` | `auto` | 官方 DeepSeek 端点和已知模型、窗口已解析时启用缓存友好策略；其他情况使用原软压力策略 |
| `CONTEXT_CACHE_SOFT_RATIO` | `0.8` | 延续原窗口策略的 20% 余量，同时用于输出预留后的可用输入窗口和字符硬上限 |
| `CONTEXT_CACHE_BOUNDARY_GROWTH_RATIO` | `0.1` | 上一压力边界处理后，新增输入达到约 10% 容量才再次推进清理；硬超限跳过此等待 |
| `CONTEXT_MAX_REQUEST_CHARS` | `600000` | 原请求体字符硬上限，未提高 |
| `CONTEXT_COMPACT_THRESHOLD_CHARS` | `300000` | `legacy` / 未知能力下的原软触发值 |

缓存友好策略的字符软边界因此为 480,000。若实际窗口为 1,000,000、输出预留为 8,000，则输入估算软边界为 793,600 tokens；两项中先触发者生效。现有估算是完整请求 UTF-8 字节数 / 2，并对媒体预留 8,192 tokens/块，**不是 tokenizer 上界保证**。对常见文本，600,000 字符的请求体限制通常先于 1M 窗口生效；CJK、媒体和较大输出预留会改变这个关系。以上是保守的初始配置，尚未经过费用优化实测。

清理使用独立 `request_view`：只保存确实改写的消息及其原始指纹、摘要/配置身份、边界水位。下一轮先验证源消息再重放同一清理结果，再追加新消息；不会因为新工具轮次进入“最近 N 轮”窗口，就每次推进清理范围，也不会从原始历史反复恢复已清理全文。

软压力仅决定何时尝试清理/摘要。完整请求的输出预留、字符和模型窗口硬检查每次都执行；超限、用户压缩、恢复压缩不受软边界等待限制。没有新增“判断是否压缩”的模型调用。摘要仍使用原来的可压缩性预检、至少 5%/256 字符收益门槛、失败冷却与完整工具边界。

这不是无限保留历史的策略。必要清理仍批量发生；无用材料可由用户主动压缩。默认策略的输入量可能增加，必须以**同等质量下的所有调用总费用**验收，不能只看命中率。

## checkpoint 兼容性

新增状态使用 `RuntimeState` 默认值和既有序列化/恢复路径，无数据库 schema 迁移：

- `prompt_snapshot` / `prompt_revision`；
- `request_baselines`（仅指纹，含独立辅助调用基线）；
- `runtime_reminders`（提醒身份/去重状态）；
- `request_view`（清理结果及边界）。

旧 checkpoint 缺失字段时从默认状态重建；快照版本、配置或源历史不匹配时重建并记录原因。恢复后相同配置、相同消息的 SDK 参数可逐字段相同。服务端缓存是否仍存在由 provider 决定。请求快照不会进入 `to_summary_source()` 的任务证据。

## 验证和后续真实 API 对照

行为测试入口：

```powershell
python -m unittest tests.test_prefix_cache -v
python -m unittest discover -s tests -q
python -m evals.prefix_cache_replay
```

`tests/test_prefix_cache.py` 通过 fake SDK 捕获实际参数，覆盖普通工具循环、纠偏三种变动和重试、SDK 推理参数、工具顺序、独立辅助基线、checkpoint/旧格式、日期、权限和规则即时刷新、压缩重注入、工具配对、指纹分片、usage 加权、清理边界和硬限制。

2026-10-02 的本地验证结果：

- 新增前缀行为测试 22 项通过；loop guard 原有 36 项在迁移至消息提醒断言后通过，保留停止/重试/恢复语义检查。
- 上下文专项 `test_context*.py`：227 项完成，1 项跳过，其余通过。
- 最终联合重跑（前缀、loop guard、工具、SDK、历史观测、MCP、上下文遥测与异步恢复等待）：108 项通过；检索评测 E01 的兼容性重跑也通过。
- 两项真实 Windows 子进程终止测试在沙箱中失败；在获准的沙箱外重跑，2 项均通过。
- 全量初次运行 923 项、跳过 6 项。发现的本次相关断言/兼容性问题已修复并做上述定向重跑；没有把这次初跑表述为全量通过。
- 仍存在 `test_team_worktrees.TeamWorktreeTests.test_supervisor_rechecks_scope_before_resuming_a_frozen_attempt` 错误：恢复时判定 `outside.txt` 仍超出写入范围。在临时副本中将本轮修改的源码文件恢复到 HEAD、同时保留开始任务前已有的 Team 修改后，同一错误仍然复现。未改动该范围检查实现或降低权限断言。
- `git diff --check` 通过。没有启动真实付费模型测试。

上传前补充复核（2026-10-02）：全量运行 935 项，933 项通过、1 项跳过，
唯一错误仍是上述 Worktree 恢复测试。进一步检查确认：临时 Git 仓库中的原文件使用 LF，
Windows 的 `write_text` 恢复操作生成 CRLF；在 `core.autocrlf=input` 下，文件内容差异虽已
归一化为空，Git 状态仍标记修改，因而被范围检查拒绝。测试改为保存并恢复原始字节，
产品的写入范围检查与原有恢复断言保持不变。此次修复仅涉及测试夹具，不改变缓存策略。
修复后重跑 `tests.test_team_worktrees`，34 项全部通过；未再重复运行完整后端测试集。
前端 41 项测试及生产构建通过，构建仍提示主 JavaScript 包超过 500 kB。

离线路径对照见 [prefix-cache-offline-replay.json](prefix-cache-offline-replay.json)。24 个相同工具结果和相同反馈时间点下，旧“system 反馈 + 每次滑动清理”路径与新路径比较：历史改写 **16 → 2**，system 变化 **3 → 0**，仅尾部追加的转换 **6 → 21**。原始工具结果全部保留，两次新清理边界可追踪；两组均关闭模型摘要。此对照只复现两个旧路径，不是完整旧版本 Agent 的任务质量对照。

同一离线对照累计请求字符 **715,919 → 1,505,903**，表明稳定前缀会带来保留上下文的成本权衡。真实命中/未命中/输出量、主调用/辅助调用费用与 API 耗时均未测量，JSON 中保留 `null`，不得把这个结果称作“实际降费”。离线运行耗时只是本地 Python 时间。

真实 A/B 需要固定模型实际版本、任务、初始文件、工具结果和推理/输出设置，交错顺序多次运行并独立评估完成质量。以 provider usage 按调用类型汇总命中、未命中、输出，计入摘要、记忆、子 Agent 等所有费用；使用测试时的实际价格/时段，区分估算费用与账单扣费。利用新增事件统计改写次数、最早改写位置、清理边界、压缩次数，并结合 `model.completed.duration_ms` 与整任务耗时比较。现有 `evals/context_suite/token_report.py`、`cost_report.py` 可继续用于保留的 SDK 用量证据，但其旧 A/D 变体不自动等价于本次改造前后版本。

99% 仅作为长会话观察目标；本轮未调用真实付费 API，未证明费用下降，也不承诺“一亿 token 约 7 元”。
