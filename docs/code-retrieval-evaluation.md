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
