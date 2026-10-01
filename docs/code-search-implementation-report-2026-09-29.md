# 按需求找代码：实施与实际对比

日期：2026-09-29。

**已完成第一阶段代码与测试。本轮工具调用、耗时和费用下降，但没有测出精准度提升，首条命中反而下降，因此没有达到“质量不退步”的预设目标。**

## 实际增加了什么

新增 `search_code(query, path=".", top_k=5, keywords=None)`。现在 Agent 可以直接提交需求，让工具在函数索引里查找，并同时得到函数名、路径、行号和连续源码。此前主要靠自己多次猜关键词、grep 再读取文件。

- Python AST 提取函数、异步函数、方法、装饰器位置；长函数分块并去重，语法错误降级为文本片段。
- SQLite FTS5 检索名称、签名、路径、注释和正文，拆分代码标识符及中文双字；原查询和扩展查询用 RRF 合并，精确名称优先。
- 必要时复用现有模型改写一次关键词；Agent 已给关键词就不另调模型。改写共享 SDK、接口地址、执行预算和费用记录，失败继续原查询。
- 默认返回五个函数，每个片段至多 20 行，总输出约束在 8,000 字符内。返回前读取当前源码核对哈希；新建、修改、删除文件会更新索引，缓存忙或损坏时降级扫描。
- 普通 CLI/Web 根 Agent 与讨论模式接入。Team、子 Agent 和原有工具行为保留，未增加外部数据库、rg 依赖、embedding、代码图或主系统提示词修改。

实现入口：[检索服务](../codeagent/code_search/service.py)、[索引](../codeagent/code_search/index.py)、[工具接口](../codeagent/tools/search_code.py)。产品用法和边界见 [使用说明](code-search.md)。

## 评分器修正与检索收益分开

原始 A 为 **14/16 = 87.5%**。N08 从 `@staticmethod` 开始引用是合法源码，但旧评分器从 `def` 行开始计算函数范围，错误拒绝了这段证据。只修正这个边界、不重跑模型，A 变成 **15/16 = 93.75%**。

**这 6.25 个百分点完全是评分修正，不是检索能力提升。** 原始报告、预测、冻结语料 ID 和校验封印没有修改；新规则带独立评分版本。

复核 A 的 18 个未标注候选：增加 3 个合理替代实现，其余 15 个按题意标为非相关。替代实现是 N02 的另一处递归脱敏，以及 C01 中两处直接按错误文本标记失败的 Team 执行记录。之后复核 B 新出现的 7 个候选，均是调用点或不符合题目要求的辅助实现。双方最终使用完全相同的标签，剩余待复核项为 0。

复核标准保留了严格原文、行号、20 行上限和关键行为要求，没有为 B 放宽引用格式。增加替代实现影响 nDCG、噪声等指标，也不能单独称为检索收益。

## 正式 A/B 数值

两组均使用 `deepseek-v4-flash`、同一接口、冻结的 `code-retrieval-v2` 源码和题目。各 20 题，均完成 20/20；有答案题 16、无答案题 4。每题新进程和冷索引，总预算仍为 12 次实际模型请求、24 次工具、300,000 token、120 秒。B 的按需改写计入同一个上限。

下表 A 使用修正并复核后的共同评分标准。

| 指标 | 改造前 A | 改造后 B | 本轮变化 |
| --- | ---: | ---: | --- |
| 首条严格命中 Hit@1 | 15/16，93.75% | 14/16，87.50% | **下降 6.25 个百分点** |
| 前五严格命中 Hit@5 | 15/16，93.75% | 15/16，93.75% | 持平 |
| 跨文件找全 | 4/4 | 4/4 | 持平 |
| 无答案误报 | 0/4 | 0/4 | 持平；两组均正确拒答 4 题 |
| MRR@5 | 0.9375 | 0.890625 | 下降 |
| nDCG@5 | 0.9325 | 0.8887 | 下降 |
| 工具调用中位数 | 7.5 | 6.5 | 减少 13.3% |
| 工具调用总数 | 166 | 144 | 减少 22 次 |
| 每题输入 token 中位数，含缓存 | 54,393 | 51,631.5 | 减少 5.1% |
| 查询耗时中位数 | 11.8905 秒 | 10.203 秒 | 减少 14.2% |
| 初始化耗时中位数 | 1.141 秒 | 0.703 秒 | 单列观察，不归因于索引 |
| 整套空闲价估算费用 | ¥0.45841844 | ¥0.41989536 | **减少 ¥0.03852308，约 8.4%** |
| 平均每题费用 | ¥0.02292092 | ¥0.02099477 | 减少约 ¥0.00193 |

完整自动报告：[A/B 对比](../eval-results/code-search-implementation/comparison-final/report.md)、[共同规则下的 A](../eval-results/code-search-implementation/before-final/report.md)、[共同规则下的 B](../eval-results/code-search-implementation/after-final/report.md)。逐题费用、用量、引用判定见各目录 `result.json`。

## 哪些问题没有解决

**N04 两组都找到正确函数，但都写错引用行号。** `classify_exception` 的片段实际从第 25 行开始，答案写成第 31 行，继续按严格规则判失败。不能把它描述成两组都找不到认证错误分类。

**N02 是首条退步题。** B 第一条的 `_redact` 路径、函数和原文位置正确，但给了 22 行，超过上限 20 行，因此首条不计分。第 4 条 `_display_value` 是复核确认的合法替代实现，所以 Hit@5 仍命中。这题 B 没有调用 `search_code`，不能断言退步是索引排名导致；但它确实是本次整体 B 的退步，不能排除后再报成绩。

仅看“路径和函数名是否出现在前五”的诊断，两组仍为 **16/16**。它不要求原文、行号和关键行为证据，不能拿来替代严格分数。当前题库的函数定位已经接近饱和，本轮主要差异仍在检索过程和最终引用。

按预设目标，前五质量、跨文件、无答案保持；首条质量未保持。工具中位数目标 ≤6，实际 6.5，未达到；输入减少约 20%，实际约 5.1%，未达到。费用不高于基线达到了。**不能宣布全部目标完成或“精准检索能力明显提升”。**

## 新工具实际上用了多少

正式 B 有 **5/20 题**调用 `search_code`，共 5 次：C01、E01、E03、N08、Z03。其余仍使用旧工具；没有强制改写主系统提示。

其中只有 N08 触发了额外模型改写：输入 100、输出 393 token，估算 ¥0.001672，已经包含在 B 总费用内。实际事件标记为 `call_kind=code_search_rewrite`，请求和用量记录均保留。

五次搜索均为冷启动，查询用时中位数约 **2.781 秒**，包含索引构建；总返回 17,757 字符。索引在首次查询中建立，不能只看 `setup_ms` 来判断索引代价。小型直接调用检查观察到已建索引的两个无改写查询约 0.156/0.172 秒，但这不是正式 A/B 数据，也不替换冷启动成绩。

这次对比测的是“Agent 多了一个可选检索工具”后的整体表现。仅 20 个问题、每题一次、使用提供方默认采样，而且只五题用了新工具；耗时受网络影响，工具描述也可能改变搜索策略。因此，8.4% 节省是本轮观测值，不是稳定收益保证，也不能全部归因于排序算法。

## 测试、调试过程和费用

针对性检查共 **84 项，83 项通过，1 项跳过**。跳过项是本机没有创建符号链接的权限；普通路径越界检查通过。没有运行全项目大型验收。

覆盖函数/装饰器证据、长函数去重、输出上限、忽略规则、编码、文件新增/修改/删除、保留 mtime 的候选变化、语法错误、SQLite 损坏/忙、取消、共享模型预算、工具注册、CLI/Web/讨论兼容、Team/子 Agent 不自动开放，以及现有评测统计和离线链路。Windows SQLite 连接未及时关闭的问题在测试中发现并修复。

测试命令分组：

```powershell
python -m unittest tests.test_code_search
python -m unittest tests.test_code_retrieval tests.test_search_tools tests.test_cli tests.test_discuss tests.test_runtime_data_paths tests.test_tools
python -m unittest tests.test_loop_guard_web tests.test_team_lead.LeadTeamPlanToolTests.test_factory_registers_entry_only_for_explicit_team_planner
```

真实调试过程没有删除失败记录：

| 运行 | 结果 | 空闲价估算 |
| --- | --- | ---: |
| 第一轮 4 道调试题 | 完成 2/4，另两题耗尽请求；模型未用新工具 | ¥0.13256348 |
| 调整工具说明后的 4 道调试题 | 完成且严格命中 4/4，3 题用新工具 | ¥0.10007684 |
| 4 个调试需求直接调用产品工具 | 检查冷/热索引及真实改写，不作为 Agent 成绩 | ¥0.00253100 |
| 锁定实现后的正式 B | 完成 20/20，按上述共同规则统计 | ¥0.41989536 |
| **本次新增调用合计** | 包含失败调试与改写，不含用户先前的 A | **¥0.65506668** |

直接调用检查发现 300 输出 token 对推理模型有时不足以给出关键词 JSON；该次降级及费用已保留。正式运行前将改写上限设为 768，仍只请求一次、仍受总体预算约束。正式 B 只跑了一整轮，没有挑选最好题目拼成绩，也没有根据正式失败题继续调整检索参数。

## 保留与复现

已有未提交修改保留。任务开始时将现有程序快照存于 `eval-results/code-search-implementation/pre-change`；本次只修改原有程序中的 CLI、Web 工厂、默认工具注册、讨论白名单、数据目录，以及评测器引用边界/报告文案，并新增检索模块。其他先前修改的程序文件与任务开始快照逐字节一致。

正式 B 的 `engine/` 保存实际程序版本；最终产品代码与该快照一致。自动变化清单中的 `pricing.py`、`runner.py` 是此前费用和环境配置修复，已存在于本任务开始快照，本次没有再修改它们。A/B 使用相同价格重算，主系统提示、原有搜索工具和上下文逻辑没有在本轮变动。

复核记录：[A 候选](../eval-results/code-search-implementation/source-review-before.json)、[B 新候选](../eval-results/code-search-implementation/source-review-after.json)、[共同标签](../eval-results/code-search-implementation/final-gold.json)、[调用与变更汇总](../eval-results/code-search-implementation/evidence-summary.json)。原始 A 保留在 `eval-results/code-retrieval-before`，正式 B 在 `eval-results/code-retrieval-after`。

用现有证据再次生成共同规则对比，无需模型调用。输出目录必须换成尚不存在的新目录：

```powershell
python -m evals.code_retrieval grade --bundle eval-results/code-retrieval-v2 --run eval-results/code-retrieval-before --gold eval-results/code-search-implementation/final-gold.json --output eval-results/before-recheck
python -m evals.code_retrieval grade --bundle eval-results/code-retrieval-v2 --run eval-results/code-retrieval-after --gold eval-results/code-search-implementation/final-gold.json --output eval-results/after-recheck
python -m evals.code_retrieval compare --before eval-results/before-recheck --after eval-results/after-recheck --output eval-results/comparison-recheck
```

首版保留为可选工具，Agent 可继续用原工具。当前主要不足是模型未必选择它、词法扩展仍可能漏检、最终引用仍会出错；不据此扩展到向量库、代码图或强制搜索流程。本轮交付的是可运行、可测量的需求检索入口，不是已经证明准确率更高的完整方案。
