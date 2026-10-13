# CVM 阶段 1 实施与验收记录

> 2026-10-12 接入重构：下文保留阶段 1 的原始验收记录。当前任务输入、目标投影与提醒收集已由 `codeagent/cvm/integration.py` 中的观察 Hook 接入，Agent 统一追加提醒；规则、配置和存储格式不变。接口说明见 [使用指南](usage-guide.md)。

阶段 1 已实现独立的任务契约。启用 `contract=active` 后，Agent 会保留完整原始目标、约束、验收项、修订历史和已批准方案；“继续”不会再把这些内容替换掉。契约能够跟随普通 Web 会话的检查点保存，并在上下文压缩后重新投影给模型。

默认仍为 `off`。本阶段没有实现验证证据账本或完成判定，模型结束一轮不会自动把任务标为已完成。阶段 2 尚未开始。

## 实际改动

| 部分 | 行为 | 主要文件 |
| --- | --- | --- |
| 任务记录 | 保存完整目标、来源、要求、修订、阶段、状态、方案版本；通过 ID 引用现有 Tasks | [contract.py](../codeagent/cvm/contract.py) |
| 状态与恢复 | 独立 schema 和配置快照；旧会话建立待核实候选；坏版本、不同工作区保留原载荷并停用干预 | [state.py](../codeagent/cvm/state.py) |
| Agent 接入 | 接收真实输入；active 更新目标投影并追加稳定提醒；压缩后补回；普通子 Agent 使用独立本地契约 | [agent.py](../codeagent/agent.py) |
| Web 接入 | 恢复契约、绑定批准操作；在原终止事务中保存 `checkpoint.metadata.cvm`，运行元数据保存配置 | [factory.py](../codeagent/web/factory.py)、[scheduler.py](../codeagent/web/scheduler.py) |
| 模式开关 | contract 开放 off、shadow、active；其余组件仍只允许 off | [config.py](../codeagent/cvm/config.py) |

`execution_guard` 的格式和重置规则没有改变。新增契约不调整只读权限、Plan Mode、工具审批或轮数预算，也没有新增分类模型调用。

## 跟进消息如何处理

第一条实质请求建立任务记录，原文完整保留。为避免把含糊语言误判为授权变更，第一版使用明确的输入格式：

| 输入示例 | 结果 |
| --- | --- |
| `调查登录问题，不修改文件` | 建立任务，保留整段原文 |
| `继续`、`Continue`、`按方案执行` | 保留当前任务 ID、目标和修订号 |
| `新增约束：接口保持兼容` | 新增有来源的约束，分配 `r1` 等要求编号 |
| `验收：给出超时原因及对应日志证据` | 新增验收项；不会自动认定已满足 |
| `替换要求 r1：允许新增参数，旧调用仍可用` | 旧要求标为 superseded，新要求引用被替换项 |
| `重点检查超时，接口保持兼容` | 原文保留为待确认修订，保持原目标；不擅自判断它替换了什么 |
| `确认修订 3：约束` | 将编号 3 的待确认内容明确纳入约束 |
| `忽略修订 3` | 保留该历史记录，但不再作为未解决修订 |
| `新任务：编写计算器` | 封存旧任务，建立新任务，旧约束不自动套用 |
| `结束任务`、`取消任务` | 用户明确关闭或取消任务；关闭不代表运行时验证通过 |

多行初始请求中的 `约束：`、`验收：` 会形成结构化条目，其余内容仍完整保留。普通自然语言的语义分类尚未实现；含糊修订会降低契约置信度，并提醒模型在歧义影响行动时使用现有 `ask_user` 或直接提问。CVM 不会仅因存在待确认内容就强制多调用一轮模型。

要求编号和修订编号是不同的：`r1` 指具体要求，数字 `3` 指一次输入修订。当前可通过 SDK 导出或 Web 检查点查看，没有新增前端契约面板。

## 如何启用

在启动 CLI 或 Web 服务的 PowerShell 中设置：

```powershell
$env:CODEAGENT_CVM_CONTRACT = 'active'
```

随后从同一个终端按原来的方式启动项目；已启动的 Web 服务需要重启后读取环境配置。其他 CVM 组件保持默认 off。只记录观察、不影响提示时，使用 `shadow`；关闭新观察和提示投影时使用 `off`。回退会保留已有契约数据和会话历史，不删除过去发送过的消息。

SDK 示例：

```python
from codeagent import AgentConfig
from codeagent.cvm import CvmConfig, ContractUpdate

config = AgentConfig(model="your-model", cvm=CvmConfig(contract="active"))
# 用 config 构造现有 Agent 后，可通过真实用户输入提供明确修订：
agent.run("接口保持兼容", task_update=ContractUpdate("constraint", "接口保持兼容"))
saved = agent.export_cvm_state()
# 新 Agent 同时恢复原有 messages、context 后：
restored_agent.restore_cvm_state(saved)
restored_agent.run("继续")
```

SDK 更新和方案绑定接口属于调用方的可信接口，没有暴露为模型工具。工具输出、摘要、运行时提醒和子 Agent 报告不会成为父任务的新用户授权。普通子 Agent 记录的是自己的委派任务，不能修改父契约。

## 恢复和方案的边界

- 普通 Web 会话在原有终止落盘事务中保存契约，并在下一次 Run 恢复同一 task_id；这不新增运行中定时检查点或进程崩溃后的自动续跑。
- 旧检查点没有契约时，只从历史用户消息建立待核实候选，忽略摘要和工具结果，不用旧 `user_goal` 猜造已验证要求。可用“确认原始目标”明确确认。
- 遇到未知 schema、损坏字段或工作区变化，保留原载荷，发送 `cvm.diagnostic`，停止该契约的干预。需要在正确工作区恢复，或为新工作区建立新会话。
- 普通单 Agent 的已批准方案按 plan_id、revision、content_hash 绑定；内容哈希覆盖 Markdown 和原有方案 payload。草稿或未批准版本不能绑定。方案启动时生成的内部提示不会再次被当作用户修订。
- 本阶段覆盖普通会话、单 Agent Plan Mode 和普通子 Agent。Team Planner/Team 会话沿用现有团队机制，契约组件明确降为 off 并发出诊断；没有将普通会话的契约强行共享给团队角色。
- 给模型的契约提醒有长度限制，完整内容始终保存在契约中。被省略的要求不会被标为完成；超长任务仍可能需要查询原始历史或向用户核对，不能把短投影当作完整授权。

## 测试与复测

在仓库根目录执行：

```powershell
python tests/run_cvm_stage1.py
```

该入口强制收集阶段 0、阶段 1 的契约与恢复测试，以及上下文、前缀缓存、Plan Mode 回归。任一模式零收集、整组跳过或断言失败，均不能作为成功验收。默认报告保存到 `eval-results/cvm-stage1/`。

本次完整回归命令：

```powershell
python tests/run_cvm_stage1.py --pattern 'test_loop_guard*.py' --pattern test_tool_evidence.py --pattern test_round_budget.py --pattern test_parallel_execution.py --pattern test_cli.py --pattern test_agent.py --output eval-results/cvm-stage1/stage1-acceptance.json
```

结果：223 项测试通过，无跳过。其中阶段 1 新增 28 项，阶段 0 的 19 项继续通过，既有回归 176 项通过。六个 off 场景仍与冻结的旧基线一致；报告中的五个已知问题是 off 对照结果，不表示 active 的任务目标修复没有生效。

验收期间发现工作区另有并行的 Todo/规划改动，均已保留。验收入口已增加测试前后源码及测试文件哈希比较，期间发生变化会判为验收失败。最终重新执行的 223 项测试通过，源码在测试期间保持一致，结束后的哈希复核也通过。这些并行改动不归入本阶段 CVM 交付范围。

新增测试覆盖完整多行目标、短跟进、增补和替换要求、歧义与确认、新任务、寒暄、显式任务结束、来源隔离、方案版本变化、跨 Run、跨工作区、旧检查点、坏版本、JSON 往返、子 Agent 隔离、只读保护、真实 ContextManager 压缩后恢复，以及自动压缩后的提醒补回。shadow 对照检查实际模型请求相同；active 的短跟进测试检查修订和提醒内容稳定，不增加模型调用。

验收记录：JSON 报告（本地生成文件 `eval-results/cvm-stage1/stage1-acceptance.json`，不随仓库发布）、测试日志（本地生成文件 `eval-results/cvm-stage1/stage1-tests.log`，不随仓库发布）。测试使用本地假模型和临时工作区，未执行真实模型效果评测。没有修改前端；未运行全库测试或前端构建。

下一阶段是阶段 2：记录工具执行事实，并把验证结果绑定到具体代码版本和验证范围。本阶段尚不能阻止“测试失败却说完成”。
