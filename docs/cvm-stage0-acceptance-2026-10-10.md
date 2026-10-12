# CVM 阶段 0 实施与验收记录

阶段 0 已建立配置入口、六个可重复的离线样本和改动前的请求基线。默认关闭 CVM，现有 Agent 行为保持兼容。任务契约、验证证据账本和完成判定仍由后续阶段实现。

## 已交付内容

| 内容 | 文件 | 用途 |
| --- | --- | --- |
| 统一配置 | [CvmConfig](../codeagent/cvm/config.py) | 不可变配置对象；CLI、Web、SDK 和普通子 Agent 传递同一个配置 |
| 六类样本 | [cvm_fixtures.py](../tests/cvm_fixtures.py) | 固定用户输入、模型响应、工具模拟行为、问题分类和未来期望 |
| 改动前基线 | [legacy-off-v1.json](../tests/fixtures/cvm/legacy-off-v1.json) | 完整请求内容、工具原始事实、源码哈希、提交号、脏文件清单和运行配置 |
| 兼容与配置测试 | [test_cvm_baseline.py](../tests/test_cvm_baseline.py) | 默认配置与显式 off 对照旧基线；检查入口传递和非法配置 |
| 验收入口 | [run_cvm_baseline.py](../tests/run_cvm_baseline.py) | 测试失败、零收集、整组跳过均不允许成功；生成当前运行报告 |

旧基线在修改生产代码之前捕获，提交为 `f520186a6c67add3f1130caed569226e97f5bb46`，当时 `codeagent/` 没有未提交改动。样本进入真实 Agent 循环，保留默认权限 hook、LoopGuard 和轮数预算；模型及工具响应由固定脚本驱动，文件写入发生在临时工作区。

比较覆盖实际交给模型客户端的全部请求参数，包括 system、tools、messages，以及终止原因、最终文本、迭代数、工具事实和相关终止事件。为消除随机差异，夹具固定提示日期、平台说明、执行 ID 和归档 ID，并将临时工作区前缀规范化。完整请求按内容哈希去重保存，不丢弃提示或工具结果文本。这里验证的是规范化后请求内容相同，不宣称真实网络传输字节经过测试。

## 六个样本的结论

| 样本 | 当前 off 观测 | 后续阶段的目标 |
| --- | --- | --- |
| 短跟进覆盖目标 | `Continue` 替换 `RuntimeState.user_goal`，原始目标仍在历史消息中 | 阶段 1：独立任务契约保留原始目标和验收要求 |
| 测试失败后声称成功 | 工具退出码 1，模型仍可输出“全部通过”并正常结束 | 阶段 3：不能认定为已验证完成 |
| 验证通过后再改代码 | 通过时的文件哈希与最终文件哈希不同，仍可正常结束 | 阶段 2：旧验证失效；阶段 3 消费这一事实 |
| 不相关测试掩盖失败 | `test_target` 失败，`test_other` 通过，模型仍可声称完成 | 阶段 2：记录验证范围，不用无关成功抵销失败 |
| 验证环境缺失 | 工具退出码 127，模型仍可声称完成 | 阶段 3：明确受阻或未验证 |
| 正常调研保护 | 连续五次成功读取，正常结束，没有循环警告或工具阻断 | 阶段 5：新增进度检测后仍保留这个正确行为 |

前五项是脚本诱发的已知问题观测，第六项是正确行为的保护样本。测试通过意味着复现与兼容性通过，不能解释成问题已经修复，也不能据此估计真实模型的假完成率。独立观测器只判断这些固定场景，不是生产验证器。

## 配置怎么使用

默认不用配置，五个组件全部为 `off`。可以显式设置：

```powershell
$env:CODEAGENT_CVM_CONTRACT = 'off'
$env:CODEAGENT_CVM_EVIDENCE = 'off'
$env:CODEAGENT_CVM_COMPLETION = 'off'
$env:CODEAGENT_CVM_SIGNALS = 'off'
$env:CODEAGENT_CVM_PROGRESS = 'off'
```

SDK 使用 `AgentConfig(model='...', cvm=CvmConfig())`；环境配置通过 `EnvironmentConfig.cvm_config` 和 `to_agent_config()` 进入 CLI/Web。`snapshot()` 返回不含凭据的独立配置字典。

阶段 0 设置 `shadow` 或 `active` 会抛出明确异常；其他非法值也会被拒绝。后续各组件实现后，再开放其模式和依赖关系校验，不能把这些保留值当作当前已经生效的功能。

生产请求没有新增 CVM 提醒、工具或模型调用，生产 checkpoint 尚未加入 CVM 运行状态。当前配置快照记录在离线基线报告中；阶段 1 再接入生产任务状态和检查点，避免阶段 0 改动原有持久化语义。

## 怎样复测

在仓库根目录执行本阶段验收：

```powershell
python tests/run_cvm_baseline.py
```

应得到 19 项测试通过、6 个样本、`off_matches_legacy=True`。默认在 `eval-results/cvm-stage0/` 生成带时间戳的 JSON 报告。

本次完整回归使用：

```powershell
python tests/run_cvm_baseline.py --pattern 'test_loop_guard*.py' --pattern test_tool_evidence.py --pattern test_plan_mode.py --pattern test_round_budget.py --pattern test_prefix_cache.py --pattern test_parallel_execution.py --pattern test_context_nonteam_regressions.py --pattern test_cli.py --output eval-results/cvm-stage0/stage0-acceptance.json
```

还可以验证入口不会把不存在的下一阶段测试误算为通过：

```powershell
python tests/run_cvm_baseline.py --pattern 'test_cvm_contract*.py'
$LASTEXITCODE
```

在阶段 1 尚未添加该测试时，应提示零收集，退出码为 `2`。普通 `unittest discover` 本身仍允许零测试成功，因此阶段验收应使用这里的受检入口。

## 本次验收结果

- 179 项测试通过：19 项新增测试和 160 项既有回归；无跳过。
- 六个固定场景全部符合改动前观测；默认及显式 off 请求与旧基线一致。
- 机器可读报告（本地生成文件 `eval-results/cvm-stage0/stage0-acceptance.json`，不随仓库发布） 和 测试日志（本地生成文件 `eval-results/cvm-stage0/stage0-tests.log`，不随仓库发布） 保存在本地忽略目录；冻结的旧基线位于 `tests/fixtures/cvm/`，供代码版本管理保留。
- 报告记录当前源码哈希、提交号、脏文件清单、测试入口哈希、配置、样本输入与脚本、工具输入与原始输出事实。样本禁止网络连接和 DNS 查询，不读取 `.env` 或调用真实模型。
- 首轮完整回归曾出现三个 Git 初始化用例失败：新增环境配置测试清空环境变量后，Windows 临时目录被缓存为项目目录。已修正测试隔离，并重跑完整回归通过；没有修改生产 Git 或 Plan Mode 逻辑。

覆盖范围是上述模块的局部回归，没有运行全库测试、前端构建或真实模型评测。本阶段未修改前端。

## 阶段 1 接续位置

下一阶段从任务契约开始：建立独立原始目标、修订、约束和验收项，处理 `Continue` 等短跟进，接入批准计划与 checkpoint。现有 `followup_goal` 样本已经保存未来期望，可新增 active 模式断言；本次 off 兼容断言继续单独保留。不要把“旧行为仍然错误”复制成 active 模式的验收要求。

阶段 0 到此结束，阶段 1 尚未实施。
