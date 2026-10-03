# 按需求找代码

`search_code` 用自然语言或符号名搜索工作区中的 Python、Java、TS/TSX、JS/JSX 实现及已注册语言的文本。CLI、Web、只读讨论、子 Agent 和 Team 均提供该工具；服务绑定实际 workspace/worktree。已有 `glob`、`grep`、`read_file` 用法不变，不需要安装 rg 或数据库服务。

目标是在正确找到代码的前提下，减少模型请求、工具往返和输入 token。已知文件或精确位置时可以直接读取；需要从行为描述定位实现时再使用 `search_code`。工具保持可选，不为提高使用率强制调用。

例如：

```json
{"query":"查找清理等待任务的实现","keywords":["cancel pending cleanup"],"path":"codeagent","top_k":5}
```

`keywords` 可省略。没有精确符号命中时，中文需求缺少英文标识符或词法检索无结果，会尝试一次现有模型改写，最多生成三组关键词。Agent 已提供关键词就不再改写。所有改写共享原来的模型请求、token、时间与费用统计，不增加独立凭据；改写最多 768 输出 token、15 秒，不重试，失败会退回原查询。预算不足时优先保留 Agent 最后一次判断机会。

第二轮修正了中英混合需求的判断：普通英文单词不自动视作明确代码定位符；snake_case、camelCase、限定名、路径、带编号标识符或引用字面量仍作为定位线索。读取工具也提示核对最终引用首行和用户要求的行数上限。重复验证仍出现引用错误，不能保证模型始终正确引用，见 [第二轮完整结果](code-search-round2-report-2026-09-29.md)。

返回内容包括函数名、相对路径、定义行、摘录首尾行、原文、内容哈希和匹配字段。每个片段最多 20 行，整体最多约 8,000 字符，默认五个不同代码实体。`truncated` 表示还有候选或内容未返回，可继续读取源码。结果相关性仍需 Agent 判断；空结果不等于证明功能不存在。

实现采用共享语言分析层：Python 原生 AST；Java、JavaScript、TypeScript/TSX 使用各自 Tree-sitter grammar。类、方法、重载、构造器、接口、类型和字段有独立实体身份；不把语法身份冒充编译器解析结果。XML/SQL/JSON/YAML 等，以及尚未注册 grammar 的源码使用明确标记的文本块。依赖缺失或语法错误不阻断词法检索。

SQLite FTS5/BM25、精确符号与可选向量通过 RRF 融合。每路候选最多80，融合最多80；文件配额仅在最终输出阶段应用。精确限定名优先于同名后缀，普通描述中的 error 等词不自动锁定同名字段。行为查询适度降低声明、测试、文档和评测产物的优先级，相关意图可取消这种偏好。向量命中的窗口最多20行，保留实际命中窗口；精确符号查询直接用词法定位，不触发全库 embedding。

索引缓存位于 `RuntimeDataPaths.code_index_dir(workspace)`，按真实工作区路径隔离。首次查询建索引，后续检查新增、修改、删除，并逐文件核对内容哈希，未变化文件不重复解析；解析器版本变化会重新解析；返回候选前还会重读源码验证哈希，变化时重建并重查一次。保留修改时间和大小的外部编辑也能在下一次搜索发现；已召回候选的旧片段不会直接作为当前源码返回。

索引遵守现有 Git 忽略和工作区边界。非 Git 目录使用既有排除规则并说明限制；单文件超过 1 MiB 或读取失败会标注范围不完整。SQLite 忙、损坏或不支持 FTS5 时扫描源码降级查询，不修改用户源码或删除异常数据库。数据库是可重建缓存，清理缓存后下一次查询会重新构建。

结果的 `scan_complete` 仅描述文件盘点是否完整，不代表语义检索找全。解析质量与扫描完成度分开：语法错误可以使 `coverage.fallback_files` 非零而 `scan_complete=true`。`parse_fallback`、`source_changed`、`rewrite_status`、`notes` 区分解析、源码变化和模型降级。`code_search.completed` 事件记录各路候选数、改写次数、更新文件数、耗时与输出字符数，便于分析成本。

可复现评测见 [评测说明](code-retrieval-evaluation.md)。原始基线、评分器修正后的基线和新增检索结果应分开保留，不能把判分修正归因于检索能力。

第三、四轮增加的源码引用协议现已移除，包括来源编号、系统说明、运行时提醒、最终答案重写和格式纠正调用。搜索工具仍直接返回路径、函数名、行号和源码片段，Agent 按用户要求正常回答；不因答案包含引用字段而额外调用模型。

本次增强以恢复后的 B2 检索实现为基础。B2 历史完整集严格命中 15/16、跨文件找全 4/4，输入 token 中位数 31,554.5；这些是历史观测，不是本次清理后重新测出的保证。第三、四轮日志与报告作为历史证据保留，见[已撤回的引用实验说明](source-citations.md)。


## 可选 embedding

2026-10-02 远程接入补充：当前工作区选择百炼 `qwen3.7-text-embedding`，使用默认 1024 维。
在 `.env` 配置 `CODEAGENT_EMBEDDING_BASE_URL`（使用控制台对应业务空间的 OpenAI 兼容地址）、
`CODEAGENT_EMBEDDING_API_KEY`、模型和维度后，再启用 `CODEAGENT_EMBEDDING_ENABLED=true`。
`CODEAGENT_EMBEDDING_BATCH_SIZE` 控制远程每次 HTTP 请求的输入数量；`qwen3.7-text-embedding` 可设 20，
`text-embedding-v4` 不超过 10。大批次拆分仍共享原调用的总期限和取消检查。
这与 SQLite 每 32 个块进行缓存处理不同。请求显式使用 float 编码。
接口规格参考 [百炼 Embedding 文档](https://www.alibabacloud.com/help/en/model-studio/embedding)。

真实评测可明确选择 `codeagent.tools.search_eval:build_hybrid_tool`，它读取同一组 embedding 配置，未启用时拒绝冒充混合组。
`build_tool` 仍固定关闭向量，便于词法对照；离线 replay 也仍不调用 embedding。
评测 runner 将独立 embedding 配置传入工作进程，不复用聊天 API Key；现有费用报表仍只覆盖聊天/改写调用，
已补充 embedding 请求数、输入用量与缓存复用统计，但没有配置 embedding 单价，因此仍不能当作混合检索总费用。失败或缺失 usage 明确标记不完整。

真实启用验证（2026-10-02 17:52）：使用用户提供的独立业务空间凭据，请求
`qwen3.7-text-embedding` 成功，已将本机 `.env` 中的开关设为 true。
验证只对 `codeagent/code_search` 范围向量化，写入该实际工作区的运行时缓存目录，共 90 个向量。
中文行为查询和精确符号查询都返回 `retrieval_backend=hybrid`、`vector_status=completed`、
`vector_complete=true`、`scan_complete=true`、`degraded=false`，片段首末行及哈希校验全部通过。
首次范围查询包含 7 次 embedding HTTP 请求，第二次只需要 1 次查询向量请求，代码向量从缓存读取。
加上连通性探测，共 9 次成功请求，provider 返回输入用量共 10,585 tokens；未调用聊天模型。
记录位于 `eval-results/embedding-activation-20261002-175244/report.json`，不包含密钥或请求正文。
这是真实远程端到端验证，不是完整项目索引或正式相关性 A/B 成绩；此前“仅模拟验证”的记录描述的是此前阶段。
已运行的 CLI/Web 进程需要重启以加载新的环境配置；密钥仅保存在 Git 忽略的 `.env` 中。

默认关闭，绝不复用聊天模型密钥自动上传源码。远程 OpenAI 兼容服务需要显式配置：

```text
CODEAGENT_EMBEDDING_ENABLED=true
CODEAGENT_EMBEDDING_BASE_URL=https://your-provider.example/v1
CODEAGENT_EMBEDDING_MODEL=your-embedding-model
CODEAGENT_EMBEDDING_DIMENSIONS=1536
CODEAGENT_EMBEDDING_API_KEY=your-embedding-key
CODEAGENT_EMBEDDING_TIMEOUT=10
CODEAGENT_EMBEDDING_MAX_CHUNKS=2000
```

启用后会发送遵守搜索范围与忽略规则的代码块和查询。API key 对无需认证的本地 HTTP 服务可留空。`DIMENSIONS` 必须匹配模型实际输出，不要求服务器支持 dimensions 请求参数。也可以向 `SearchCodeTool` 注入本地 `EmbeddingProvider`，实现 `provider/model/dimensions` 和 `embed(texts, check=..., timeout=...)`；自定义实现必须遵守超时并定期执行 check，不得吞掉取消。

当前使用 `source-v2.sqlite3` 与 `vectors-v2.sqlite3`，保留旧 v1 缓存，不自动删除。源码读取、哈希、解析和远程请求都在写事务外；短事务发布文件版本，读事务固定各路候选的同一代索引。缓存忙或损坏仍可读当前源码降级。Git 文件盘点现在可取消。

向量 profile 包含实际工作区、provider/端点、model、维度与输入格式版本；内容键取实际 embedding 输入哈希，不再混入整文件哈希或行号。只插空行时复用未变化块；改正文、签名或嵌入上下文才重新嵌入。模型切换不混用旧向量。删除文件从当前召回消失，完整盘点后回收当前 profile 的过期缓存；历史模型缓存保留。

`MAX_CHUNKS` 现在是每轮构建预算，而非永远只收前 N 块。已缓存块不占预算，缺失块逐轮补齐，短事务租约避免多进程重复构建同一批，崩溃租约会过期。仍使用精确余弦检索，尚未引入 ANN。自然语言冷查询可以补齐一轮，亦可预先使用独立命令构建：

```powershell
# 默认离线，不读 embedding 密钥、不调用远程 API
python -m codeagent.code_search index --workspace D:\ademo\AgentDemo
# 显式使用已启用的 embedding 配置，按轮续建
python -m codeagent.code_search index --workspace D:\ademo\AgentDemo --embedding --max-chunks 2000
```

`status` 会校验/更新词法快照；加 `--embedding` 只统计当前向量覆盖，不补建。不要将 LLM 前缀缓存和代码向量缓存命中率混为一谈。

返回字段保持兼容，新增：

- `retrieval_backend`：`lexical` 或 `hybrid`；`index_backend` 仍说明词法索引后端。
- `vector_status`：`not_configured`、`skipped_exact`、`completed`、`partial`、`failed`、`source_changed`。
- `vector_complete`、`vector_candidates`、`degraded`、`query_intent`、`file_quota`。

未配置或 embedding 失败时保留词法结果。超时/协议错误可降级，但用户取消、运行预算耗尽直接终止，不当成普通 provider 错误。远程 HTTP 有总期限并在取消时关闭请求。`scan_complete` 仍只描述源码扫描；`vector_complete` 描述本次语义路覆盖范围，二者都不证明找全。

## 多语言 LSP 和编辑反馈

`lsp` 工具提供 `definition`、`references`、`diagnostics`、`sync`，参数为 `operation`、`file_path`、可选 `line` 和 `character`。输入位置及返回 `locations` 从1开始，character 使用 UTF-16 单位；诊断 `range` 保留 LSP 的零起点。仅返回工作区内位置，跨工作区依赖的结果计入 `omitted_locations`。

Python 探测 pyright/pylsp；TS/JS 探测 TypeScript language server；Java 探测 jdtls。可复用已安装的 VS Code Java 扩展及其 Java 21，也可使用 CodeAgent 数据目录 tooling/typescript-lsp 下的已安装服务器。查询不会下载安装服务器，也不自动执行 tsc/Maven/Gradle 命令。可配置：

```text
CODEAGENT_LSP_ENABLED=true
CODEAGENT_LSP_TIMEOUT=5
CODEAGENT_LSP_FEEDBACK_TIMEOUT=1.5
CODEAGENT_PYTHON_LSP_COMMAND=["pylsp"]
```

命令采用 JSON 参数数组，不经过 shell 拼接。代码层可通过 `LspConfig.servers` 注册更多 `ServerDefinition`；可用 JSON 环境配置指定 JAVA/TYPESCRIPT 的 COMMAND、SETTINGS、INITIALIZATION_OPTIONS；与 PYTHON 使用同样前缀规则。Java 数据目录按实际 worktree、项目和 Agent 所有者隔离。Python/TS 识别最近项目根，Java 识别工作区内 Maven/Gradle 聚合根。

服务器按需启动，慢启动返回 initializing 并可在下一请求继续；支持动态能力注册和 workspace/configuration。接收真实磁盘内容，通过 didOpen/didChange/didClose 同步；每次请求还核对所有已打开文档，补上外部编辑的变化。支持推送与拉取诊断、版本检查、请求超时、取消通知、退出及进程回收。缺失、启动失败或超时返回可解释的 status，不阻塞搜索/编辑。`diagnostics=[]` 只有结合 status 才能理解；timeout/unavailable 不能理解为没有错误。部分服务器（包括本次实测 pylsp）不发送诊断版本，结果明确标记 `diagnostics_versioned=false`、`diagnostic_freshness=unversioned_after_sync`，不能声称有严格版本证明。

编辑成功后，Agent 的 PostToolUse 读取 `ToolOutput.changed_files`，使相关路径索引失效，同步 LSP 并在总反馈时间内获取诊断。反馈最多处理5个文件，每文件最多3条简短诊断，放在工具结果开头的 `<edit_diagnostics>` 中，让下一次模型请求能看到；原始成功状态、变更文件等事实不被替换。Team 权限包装器保留 ToolOutput，不再丢弃这些字段。shell 未声明 changed_files 时不承诺即时反馈，但下一次搜索/LSP 请求会读取磁盘兜底。

每个 Agent 拥有独立检索状态和 LSP 文档/进程，子 Agent 克隆时不共享父 Agent 的预算回调或改写客户端。子 Agent 可提供 keywords；Agent 初始化会绑定自己的改写客户端、模型和事件归属；每次查询捕获独立 QueryContext，避免借用父 Agent 的预算或客户端。单实例检索/LSP 串行，独立只读 Agent 可以并行；SQLite 写事务保护跨进程索引，同一 provider 实例的调用受锁保护。Team 保留原有执行门禁与串行调度策略。每次 Agent run 完成、取消、异常或 Team yield 都关闭自己的 LSP 进程，下次需要时重启。

验证分两类：`tests/test_code_intelligence.py`、`tests/test_polyglot_search.py` 使用确定性 embedding/模拟 HTTP 和实际启动的模拟 JSON-RPC 进程，不需要密钥。`tests/test_lsp_integration.py` 设置 `CODEAGENT_TEST_REAL_LSP=1` 后，分别运行已安装 Python、TypeScript、Java 服务器的定义、引用、修改后诊断及释放检查；缺少服务器则跳过并说明。旧题集已经完成零模型、零 embedding 的工具回放，未重新运行付费 Agent 挑战或真实向量相关性评测，详见评测记录。


## 与天枢参考设计的关系

本次参考本机天枢 `c2402fe` 的 `src/search/{hybrid-search,embedding-provider,vector-index}.ts` 与 `src/lsp/{rpc,manager,server-registry}.ts`，在当前项目 `2576bd7` 基础上增强：

- 复用 provider 抽象、RRF 融合、文档版本同步、可扩展服务器注册等设计；保留当前 Python AST 切块、FTS5/BM25、忽略规则、源码核验和预算机制。
- 使用 SQLite 持久化向量与有界精确余弦扫描，避免为当前体量引入新的向量服务；这是规模折中，不宣称适用于无限大的仓库。
- 远程 embedding 只能显式开启，绝不因已有聊天凭据而自动发送源码。
- 搜索前核对内容哈希，付出读取开销以覆盖外部编辑；AST 解析和向量仍按内容增量复用。
- 使用当前 Agent 的 ToolOutput/PostToolUse/工具结果投影完成诊断闭环，不移植 TypeScript 的 tsc 流程。
- 每个 Agent/run 拥有独立 LSP 进程与文档状态，结束或 yield 时释放；牺牲跨 run 的暖启动以换取明确的生命周期与 worktree 隔离。

限制：向量召回仍是线性扫描；没有完整调用图、跨 run 的 LSP 池化或独立语言构建检查器。无版本推送诊断即使经过同步与有限等待，也不能严格证明来自最新版本。真实 Java 验证目前是小型单文件项目，尚未验证大型 Maven/Gradle 多模块项目和框架动态行为。真实 provider 相关性 A/B 未在此次执行。

Windows 使用 Job Object 管理 LSP 进程树，父进程先退出时也能回收子进程；POSIX 使用进程组。服务器主动脱离 POSIX 进程组不在当前回收保证范围内。

拉取式诊断标记 `diagnostic_freshness=requested_after_sync`：响应对应同步后的请求，但协议结果没有文档版本号，所以 `diagnostics_versioned=false`；不将请求对应关系当成服务器版本证明。


## 历史验收记录（2026-10-02 多语言重构之前）

- 全量 unittest discover 跑完953项，2项默认跳过。旧 `test_loop_guard_web` 的一个测试有3个子断言仍要求 Team/子 Agent 不提供 search_code，与本次明确扩展范围冲突；已更新为工具可用且实例独立的断言，原有预算/角色限制保留。
- 更新断言后，`tests.test_loop_guard_web`、`tests.test_code_search`、`tests.test_code_intelligence` 和新增 Team worktree 集成用例合计38项复测通过。随后补充服务器使用空选项对象声明能力的协议分支，对应测试再次通过。未声称修改断言后重新完整运行953项。
- 独立的 Web API/并发/调度/工作区47项测试通过；此前 Agent/CLI/权限/并行/取消/Team 相关95项测试通过。
- 真实集成：在本机已有 pylsp 1.10.0 上运行 `CODEAGENT_TEST_REAL_LSP=1 python -m unittest tests.test_lsp_integration -q`，1项通过，包含定义、引用、修改后错误诊断和进程释放。Windows PowerShell 请先设置 `$env:CODEAGENT_TEST_REAL_LSP='1'`，再执行 Python 命令。
- embedding 验证全部使用模拟 provider/httpx transport，无真实密钥和付费接口。模拟 LSP 测试启动独立 Python JSON-RPC 进程，覆盖协议、服务器缺失/退出、超时、取消、旧诊断丢弃、文档同步和线程/进程释放。
- `git diff --check` 通过。初始未跟踪文档和 tmp7exymt_m 目录保留，未执行 stash/reset/clean 或提交。


## 多语言重构验收（2026-10-02）

本轮重构共享源码/实体模型、解析适配、索引协调和向量构建；保留 search_code 参数与原有证据字段、Python AST、FTS5、Git ignore、工作区保护及取消语义。新增字段包括逐项 language/entity_id/parse_quality/evidence_origin，以及 coverage/index_generation/embedding_usage。

- 最终全项目 `python -m unittest discover -s tests -q`：979项，975项通过、4项按条件跳过，耗时359.462秒。修复相同版本并发发布后重跑通过；此前出现过的977项并发失败保留在评测记录。最后的拉取诊断新鲜度标记调整另经17项 LSP/反馈/隔离测试通过。
- 真实 Python、TypeScript、Java 的定义、引用、编辑诊断和资源释放均有测试。TS 使用 typescript-language-server 6.0.1 + TypeScript 5.9.3；Java 复用本机 Red Hat Java 1.56.0 扩展所带 jdtls/Java21，不改系统 Java8。
- 修复后50项检索/LSP定向测试通过，并发用例重复24次通过；模拟测试覆盖失效、模型切换、插行复用、超过2000块续建、租约、源码一致、动态 LSP 能力、慢启动继续、Windows 编码 URI、子进程释放、Java 编辑诊断进入下一次模型请求、子 Agent 客户端归属。另有两个真实 worker 进程共享缓存且同块只嵌入一次的离线验证。
- AgentDemo 最终离线实际扫描159个文件、3312个块，覆盖 TS/TSX/JS/Python 等；Runtime.execute 定位到 server/core/runtime.ts:541，返回541–560行。它是接通验证，不是人工标注相关性成绩；1个文件有解析降级，扫描完整。
- 本轮验证没有调用真实 embedding 或付费聊天 API。此前90块远程启用验证保留为历史记录；新版使用独立缓存，首次语义检索需重新构建。
