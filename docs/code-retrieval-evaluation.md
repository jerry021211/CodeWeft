# 代码定位能力：改造前后怎么测

这套脚本已经具备“冻结题库 → 运行项目 Agent → 判分 → 出报告 → 对比”的完整流程。**当前先测现有工具，等 `search_code` 实现后再测一次。** 首版是 20 道正式题和 4 道调试题，检索范围为本项目的 Python 源码与测试，不包含前端 TypeScript。

这里测的是“同一个模型借助工具，最终能不能定位对代码”，不是改代码成功率，也不是单次搜索接口的排名成绩。

## 题目参考了什么

检索日期：2026-09-28。使用以下项目的一手资料，借鉴方法后，再核对本项目源码编题；没有照抄不适合本项目的问题，也不声称获得这些基准的官方成绩。

| 参考 | 原方法 | 本项目采用的部分 |
| --- | --- | --- |
| [RepoQA · Search Needle Function](https://github.com/evalplus/repoqa#-search-needle-function-snf) | 根据自然语言功能描述定位目标函数；原任务侧重长上下文理解 | 自然语言题不直接给出函数名，让模型根据行为寻找实现 |
| [CodeSearchNet · Evaluation](https://github.com/github/CodeSearchNet#evaluation) | 自然语言代码搜索、人工相关性判断、nDCG 排名评价 | 每题关联真实函数和关键行为代码，按有序结果评分；保留待复核候选 |
| [BEIR](https://github.com/beir-cellar/beir) | 分开保存语料、问题、相关性标签，并统一计算检索指标 | `corpus.jsonl`、`queries.jsonl`、`qrels.tsv` 分离，以及 Hit、MRR、nDCG 统计 |
| [CodeRAG-Bench](https://aclanthology.org/2025.findings-naacl.176/) | 研究检索与代码生成效果之间的关系 | 区分“找到代码”与“完成编程任务”，不把定位提升解释成修复成功率提升 |

跨文件题、无答案题、源码引用检查是为本项目增加的规则。当前标签由助手逐项核对函数与关键代码，**尚未经过多人独立人工标注**，适合先做内部前后对比。

## 题目组成

| 类型 | 正式题数量 | 要检查的能力 | 例子 |
| --- | ---: | --- | --- |
| 已知名称 | 4 | 已知函数名时，能否快速找到关键实现 | 工具列表顺序改变，哪里保持摘要一致？ |
| 只描述需求 | 8 | 不给函数名，能否理解行为并定位 | 旧对话缺少工具结果，哪里补错误占位记录而不重放工具？ |
| 跨文件 | 4 | 能否找全一个流程的多个必要环节 | 哪里把异常变成返回文本，哪里把该文本标成失败？ |
| 没有实现 | 4 | 能否正确说明不存在，避免硬找一段不相关代码 | 项目是否内置了 Redis 分布式任务锁？ |

正式题编号 E01–E04、N01–N08、C01–C04、Z01–Z04。调试题 D01–D04 涵盖取消等待、文件起始行号、路径边界和缺失用量记录，与正式题的目标函数分开。此前已经讨论过的取消与 offset 示例只放入调试集。

全部题目在 `evals/code_retrieval/cases.py`，冻结后还会生成便于阅读的 `questions.md`。每个正例答案都有源码位置、函数名和关键行为所在行。无答案题检查的是**仓库内置实现**，不包括用户后来配置的外部工具；关键词检查只是防止源码变化后继续使用旧标签，并非“不存在”的充分证明。

不要根据正式题成绩反复调检索参数。先用 4 道调试题完成开发，锁定实现后再跑正式题。正式题规模小，后续若需要对外结论，应增加未看过的真实用户问题并独立复核标签。

## 看哪些数字

| 指标 | 通俗解释 | 计算与方向 |
| --- | --- | --- |
| Hit@1 | 第一条就找对了吗 | 有答案的 16 题中，第一条引用命中的比例；越高越好 |
| Hit@5 | 前五条里有没有正确代码 | 16 题中至少命中一个必要实现的比例；越高越好 |
| 跨文件找全率 | 一个流程的几个环节是否全找到了 | 4 道跨文件题中，前五条覆盖所有必要证据组的比例；越高越好 |
| 无答案误报率 | 本来没有实现，却声称找到了 | 4 道无答案题中返回 `found` 的比例；越低越好，同时看正确拒答与未完成数 |
| MRR@5 | 第一个正确答案排得靠不靠前 | 首次命中的排名取倒数，前五未命中记 0，再对有答案题取平均 |
| 二值 nDCG@5 | 多个正确答案是否都排在前面 | 正确且不重复的引用给 1 分，按排名折扣，与理想排序相比；不是 CodeSearchNet 原版分级标签 |
| 时间、token、工具次数 | 为找到代码花了多少代价 | 报告中位数和用量分项；同时核对完成任务数，失败得快不等于检索更快 |
| 估算费用 | 按指定价格，这轮模型调用花多少钱 | 展示总费用、三类费用分项、每题费用、平均费用及 A/B 差额；使用下方空闲时段价 |

跨文件题的 Hit@5 只表示找到了一个环节，**找全率**才表示全部找到。普通输入、缓存读取、缓存写入、输出 token 分开保留；输入中位数将前三项相加。任何调用缺少完整用量，这道题的用量就标为未知，不能把它当成免费。

举例：前五命中从 10/16 变成 13/16，就是 **62.5% → 81.25%，增加 18.75 个百分点，多解决 3 道题**。这是说明算法的例子，不是当前实测结果。正式正例每变化一道题就是 6.25 个百分点，因此应看具体题目，不只看百分比。

默认每题一遍。若结果波动明显，A/B 同时用 `--repeats 3` 重跑整套，全部纳入统计；不要只重跑失败题后挑最好成绩。部分正式题从不同角度考查同一个函数，例如单函数定位与跨文件流程，不能把这些题看作完全独立的统计样本。重复运行也不是更多独立问题，首版不自动宣称统计显著或泛化提升。

## 运行方式

以下命令在项目根目录运行，使用当前已安装项目依赖的 Python，要求系统有 `git`，不需要安装 `rg`。检索使用项目自带的 `glob`、`grep`、`read_file`。所有输出目录必须是新目录，脚本不会覆盖旧证据。

### 1. 冻结改造前代码和题库（一次即可）

```powershell
python -m evals.code_retrieval prepare --output eval-results/code-retrieval-v2
```

如果这个目录已经由本次工作生成，直接复用，不要再次 prepare。快照包含当前未提交源码；后面就算工作区改了，A 仍从冻结的程序运行，A/B 仍检索冻结的目标代码。

当前使用 **v2 快照**：已移除评测额外加入的 `rg` 工具。旧 `code-retrieval-v1` 和旧离线报告仅保留记录，不再用于新一轮 A/B 评测；新版运行入口会拒绝误用旧工具配置。题目与评分规则不因这次调整改变。

### 2. 正式测改造前的 A 组

真实模式沿用项目的模型配置：`MODEL_ID`，以及 `API_KEY` / `ANTHROPIC_API_KEY`、`BASE_URL` / `ANTHROPIC_BASE_URL`。读取项目目录或最近父目录的 `.env`，同名进程环境变量优先；两种名称同时存在时，优先选择 `API_KEY`、`BASE_URL`，与项目逻辑一致。无需为评测重复配置。真实模式会调用模型并产生用量；也可通过 `--model` 指定固定模型版本。

配置读取在启动冻结程序之前完成，密钥通过进程环境传入，不写进快照。现有 v2 快照可以直接使用这些配置别名，无需重新 prepare。

```powershell
python -m evals.code_retrieval run --bundle eval-results/code-retrieval-v2 --variant baseline --mode live --split test --output eval-results/code-retrieval-before
```

完成后自动生成 `eval-results/code-retrieval-before/report/report.md`。20 道题指 20 次 Agent 任务，不是 20 次 API 调用；每道任务最多 12 次模型请求、24 次工具调用、120 秒查询时间、90 秒初始化时间。

如只验证脚本链路，用下面命令，不会调用真实模型：

```powershell
python -m evals.code_retrieval run --bundle eval-results/code-retrieval-v2 --mode offline --split dev --output eval-results/code-retrieval-smoke-v2
```

离线模式用固定模拟回复，先读一个文件，再返回“证据不足”。报告会明确标记为离线，命中率为 0 是预期行为，**不能用来代表当前项目的检索水平**。

### 3. 接入新检索工具后测 B 组

当前工作只做评测系统，还没有实现 `search_code`。以后新增一个工厂函数，例如 `codeagent.tools.search_eval:build_tool`，按以下接口返回工具：

```python
def build_tool(*, workspace, index_dir, client, model, event_emitter):
    # 在 workspace 内检索；索引写入 index_dir。
    # 返回遵守现有 Tool 接口、definition.name 为 "search_code" 的对象。
    # 如果要调用语言模型，使用传入的 client / client.fork，让请求和用量纳入记录。
    ...
```

工厂不能读取题库、答案、评测输出或项目外源码。每题使用独立索引目录，测冷启动；初始化时间单列，延迟到第一次搜索才建索引的时间会计入查询耗时。若以后使用独立 embedding 服务，其调用用量需要额外接入统计；目前费用只覆盖已记录的模型调用。

```powershell
python -m evals.code_retrieval run --bundle eval-results/code-retrieval-v2 --variant enhanced --tool-factory codeagent.tools.search_eval:build_tool --mode live --split test --output eval-results/code-retrieval-after
python -m evals.code_retrieval grade --bundle eval-results/code-retrieval-v2 --run eval-results/code-retrieval-before --output eval-results/code-retrieval-before/report-current
python -m evals.code_retrieval compare --before eval-results/code-retrieval-before/report-current --after eval-results/code-retrieval-after/report --output eval-results/code-retrieval-comparison
```

工厂 `codeagent.tools.search_eval:build_tool` 已实现，与 CLI/Web 使用同一个 `SearchCodeTool`。模型与基线必须一致，例如本次两组都显式传入 `--model deepseek-v4-flash`。比较报告列出提升/退步的题目、命中率的百分点差、耗时和 token 差。已有同名输出目录时应复用证据或改用新的目录，不覆盖旧报告。

2026-09-29 评分规则修正：函数引用范围包括装饰器，冻结语料 ID、问题和原始报告保持原样。`result.json.version` 区分新旧规则，比较器拒绝混用。因此旧 A 必须离线重算后再与新 B 比较；修正规则产生的涨分不算检索提升。有复核标签时，两次 `grade` 都传入同一份 `--gold`，然后比较两个复核报告。

## 公平对比和判分边界

- A 使用冻结的现有 Agent 与项目自带的 `glob/grep/read_file`；B 使用改造后的 Agent 加 `search_code`。两组采用同一份只读评测配置：关闭记忆、技能、MCP、子智能体与主动压缩，保留当前任务的工具输出和上下文历史读取。评测不注册额外的 `rg` 或 `bash` 工具。这个配置衡量受控定位任务，不等同于所有生产交互。
- 语料是函数级片段，但 Agent 看完整的冻结 Python 文件。题目、qrels、答案及报告不放入可搜索目录，评测测试文件也被排除。源码哈希改变、模型/接口/主要依赖/预算不同，比较脚本会拒绝合并。
- 当前模型客户端没有 temperature 参数，双方沿用提供方默认采样；请求中的模型名和响应模型标识会记录。即使名称相同，远端服务也可能更新，尽量使用固定版本并在相近时间测试。
- 每题新进程、新 Agent 和新索引，固定打乱题目顺序。SDK 实际请求保留记录并共享 12 次请求上限；如果新工具绕过传入 client 自己发请求，这部分就不在现有统计内，必须补接记录后才能谈完整成本。
- 最多评分前 5 个最终位置。路径、精确函数名、行号和最多 20 行连续源码必须对应冻结语料，并包含所问行为的关键行。只报文件、只引用函数声明、伪造引用、重复函数均不能凑命中数。解释是否完整正确不由本脚本自动判断。
- 超时、失败、漏答计入原分母；无答案题中的“证据不足”单列，不当作正确拒答。候选结果来自真实函数但尚未被标注，会进入 `review.json`，当前先按未命中计算并将结果标为待复核。
- `run.json` 保存程序变化文件清单。如果 B 顺便更改提示词、上下文机制或原有工具，即使模型/预算一样，也只能说“组合改造后的提升”，不能把全部增益归给新检索工具。

## 复核与报告文件

### 按用户提供的空闲时段单价统计费用

价格来自本任务中用户提供的截图，固定使用以下人民币单价，不根据实际调用时间切换高峰价，不自动套用其他产品价格：

| 类别 | 每百万 token 单价 |
| --- | ---: |
| 缓存命中输入 | ¥0.02 |
| 缓存未命中输入 | ¥1 |
| 输出 | ¥4 |

计算公式：`费用（元）= (缓存命中输入 × 0.02 + 缓存未命中输入 × 1 + 输出 × 4) / 1,000,000`。

当前使用 Anthropic Messages 格式的用量：`cache_read_input_tokens` 是命中输入；`input_tokens` 与 `cache_creation_input_tokens` 相加作为本次估算的未命中输入；`output_tokens` 是输出。输入三项分别计数，不能再把缓存读取加到未命中里收费。字段拆分参考 [Messages 缓存用量说明](https://platform.claude.com/docs/zh-CN/build-with-claude/prompt-caching)；**缓存写入按未命中价处理是这次估算的约定，不套用该页面的 Claude 定价或写入倍数**。

报告的费用来自真实用量按上述单价换算，并非服务商账单。失败任务只要用量完整也会计费；有任务缺失用量时，整套总费用、平均费用与 A/B 费用差显示未知，同时展示用量完整任务的费用小计及覆盖数量。离线模式不伪造用量，费用显示未知。费用以 8 位小数展示，避免少量 token 被四舍五入成免费。

费用规则与来源写入 `result.json` 的 `pricing`，逐题金额在 `rows[].cost`，汇总金额在 `metrics.cost`，单位均为人民币元。不同价格规则的报告不能直接比较，应先用同一版规则重新统计。

新启动的评测会自动输出带费用的报告，现有 v2 快照无需重建。如果已跑完，或评测是在更新脚本之前启动的，用以下命令根据已有结果重新生成报告，**不会再次调用模型**：

```powershell
python -m evals.code_retrieval grade --bundle eval-results/code-retrieval-v2 --run eval-results/code-retrieval-before --output eval-results/code-retrieval-before/report-priced
```

新报告位于 `eval-results/code-retrieval-before/report-priced/report.md`。比较时，A/B 都使用重新统计后的报告目录；若已复核标签，重新统计时继续传入相同的 `--gold`。

### 保留的文件

每组运行保留：

- `run.json`：模型、配置、代码哈希、程序变化和运行状态。
- `predictions.jsonl`：逐题最终答案、失败状态、时间、用量。
- `trials/`：逐题模型请求/响应、事件、工具定义、标准错误输出；敏感内容按已有记录器处理。
- `report/report.md`：可阅读的汇总表、分类表、逐题结果。
- `report/result.json`：完整结构化统计。
- `report/review.json`：可能需要补充相关性判断的候选。

如果发现标准答案漏标了合理实现，复制冻结的 `gold.json`，在对应证据组的 `members` 增加语料里的 `doc_id` 与关键行 `evidence_lines`；确认无关的候选可以放入该题 `nonrelevant` 数组。不要改题目、分类或原始快照。对 A/B 都使用同一版复核标签重新判分：

```powershell
python -m evals.code_retrieval grade --bundle eval-results/code-retrieval-v2 --run eval-results/code-retrieval-before --gold eval-results/reviewed-gold.json --output eval-results/code-retrieval-before-reviewed
python -m evals.code_retrieval grade --bundle eval-results/code-retrieval-v2 --run eval-results/code-retrieval-after --gold eval-results/reviewed-gold.json --output eval-results/code-retrieval-after-reviewed
```

之后比较这两个新报告目录。标签不一致时，比较脚本会拒绝生成对比，避免只给改造后放宽答案。

评测器自身的测试只覆盖关键统计规则、题目源码对应关系和离线运行链路，不调用付费模型。`--mode live` 才是真实付费模型评测；本次检索改造的运行结果另见实施报告。

## 2026-10-02：增强版本的对照口径

历史 B2 在 20 题上最终严格 Hit@1/5 均为 15/16；最初无 search_code 的 A 也是 15/16。
B2 的工具调用中位数从 A 的 7.5 降到 6，输入 token 中位数从 54393 降到 31554.5，
耗时中位数从 11.8905 秒降到 8.7265 秒。历史固定单价下整套估算费用从 0.45841844 元降到
0.38509016 元。这是历史单次结果，不能用来证明本次 embedding/LSP 改造带来的收益；
第二轮小样本重复也没有证明准确率稳定提高。详见 `code-search-round2-report-2026-09-29.md`。

新增无网络的工具级回放：

```powershell
python -m evals.code_retrieval.replay --bundle eval-results/code-retrieval-v2 --history eval-results/code-search-round2 --output eval-results/your-new-replay-directory --before-ref 2576bd7
```

输出目录必须是新目录，不覆盖旧证据。脚本验证题库封印，复制冻结目标；两个独立进程使用相同的当前依赖，
仅将 before 的 chunks/index/service 换成指定 Git 提交版本。保存全部引擎文件哈希、目标哈希、查询、原始结果及评分。
每次调用使用独立冷索引。它刻意不构造模型客户端，不读取 embedding 配置、不调用远程服务。
历史真实改写按原文重放；错误的历史参数保留为失败，不擅自修正。

三组分别报告，禁止混合分母：

- 原始正式 20 题，16 道有答案：不生成新改写，检查离线词法退化下的能力。
- B2 曾发出的 8 次 search_code 调用：7 次对应有答案问题，来自 6 道不同问题，其中一次参数错误；这是有选择的历史调用样本。
- 已知 C01 失败查询及保存的改写：单题诊断，不能当作新的盲测题。

`identity_hit*` 只检查函数身份；`hit*` 复用原严格证据评分。
`candidate_hit40` 检查融合后候选列表，不代表融合前所有召回路的并集。
`source_valid/returned` 另行逐字检查 quote、首末行及内容哈希，涵盖相关与无关结果。
候选返回不能视为 Agent 断言“存在实现”，所以此回放不报告无答案误报率。
旧题库未标注但源码有效的候选仍列为 `unjudged`，成绩只相对于冻结标签。
单次冷查询的 duration 仅留作排查，不作为速度优劣结论。

本轮回放发现并修正了两处问题：

- 超长结果截断时 `splitlines()` 丢掉末尾空行，造成 quote 与 end_line 不一致。改为保留空行边界的拆分；新增含多处空行的大输出回归测试。
- 单文件配额在融合候选阶段就丢弃函数。v3 回放中 N05 的目标由候选第 7 位变为不存在，虽然前五命中不变。配额移到最终结果选择阶段，候选池保留原始排名；新增同文件多函数的候选保留测试。没有针对 N05 或 C01 调整检索权重。

失败的首次快照尝试及修复前 v2/v3 结果均保留在 `eval-results/code-search-enhancement-20261002-replay*`，
最终版本使用新目录 `eval-results/code-search-enhancement-20261002-replay-v4`，不能把修复前后的产物混用。

最终 v4 两组各 29 次调用尝试（含一次历史无效参数），零模型/embedding 调用：

| 指标 | 增强前 | 增强后（关闭 embedding） |
| --- | ---: | ---: |
| 原始题精确符号 Hit@1 | 4/4 | 4/4 |
| 原始题有答案的严格工具 Hit@5（无新改写） | 5/16 | 5/16 |
| 原始题有答案的融合候选 Hit@40 | 6/16 | 6/16 |
| 历史有答案调用的严格工具 Hit@5 | 4/7 | 4/7 |
| 所有返回片段的 quote/首末行/哈希完全一致 | 155/158 | 158/158 |

C01 已知查询的 ToolRegistry.execute 仍排第 10，前五仍未命中。严格评分未出现逐题胜负变化。
因此本轮证明的是词法回归持平和本批片段可靠性修复，尚未证明语义检索或 Agent 最终质量提升。
上表的工具级 5/16 不能与历史 Agent 最终答案的 15/16 相减：后者允许改写和多轮 grep/read_file 补证。
158 个片段来自三组相关回放，不能当成 158 道独立题，也不能推断全项目永远无引用问题。

验证记录：相关 49 项测试中 48 项通过，1 项真实 LSP 测试因未显式启用而跳过；
完成配额与空行修复后，`tests.test_code_retrieval_replay`、`tests.test_code_search`、
`tests.test_code_intelligence` 共 30 项通过。真实 embedding 未运行，本轮未重新运行真实语言服务器。

### 后续真实质量实验的六项量化维度

| 维度 | 主指标与分母 | 能回答的问题 |
| --- | --- | --- |
| 找得到、排得前 | 有答案题的函数 Hit@1/5、MRR@5；融合前候选 Recall@K；融合后必需证据组覆盖率、跨文件全部找全率 | 丢在召回、排序还是结果配额？ |
| 能用于回答 | 最终严格 Hit@1/5；身份命中与严格命中的差；首次收齐证据后的额外模型/工具调用；完整任务成功率 | 工具找到后，Agent 是否正确引用并及时结束？ |
| 语义增益 | 自然语言、无标识符、中文描述分别统计；只被向量召回的相关函数数；新词法与混合检索逐题胜/负/平 | 向量是否带来已有词法之外的有效信息？ |
| 时间与成本 | 冷建索引、热查询、增量更新的 p50/p95；模型与 embedding 分项用量；按预设查询数摊销的索引成本；总费用/严格成功数 | 提升是否值得延迟与费用，重复使用能否摊薄成本？ |
| 可信与稳定 | quote/路径/首末行/哈希一致率；增删改后陈旧结果数；忽略及越界泄漏数；降级完成率、取消终止时间、隔离与并发测试 | 是否可靠地返回当前工作区证据？ |
| 编辑反馈 | 独立编辑任务的诊断进入下一次模型请求的比例和延迟；修复轮数、最终测试通过率；进程释放 | LSP 是否真正改善编辑闭环？ |

主比较设为：A=本次增强前的现有检索，B=新代码且关闭 embedding，C=新代码且显式启用真实 embedding。
最初完全没有 search_code 的版本只做历史背景，避免把“从无到有”的收益算给新改造。
LSP 在另一套允许编辑的任务上对照启用/关闭，按语言服务器可用/不可用分组；不能混入旧只读定位题集。

旧 20 题继续做回归，但已经被多次查看，不作为新的留出集。新增至少 30 道未用于调参的题，
在观察新旧结果前冻结标签，覆盖无标识符自然语言、多处相似实现、跨文件证据和无答案；开发题与留出题分开。
同一题各组至少重复 3 次，交错运行 A/B/C，共用目标快照、模型、依赖、预算、输出长度和评分器。
保留失败、超时及全部重复，按题配对报告胜/负/平和区间；重复运行不能冒充独立新题。
相关题来自同一实现时按实现分组统计不确定性。16 道正例中一题就是 6.25 个百分点，不能把一题变化宣传成稳定提升。

验收先守住精确符号、跨文件完整性及无答案误报的回归底线；源代码一致性和工作区越界要求零违规。
语义效果必须由真实 provider 的留出集结果证明，报告质量、延迟和成本的取舍，不预先承诺提升百分比。
模拟 provider 只用于缓存、失败、取消和融合机制验证，不计入语义成绩。

后续接线更新：`codeagent.tools.search_eval:build_tool` 仍固定只测词法；新入口
`codeagent.tools.search_eval:build_hybrid_tool` 读取显式 embedding 配置，runner 已传递相关环境设置。
旧费用记录器仍未收集 embedding 用量，不能据此报告混合检索的总成本。
开启 C 组前还需记录脱敏的 provider/model/维度、缓存状态和 embedding 用量，
并逐次确认 `retrieval_backend/vector_status/scan_complete`，避免把失败降级当成混合成功。
故障降级另报，不从 C 组主分母剔除。旧 worker 未接入 LSP，编辑实验也需独立接线。

## 多语言重构执行与量化记录（2026-10-02）

以下记录属于本轮多语言子系统重构，不能与上面的早期向量接入 v4 混为同一次实验。
实现说明见 [code-search.md](code-search.md)。生产入口保持 `search_code`；Python 保留原生 AST，
Java、TS/TSX、JS/JSX 使用独立 Tree-sitter grammar，其他已注册源码/配置类型明确采用文本降级。

### 冻结旧题的工具回放

产物：`eval-results/polyglot-20261002-lexical-final/report.json`。原始命令：

```powershell
python -m evals.code_retrieval.replay --bundle eval-results/code-retrieval-v2 --history eval-results/code-search-round2 --output eval-results/polyglot-20261002-lexical-final --before-ref 2576bd7
```

该目录已存在，不要覆盖；再次实验应指定新目录。A 是提交 `2576bd7` 中的旧实现，
B 是此轮多语言检索实现的冻结快照且关闭 embedding；A 不是本轮开始时含未提交改动的完整工作区。
各组29次调用尝试：20道原始题、8次历史工具调用、1次已知失败诊断。
保留历史无效参数调用（每组1次错误）；全过程没有模型改写或 embedding 请求。
最终快照之后补充了评测基线入口、缓存损坏恢复、相同版本并发发布、异常编码跳过和 LSP 生命周期修复，未再改相关性排序。

| 指标 | A：旧实现 | B：多语言词法 |
| --- | ---: | ---: |
| 原始精确符号 Hit@1 | 4/4 | 4/4 |
| 原始有答案题严格 Hit@5 | 5/16 | 5/16 |
| 原始有答案题候选 Hit@40 | 6/16 | 6/16 |
| 历史有答案调用严格 Hit@5 | 4/7 | 4/7 |
| 历史有答案调用身份 Hit@5（不要求证据行） | 5/7 | 4/7 |
| 历史有答案调用候选 Hit@40 | 6/7 | 6/7 |
| 全部返回片段的源码/行号/哈希一致 | 155/158 | 142/142 |

结论：严格命中和精确符号底线持平，本批源码一致性问题已修复；不能声称所有指标无回归。
历史 N02 调用的 `_redact` 在新候选池排第8，身份 Hit@5 出现下降，旧返回也没有满足该题严格证据。
C01 仍未进入前五，但两组候选40内均有目标。新输出优先保留前五条证据、减少超过预算的尾部结果，
所以返回片段数量不同；142/142不是独立留出题集，也不证明任何项目永久不会有引用错误。
历史 Agent 的15/16允许多轮搜索、改写和读文件，不能与这里的单次工具5/16直接比较。
初次回放暴露的证据截短、普通字段误当精确符号等问题已按通用规则修复；旧失败产物及 v2/v3 均保留。

### 同一多语言语料上的下一轮对照入口

新增 `prepare --annotations`，接受人工独立标注，不用被测解析器自动生成正确答案。
标注文件格式示例（路径和行号必须改成目标快照里的真实位置）：

```json
{
  "annotation_status": "reviewed_before_running",
  "documents": [
    {"id":"refund-string","path":"src/Order.java","symbol":"Order.refund","start_line":12,"end_line":20}
  ],
  "cases": [
    {"id":"J01","query":"按订单号退款的入口在哪里","split":"test","category":"natural_language",
     "groups":[{"id":"implementation","members":[{"doc_id":"refund-string","evidence_lines":[15]}]}]}
  ]
}
```

重载用不同文档 id 和定义范围；同一行有多个同名声明时可补充精确 `signature`。
冻结时检查忽略、工作区边界、源码跨度、证据锚点，保留目标与引擎哈希：

```powershell
python -m evals.code_retrieval prepare --source D:\ademo\AgentDemo --annotations D:\labels\agentdemo.json --output eval-results/agentdemo-frozen-v1
```

用同一个冻结 bundle、同一个增强引擎和相同预算选择三种 `--tool-factory`：

| 组别 | 工厂 | 比较内容 |
| --- | --- | --- |
| A | `codeagent.tools.search_eval:build_window_tool` | 相同文件盘点、文本窗口、词法检索 |
| B | `codeagent.tools.search_eval:build_tool` | 多语言语法实体、符号与词法检索 |
| C | `codeagent.tools.search_eval:build_hybrid_tool` | B 加显式配置的真实 embedding |

三组均使用 `--variant enhanced`；不要用 runner 的旧 `baseline`（无 search_code）代替这里的 A。
`run` 默认 `--mode offline`，只检查配置和冻结产物，不产生质量分数；`--mode live` 才调用聊天模型，
C 还会调用已配置 embedding。每组使用独立输出与缓存目录，并分开测冷建、热查询和增量更新。
本轮只验证这些入口和评分器，没有伪造30道新留出题或真实语义提升百分比。

`code_search.completed` 事件现在保留逐次 `embedding_usage`：eligible/ready/pending、embedded、
cache_hits、requests、failed_requests、input_tokens、usage_complete；逐题记录汇总请求、用量、缓存复用和构建数量。
cache_hits 是查询构建前已存在的块数，不能直接充当请求成功率。缓存命中、向量覆盖与聊天前缀缓存是不同指标。
provider 不提供 usage 或调用部分失败时，`usage_complete=false`，请求/用量也可能只是不完整小计。
现有费用列标记 `cost_scope=chat_only_embedding_price_not_configured`，未配置 embedding 单价，不能将它报成端到端总费用。
质量仍按前述六个维度报告，明确向量完成/部分覆盖/降级，失败保留在主分母；生产编辑反馈已接通，
但只读 retrieval worker 不承担 LSP 编辑质量实验。

### 实际项目、协议与生命周期验证

- AgentDemo 最终离线接通记录：`eval-results/polyglot-20261002-agentdemo-smoke/final-runtime.json`。
  159个文件、3312个块，扫描完整，1个文件解析降级；`Runtime.execute` 第一名，路径
  `server/core/runtime.ts`、摘录541–560行。目标仓库未修改，没有调用远程服务；这不是相关性留出集成绩。
- 模拟测试涵盖多语言符号/重载、精确及自然语言查询、离线回退、增删改、插行复用、模型切换、
  损坏向量恢复、超过2000块的续建、租约并发、边界/忽略/源码一致性、CLI/Web/子 Agent/worktree、
  LSP 协议/慢启动/超时/取消/动态能力以及编辑诊断进入下一次模型请求。
- 真实集成独立执行：本机 pylsp；typescript-language-server 6.0.1 + TypeScript 5.9.3；
  已安装 Red Hat Java 1.56.0 扩展内的 jdtls/Java21。三种语言均通过定义、引用、修改后诊断及释放检查。
  Java 未修改系统 Java8，也未运行 Maven/Gradle 多模块项目。
- TypeScript 一次复测发现父进程退出后目录仍短暂占用。修复为 Windows Job Object 管理整个树，
  退出前保留子进程句柄并等待实际终止；修复后三种真实服务器共3项通过，TS退出场景另连续3次通过。
- 一轮全量977项出现1项并发失败：相同文件/解析器版本已由另一实例发布时被误报为扫描不完整。
  已修复版本比较，新增控制交错顺序的测试；该用例及双实例向量租约测试共重复24次通过。
  此轮失败不记为全量通过，修复后重新运行整个套件。
- 另以两个真实 Python worker 进程、同步启动屏障和模拟 provider 验证共享缓存：
  同一代码块只嵌入1次；一组返回 completed/embedded=1，另一组明确返回 partial/embedded=0，
  两组源码扫描均完整。未将另一进程尚未完成的向量误报为全覆盖。
- 修复后完整重跑：979项，975通过、4项按条件跳过，359.462秒；50项定向检索/LSP测试通过。
  最后的拉取诊断标记调整另经17项测试通过：`requested_after_sync` 不再误写为服务器提供版本证明。
  真实服务器验证独立于默认跳过项；本轮未使用真实 embedding 或付费聊天 API。

仍待独立验收：未调参多语言留出集上的真实 B/C 语义收益、端到端总费用、大型 Java 多模块工程、
向量 ANN/大型仓库性能、跨 run LSP 池化以及严格带版本的诊断保证。以上没有算作已经完成。

## 2026-10-03 真实评测预注册方案

本节在观察本轮检索结果前确定；不修改生产排序权重，失败保留在分母，原始响应全部保留。
研究依据：[CodeSearchNet](https://github.com/github/CodeSearchNet) 的语言隔离和源码定位，
[CoIR](https://github.com/CoIR-team/coir) 的多任务/多域比较，以及
[BEIR](https://github.com/beir-cellar/beir) 的 qrels、MRR、nDCG、Recall 与保存 runfile 的方法。
没有直接复制论文分数，也不宣称下面的小样本等价于其官方完整测试。

### 数据及污染控制

1. 公开集：从 [原始 CodeSearchNet 镜像](https://huggingface.co/datasets/code-search-net/code_search_net)
   的 Python/Java/JavaScript **test** 各固定取偏移17/317/617处100行。
   预先按重复代码、描述长度25–1000、源码长度30–12000及可完成去注释处理筛选；保留排除原因。
   得到268/224/251个函数，共743个候选；每语言固定种子20261003抽20道描述查询，共60题。
   去掉代码注释及 Python docstring，保留字符串字面量；Java 函数外加中性 Sample 类。
   标注来自原始描述/函数配对，这是 proxy label，不是99个人工自然查询评测。
   CoIR 当前 CodeSearchNet 的 corpus 是注释、query 是代码，直接运行会测反方向，因此不直接套用。
2. 仓库集：Click（提交06b2a678）、Spring Petclinic（500158f7）、AgentDemo 的 server/shared 内容快照，
   各14题：2精确符号、4中文、2英文行为、2跨文件、2相似名字/重载、2无答案，共42题。
   标签由助手先阅读源码、标出函数范围和实际行为证据行，不由被测检索器生成，不是独立人工双审。
   此范围不含 Click/Petclinic 测试、文档或 AgentDemo 前端，不把范围外的实现当成错误。
3. 来源、原始下载、过滤、行号、证据、候选库和引擎 SHA256 全部冻结。
   不下载密钥、不执行上游仓库脚本；所有副本在独立结果目录内，Git 初始化防止继承父项目的忽略目录。
   老20题只作历史回归背景，不计为本轮新题。本轮看过结果后，题库变成回归集，不能再称未见留出集。

### 对照和指标

- A：同一文件集合的文本窗口+词法；B：多语言语法实体+精确符号+BM25；C：B+真实 Qwen embedding。
  不是拿“完全没有搜索工具”充当 A。先比较 A/B 的语法效果，再比较 B/C 的向量效果。
- 每题三组各3次，共918次工具查询。组序交错，参数、top_k=5、输出预算固定，关闭模型改写。
  C 预先完整构建向量，冷构建和热查询分开计时；构建不完整及查询降级必须单列，不能装作完整向量。
- 分别报告身份 Hit@1/5、MRR@5、nDCG@5、证据严格 Hit@5、跨文件全部覆盖、候选 Recall@80、
  只被向量召回的目标、路径/行号/quote/哈希一致率、重复率、p50/p95、逐题胜负平。
  同一题重复不扩充独立样本数；区间按公开样本上游仓库、私有样本相关文件集合做配对聚类 bootstrap。
- 无答案题中“工具返回候选”不等于 Agent 误报存在。单独记录 nonempty 与 absence_proven；
  Agent 的正确拒答率需要另一层真实模型实验，不能给工具空结果编造不存在证明。
- 真实 provider 单独记录每次 HTTP 子批次、实际输入 token、耗时、错误、冷构建与查询用量。
  本轮预设不超过800个 embedding HTTP 请求、600万输入字符；预算终止不得当成普通降级。
  1次接口探测单独记账。费用不能使用旧聊天模型单价代替；标准价估算与账户实际账单分开。

### 工程及 Agent 验收

除918次定位外，继续验证新增/修改/删除、插行复用、模型切换、损坏缓存、忽略/边界、两工作区、
向量故障离线回退、预算取消、并发索引及资源释放。真实 pylsp、TypeScript language server 和 jdtls
分别测试定义、引用、修改后诊断；模拟故障专门测试超时和缺失，不能用它代替真实服务器。
有限规模真实 Agent 实验另记录模型可见诊断、最终答案/修复、模型调用/输入/缓存命中，不与检索器成绩混合。
运行结果、命令、真实与模拟范围、未完成场景最终追加在下面。

## 2026-10-03 真实执行结果

主证据目录：`eval-results/retrieval-real-20261003/`。汇总是 `assessment.json`，
检索逐条结果为 `run-v1/queries.jsonl`，主实验汇总为 `run-v1/report.json`。
生产代码与开始时的 `frozen-engine/codeagent` 逐文件一致；没有针对测试答案调排序或改生产代码。
所有既有未提交改动保留，`.env` 未修改。这里测试的是当前增强实现；A 是同引擎的文本窗口消融，
不能把 A 的成绩当成历史版本整个 Agent 的成绩。

### 检索质量：918/918 次完成

102 个问题 × A/B/C × 3 次，独立问题数仍为102。60道公开正例、36道仓库正例和6道无答案题分开统计。
每组每题3次的质量指标一致，没有用重复次数放大统计样本。

下表为 Top-5 定位命中率。定位按正确路径与目标源码范围重叠计分，**不是编译器符号绑定正确率**。

| 数据 | 正例题数 | A 文本窗口词法 | B 语法实体词法 | C 语法实体+真实向量 |
| --- | ---: | ---: | ---: | ---: |
| CodeSearchNet Python 子集 | 20 | 80.0% | 85.0% | 95.0% |
| CodeSearchNet Java 子集 | 20 | 85.0% | 95.0% | 95.0% |
| CodeSearchNet JavaScript 子集 | 20 | 80.0% | 85.0% | 95.0% |
| 公开子集合计 | 60 | 81.7% | 88.3% | 95.0% |
| Click 源码范围 | 12 | 41.7% | 41.7% | 100.0% |
| Petclinic 源码范围 | 12 | 33.3% | 50.0% | 100.0% |
| AgentDemo server/shared | 12 | 33.3% | 58.3% | 100.0% |
| 仓库自建题合计 | 36 | 36.1% | 50.0% | 100.0% |

| 指标 | 公开 B → C | 仓库 B → C |
| --- | ---: | ---: |
| Hit@1 | 53.3% → 58.3% | 30.6% → 77.8% |
| MRR@5 | 0.6692 → 0.7367 | 0.3736 → 0.8634 |
| nDCG@5 | 0.7228 → 0.7908 | 0.4050 → 0.8798 |
| 严格证据 Hit@5 | 88.3% → 95.0% | 44.4% → 86.1% |
| 全部目标定位覆盖 | 88.3% → 95.0% | 50.0% → 97.2% |
| 全部目标严格证据覆盖 | 88.3% → 95.0% | 44.4% → 77.8% |
| 候选 Recall@80 | 98.3% → 98.3% | 52.8% → 100.0% |

严格证据要求摘录与当前源码一致，且覆盖预先冻结的关键行之一。公开配对数据没有人工行为锚点，
因此其“严格”指标仅额外检查源码真实性，不能与仓库题的行为证据难度等同。
17个问题的目标只被向量通道召回、未被该次词法/精确通道召回。
中文12题原始检索 Hit@5 为 B 1/12、C 12/12，C 严格证据9/12；关闭改写使此处专门反映检索器差异。
精确符号6题 B/C 都为6/6，C 的18次精确查询跳过了远程向量。
跨文件6题 C 都找到至少一个目标，但只有5/6覆盖全部目标、3/6覆盖全部关键证据。

配对结果：公开 B→C 的 Hit@5 为4胜/0负/56平，增加6.67个百分点，按原始仓库聚类的95%区间约
[0, 17.39]个百分点，**区间包含零，不能宣称已证明普遍显著提升**。仓库题为18胜/0负/18平，
增加50个百分点，近似聚类区间[34.04, 70.37]个百分点。自建题只来自3个项目、由助手标注，
相关性聚类按相同目标文件集合实现，不是严格的项目外推或所有重叠目标连通分组；区间不能替代独立复核。

所有3915条返回片段均通过路径边界、源码哈希、行号和逐字摘录校验；918次扫描完整、没有 source_changed，
Top-5 内未发现同一实体重复。6道无答案题中 C 均返回候选，但 `absence_proven=false`；这不是不存在判定。

### 速度、构建和费用

| 范围 | B p50 / p95 | C p50 / p95 |
| --- | ---: | ---: |
| 公开题热查询 | 328 / 375 ms | 2016 / 2344 ms |
| 仓库题热查询 | 109 / 156 ms | 2375 / 3141 ms |
| 精确符号热查询 | 109 / 250 ms | 110 / 187 ms |

冷构建与上述热查询分开。六个范围共5157个向量块全部就绪，pending=0：
Python公开集37.8秒、Java53.6秒、JavaScript45.6秒、Click161.8秒、Petclinic22.6秒、AgentDemo157.5秒。
这是本机、本次网络、这些源码范围的结果；未測全仓库冷启动交互、百万块索引或并发远程吞吐。
普通搜索仍要做输入向量化和本地扫描，文档向量缓存命中不意味着查询零网络成本。

| 真实 embedding 阶段 | HTTP 请求 | 输入 token |
| --- | ---: | ---: |
| 主实验冷构建 | 325 | 575703 |
| 主实验热查询 | 288 | 19929 |
| Agent 检索 | 9 | 233 |
| 增量/缓存验收 | 11 | 437 |
| 独立接口探测 | 1 | 20 |
| 合计 | 634 | 596322 |

634次请求成功；800请求/600万字符预算未触发。使用真实 `qwen3.7-text-embedding`、1024维，
未调用其他模型替代。模型切换验收是**同一个真实模型的不同缓存身份**，只证明命名空间失效隔离；
多模型/维度变更还有模拟测试，不声称比较了两个真实模型的语义质量。

聊天模型实际成功响应77次，返回的 model 字段为 `deepseek-v4-flash`，另有7次夹具错误的 HTTP 400。
已知 usage：未缓存输入94166、缓存输入320640、输出13791，成功响应的输入缓存占比77.3%。
这是聊天前缀缓存，与代码块向量缓存、向量覆盖率分开。无法仅凭账单总 token 或柱状图判断检索缓存是否有效。

按已核实的北京区同步 embedding 标准价0.5元/百万输入 token，embedding 约0.2982元；
按当前周末低峰 DeepSeek 对应模型别名标准价估算，成功聊天请求约0.1557元；已知用量合计约0.4539元。
这是**标准价估算，不是账户实际账单**，未计免费额度/账户折扣；7次400没有返回 usage，不能假装全量计费信息完整。
单价、日期、来源及适用范围保存在 `pricing-evidence.json`。依据：
[阿里云同步 embedding 定价](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)、
[DeepSeek 定价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。
DeepSeek 的 Anthropic 兼容接口忽略 cache_control 并使用自动缓存，不能用是否显式打标直接判断命中。
参见[官方兼容说明](https://api-docs.deepseek.com/guides/anthropic_api/)。

### 真实 Agent、编辑反馈与 LSP

只读 Agent 用 P03/P13、J03/J13、T06/T13，B/C 各6次：均为3/3正例严格成功、3/3正确拒答，无误报。
B 用22次模型请求、31次工具调用、平均6.53秒；C 用24次模型请求、30次工具调用、平均9.47秒。
Agent 允许正常查询改写、grep/read补查，所以 B 能补上直接中文词法检索的不足。
**本次没有证明端到端准确率提高或总费用降低**；工具层收益不能直接换算成 Agent 收益。
只读实验的成功响应输入缓存占比77.5%，编辑实验76.7%。这两组均仅一次试验，不作显著性结论。

编辑实验是 Python/TypeScript/Java × LSP关闭/冷启动/预热，共9次。
第一步由可辨认的夹具通过真实 edit_file 注入语法错误，后续读文件/修复决策才由真实模型生成。
9/9修复通过：Python编译+行为断言、TypeScript tsc+Node断言、Java javac+运行断言。
没有把 TypeScript 编译器用于 Java/Python。三种语言的 warm 组均在下一次真实模型请求中看到 severity=1 错误；
检查的是保存的模型请求，不只是 hook 注册或 UI 提示。

| 编辑反馈场景 | 实际模型可见内容 | 验收结论 |
| --- | --- | --- |
| LSP关闭，三种语言 | 无诊断附加内容 | 3/3仍可读文件修复，作为对照 |
| Python冷启动 | initializing，后续ok；未捕获本次错误诊断 | 首次反馈不能保证即时就绪 |
| TypeScript冷启动 | ok，真实错误及修复后诊断 | 完整闭环通过 |
| Java冷启动 | initializing/timeout；未捕获本次错误诊断 | 反馈预算内仍可能无诊断 |
| 三种语言预热 | 错误诊断进入后续请求，修复后ok | 完整闭环通过 |

所有启动的编辑服务器进程均回收，读写线程结束。任务过于简单、对照组也全成功，所以这里证明接线和生命周期，
不能据此宣称 LSP 提高修复成功率。尚需更难的类型/跨文件错误和多次随机试验。

真实项目导航：Click、AgentDemo，以及自行构造的 Java Maven 双模块项目均通过定义/引用及释放。
Petclinic **默认配置失败**：jdtls 自动选择 Gradle，日志显示 Gradle构建模型同步失败、wrapper校验文件下载失败，
然后出现 timeout/initializing/空结果；并非源码里不存在目标。虽然未显式执行上游 wrapper 命令，
jdtls 自动导入确实触发了构建依赖同步，不能把这段过程描述成纯静态读取。
另建副本，按照 [jdtls 初始化设置](https://github.com/eclipse-jdtls/eclipse.jdt.ls/wiki/Running-the-JAVA-LS-server-from-the-command-line)
显式禁用Gradle导入、启用Maven，并在 initializationOptions.settings 传入配置，定义/引用通过。
这轮是 `petclinic-maven-v2` 诊断性复核，**不覆盖默认失败，不表示默认导入配置已修复**。
原失败日志保存在 `real-project-lsp-v1/petclinic-import-log-excerpt.txt`。

### 工程回归和失败审计

真实 provider 小型验收14项通过：冷建、缓存复用、插行复用、正文更新、新增、删除、片段真实性、
模型身份失效、离线故障回退、忽略、边界、工作区隔离、取消、预算。
离线故障使用故意抛错的 provider，控制流使用显式取消/预算耗尽；没有谎称遇到了真实网络宕机。

定向测试94项全部通过，无跳过。覆盖检索/多语言实体/符号重载、缓存损坏、2000块以上推进、双实例及双进程索引，
LSP模拟协议/取消/超时/启动失败/子进程树释放、CLI/Web注册、普通/子Agent与Team/worktree绑定及诊断上下文。
本次新增7项评测器测试，检查评分分母、源码真实性、重复收益、注释去除、夹具模式及usage统计。
本次未重跑整个历史979项套件；94项是当前验证，不借用上轮全量结果充数。
另外独立启用真实服务器复测3项，3通过、0跳过，14.0秒：pylsp、TypeScript language server、jdtls
均执行定义、引用、修改后诊断及进程退出。日志为 `real-lsp-tests.log`，机器摘要为 `real-lsp-tests.json`。

未隐藏的失败：

- 编辑 `agent-edits-v1` 前7次被接口以400拒绝：脚本生成的首个工具调用缺少思考模式历史块；
  随后打印编译错误时还遇到Windows GBK输出编码错误。原记录全部保留。
  v2对**所有编辑组**显式关闭thinking、控制台使用UTF-8，保持题目、预算和验证器不变后重新运行9次。
  这是夹具修正，不是生产检索调参；只读Agent实验保持原来的默认思考模式。
- 第一轮94项中，新评测器测试错误地假设日志会保留 `thinking` 配置字段，触发1个错误；
  现有脱敏器会过滤所有同名字段。修正为检查真正送给SDK的参数，随后94项完整重跑通过；
  原 `focused-tests.log/json` 与 `focused-tests-v2.log/json` 均保留。
- Java默认Petclinic导入失败如上，既没有从报告剔除，也没有归咎于检索排序。

### 目前暴露的缺口与后续验收门槛

1. **证据选择仍需改进。** P03找对函数却只摘到函数前部，T02没覆盖预设路径校验行；
   J03摘到函数末尾的数据库异常处理，未覆盖冻结的前置重复名检查行。
   J03后半段也有拒绝重名的有效逻辑，提示人工锚点可能漏掉等价证据；本轮不事后放宽标签美化分数。
   下一版应支持关键行周围摘录/多个证据窗口，并由独立人员审查可替代证据。
2. **跨文件组合有遗漏。** T10原子写入+恢复校验只返回一个必要函数；候选池有另一目标，Top‑5融合/配额未保留。
   改进应在新的留出集上衡量全部目标覆盖，不能只追求至少命中一个函数。
3. **不是每个查询都改善。** 公开题 public-java-349 的MRR从1/2降到1/3，public-javascript-61从1降到1/2。
   Python公开子集 A→B 的Hit@1也下降；保留纯词法基线用于持续观察，不能只展示汇总提升。
4. **公开标签存在噪声。** public-javascript-18 的描述实为 eslint 配置注释，无法提供正常功能语义。
   它仍保留在60题主分母。公共源码可能存在模型预训练污染；未独立双审、不代表官方CodeSearchNet/CoIR成绩。
5. **冷启动诊断与Java项目导入是实际短板。** 需要明确初始配置、导入状态、可解释失败与后续补送机制，
   不能延长每次编辑等待来掩盖问题。默认设置的可用性应与手动配置后成功率分别报告。
6. **规模和外推不足。** 当前最大真实范围1662向量块；未验证百万块、ANN、远程限流下并发、长时间运行泄漏，
   也未做真实Go/Rust/C#语法及语言服务器集成。文本兜底测试不能算这些语言的结构化能力通过。
7. **进一步质量结论要新样本。** 本轮结果已看过，今后只能当回归集。
   下一轮至少新增30道独立标注、项目隔离且不调参的留出题；按语言/中文意图/无答案/跨文件分层，
   比较严格全部证据覆盖、误报率、p95与每个成功任务成本，并用真实Agent多次试验确认收益。

### 复现入口

本轮新代码位于 `evals/code_retrieval/benchmark_sources.py`、`field_cases.py`、`field_benchmark.py`、
`field_agent.py`、`field_integrity.py`、`field_lsp.py`、`field_report.py`；评分测试在 `tests/test_field_benchmark.py`。
题库及全部行号/哈希见 `suite.json`。采集来源、提交和过滤详情见 `public-sources.json`、`repository-sources.json`
及 `public/` 内的 metadata/exclusions；Agent问题和预算另外冻结于 `agent-plan.json`。

```powershell
# 只重算已有证据，无网络、无付费调用
python -m evals.code_retrieval.field_report --base eval-results/retrieval-real-20261003
python -m unittest tests.test_field_benchmark -v

# 以下为已执行的真实实验命令；输出目录必须是新的，避免覆盖证据
python -m evals.code_retrieval.field_benchmark run --base <已冻结的新评测目录>
python -m evals.code_retrieval.field_agent retrieval --base <已冻结的新评测目录>
python -m evals.code_retrieval.field_agent edits --base <已冻结的新评测目录>
python -m evals.code_retrieval.field_integrity --base <已冻结的新评测目录>
python -m evals.code_retrieval.field_lsp --base <已冻结的新评测目录>
python -m evals.code_retrieval.field_lsp --base <已冻结的新评测目录> --maven-followup
$env:CODEAGENT_TEST_REAL_LSP='1'
python -m unittest tests.test_lsp_integration -v
```

`field_report`只汇总已完成证据；不要在后续实验追加provider ledger后覆盖最初主实验的 `run-v1/report.json`，
否则会把后续Agent/增量调用混进主实验费用。最终 `assessment.json` 按阶段单独汇总所有已知用量。
