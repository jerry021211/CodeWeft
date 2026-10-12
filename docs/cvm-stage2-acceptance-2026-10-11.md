# CVM 阶段 2 实施与验收记录

阶段 2 增加工具事实账本和版本化验证记录：保留每次实际尝试的退出码、进程状态、输出完整性、测试汇总，以及检查前后的声明文件集合哈希。一轮结束时重新核对文件，发现后续编辑、新增或删除后将旧验证标为 stale。

本阶段只观察。没有自动补跑命令、额外模型调用、完成门禁或前端验收卡片；模型仍可能在测试失败后口头说成功，账本会独立保留失败事实。阶段 3 尚未实施。默认配置仍全部 off，evidence 当前只开放 off/shadow。

## 实际改动

| 文件 | 职责 |
| --- | --- |
| [evidence.py](../codeagent/cvm/evidence.py) | 原始事实、执行身份去重、验证记录、同定义失败替代、结束核对、完整性校验 |
| [verification.py](../codeagent/cvm/verification.py) | 可信检查定义和简单 unittest/pytest 命令的保守适配器 |
| [snapshot.py](../codeagent/cvm/snapshot.py) | WorkspaceGuard 内的文件清单、内容哈希、新增删除检测、扫描上限和性能记录 |
| [state.py](../codeagent/cvm/state.py) | 任务关联、真实用户和已批准方案中的检查声明、版本化导入导出 |
| [agent.py](../codeagent/agent.py) | 串行、并行、重试、取消、拒绝接入；结束时核对证据；SDK 注册接口 |
| [registry.py](../codeagent/tools/registry.py) | 提供真实 BashTool 的 cwd 和进程来源信息，避免仅凭工具名称认定可信执行 |
| [factory.py](../codeagent/web/factory.py) | Team 保持 contract/evidence 均 off，普通会话沿用原检查点保存与恢复 |

记录不是 PostToolUse Hook。实际尝试返回后立即生成独立事实包，再进入 Hook 和上下文投影；取消异常中的部分输出也采集。并行工作线程只产生自己的事实包，父线程按请求顺序和尝试次数统一入账。同一次返回不重复记账，真实重试保留不同 attempt 和原始 execution_id。未执行的拒绝记录 executed=false；没有收到结果的中断可记录 executed=null，避免把未知当作已执行成功。

普通子 Agent 只记录局部事实，不继承父账本。子 Agent 自称成功不会成为父任务的验证通过。整个阶段不改变 BashTool、权限、审批、预算和自动重试规则。

## 怎么启用

在启动后端的 PowerShell 中执行，然后从该终端启动或重启原服务：

```powershell
$env:CODEAGENT_CVM_CONTRACT = 'active'
$env:CODEAGENT_CVM_EVIDENCE = 'shadow'
```

如需纯观察对照，可把 contract 也设为 shadow。evidence=shadow 要求 contract=shadow/active，以保留跨 Run 的任务身份；evidence=active 尚未开放，completion 等后续组件仍只允许 off。不要把 shadow 理解为自动验收通过。

## 如何声明检查范围

模型执行的命令会被记录，但模型不能自行把一条测试声明成覆盖全部验收。第一版支持真实用户消息中的明确 JSON 行、已批准方案中的同类行，或可信 SDK 调用方注册的定义。

在一个临时 Python 项目的普通会话中发送，例如：

```text
检查计算逻辑，执行下面指定的测试。
验证配置：{"command":"python -m unittest discover -s tests","inputs":["src","tests","pyproject.toml"],"cwd":"."}
```

根据项目实际目录修改 inputs；它们是相对于 cwd 的文件或目录，不是 glob。应包含相关源码、测试、配置和实际存在的依赖锁文件。目录会递归记录成员，可检测新增和删除。日志、数据库和其他持续写入的数据不应放入验证输入集合，否则它们变化也会使验证过期。

command 必须和实际 Bash 调用的命令文本一致，cwd 也必须相符。不做去引号、小写化等可能改变含义的宽松匹配；测试范围与快照输入范围分别保存。没有匹配定义时，支持的 runner 仍可得到执行时的测试结果，但 validity=unknown，不能冒充有版本绑定的有效验收。

检查声明不自动执行命令、不授予新权限。自然语言验收条件不会自动转换成检查定义。普通子 Agent 的委派文本不会冒充用户声明；旧会话历史也不会补造已经存在的验证结果。启动项目检查配置文件的自动读取尚未增加，当前采用真实用户、批准方案和可信调用方这三种明确来源。

SDK 使用现有 Agent 创建任务之后注册：

```python
from codeagent.cvm import VerificationDefinition

# agent 已使用 contract=shadow/active、evidence=shadow，并已有 current task。
agent.register_verification(VerificationDefinition(
    command="python -m unittest discover -s tests",
    inputs=("src", "tests", "pyproject.toml"),
    cwd=".",
    source_kind="host",
    source_ref="host:project-test-policy-v1",
))
saved = agent.export_cvm_state()
```

该接口没有加入模型工具 schema。新增“新任务”会更换 task_id，旧任务的定义与验证不会自动覆盖新任务。

## 记录怎么看

普通 Web 会话仍保存到 `state.db` 的 `checkpoints.metadata_json.cvm`。阶段 2 状态使用外层 schema_version=2，新增 evidence 子对象；子对象具有自己的 schema_version、policy_version 和内容指纹。没有新增数据库表。运行配置仍可在 `runs.metadata_json.cvm_config` 查看。

```text
cvm.evidence
  definitions       用户或可信调用方声明的命令、输入范围、来源、任务
  observations      每次工具尝试的真实返回事实
  verifications     命令结果、检查范围、前后快照、有效性、日志 ID
  diagnostics       缺失或降级原因
  degraded          采集失败等是否使账本不再足以支持有效验证
  content_sha256    发现存储损坏用的指纹，不是防恶意篡改签名
```

在数据库查询工具中执行：

```sql
SELECT conversation_id, run_id, created_at,
       json_extract(metadata_json, '$.cvm.evidence.verifications') AS verification_records,
       json_extract(metadata_json, '$.cvm.evidence.diagnostics') AS diagnostics
FROM checkpoints
WHERE json_extract(metadata_json, '$.cvm.evidence') IS NOT NULL
ORDER BY created_at DESC
LIMIT 20;
```

| 字段 | 含义 |
| --- | --- |
| result=passed/failed | 该次受支持进程返回的测试汇总结果，模型文字不参与判定 |
| result=unknown | 未支持命令、没有可信进程信息、无完整汇总、零测试、全跳过等 |
| result=blocked/timeout/cancelled/running | 未执行、环境阻塞、超时、取消、清理未确认等状态 |
| validity=fresh | 声明输入集合在检查前后及最近一次核对时一致；不是整个任务 verified |
| validity=stale | 已经发现文件版本或成员变化；不删除历史测试结果 |
| validity=unknown | 无可信定义、快照部分缺失、读取失败或账本降级 |
| superseded_by | 同一定义和范围后续有效通过替代旧失败；旧失败本身仍保留 |
| output_id | 原有工具日志归档引用；账本不复制完整日志和代码 |
| task_revision | 采集时的任务修订号，避免以后将旧证据误解成对所有新要求的证明 |

结果和有效性要一起看：passed + stale 表示“当时通过过，但之后代码变了”；failed + fresh 表示“当前核对的文件版本下，该次检查失败”；unknown + fresh 只表示输入版本稳定，测试语义仍未知。

Web 在原来的每轮终止事务中保存，不新增运行中定时检查点。CLI 仍主要在进程内保存，可通过 SDK 导出；本阶段没有新增退出后自动恢复。回退 evidence=off 时保留已有证据载荷，重新启用后会重核文件。阶段 1 检查点可以恢复为空账本；未知版本、跨工作区或损坏载荷保留原数据并停止干预，不升级成通过。旧阶段 1 程序看到 schema 2 会按未知版本保留载荷并停用干预。

## 快照与判定的边界

- 仅支持简单 `python -m unittest ...`、`python -m pytest ...` 和 `pytest ...`，选项采用小型允许列表。复合 shell、重定向、吞错包装器、npm、任意 Python 脚本保留执行事实但不认定可信测试通过。
- 要求真实 BashTool 来源、进程 ID、退出码和可识别的终止汇总。成功退出或输出“passed”本身不够。已有完整 Bash 归档的尾部可用于解析汇总，显示页截断不会自动丢失这一证据；源日志本身不完整则降级为 unknown。
- 输入列表是明确声明的覆盖范围，不推断它穷尽所有隐式依赖。配置错误、遗漏依赖、测试断言被削弱、伪造测试框架本身，都不能靠该账本证明业务正确。阶段 6 的独立 verifier 仍然必要。
- 本地快照支持非 Git 工作区，包含文件清单和内容哈希，不依赖 HEAD。它不创建隔离 worktree，也不归因是谁修改了共享文件。并发或外部修改会导致 stale/unknown。
- 文件快照不是原子文件系统快照；检测扫描时变化及检查边界变化，无法证明运行期间没有发生过“修改后又恢复”的瞬时变化。外部服务、系统库和环境变量未被冻结；host_python 只是宿主版本，runner_version 未探测时明确为 null。
- 每次快照最多检查约 5000 个条目、读取 32 MiB；超过上限、权限错误、链接路径等会标 partial。每次保存扫描字节数与高精度耗时。只在有匹配定义的检查前后、恢复和结束边界核对，不每个模型轮次扫描全仓。
- 快照政策明确排除 `.git`、`__pycache__` 和几类工具缓存目录；敏感文件如 `.env`、私钥文件不读取，遇到时标 partial。路径经过 WorkspaceGuard，不越出当前工作区。
- 账本最多保留 5000 条 observation，达到上限会 degraded 并使验证有效性降级，不静默丢失记录后继续宣称 fresh。
- 普通读取或有损搜索返回零条，不推导 absence_proven；当前该能力始终为 false。

## 测试与复测

在仓库根目录执行受检入口：

```powershell
python tests/run_cvm_stage2.py
```

入口强制收集阶段 0、1、2 专项及上下文、前缀缓存、Plan Mode、工具事实、并行执行、Bash 进程清理回归。零收集、整组跳过或失败均不能通过。报告比较测试前后的源码和测试文件哈希，并继续对照冻结的 off 请求基线。

本次最终回归命令：

```powershell
python tests/run_cvm_stage2.py --pattern 'test_loop_guard*.py' --pattern test_round_budget.py --pattern test_cli.py --pattern test_agent.py --pattern test_read_only.py --pattern test_web_scheduler.py --output eval-results/cvm-stage2/stage2-acceptance.json
```

新增专项覆盖 E01 至 E12 的工具事实、范围、快照与恢复边界；E13 覆盖测试文件变化导致过期，不声称检测所有测试语义作弊。真实 Agent 用例覆盖 Hook 短路/修改、串行重试、并行乱序与取消、权限拒绝、参数错误、真实 Bash/unittest、结束前外部编辑、压缩、任务隔离、批准方案、Web 保存与恢复、采集失败和 off/shadow 实际模型请求对照。

最终共收集 315 项，314 项通过，1 项为 Windows 上正常跳过的 POSIX 清理测试；其中阶段 2 新增专项 34 项。off 请求与冻结基线一致，测试期间源码及测试文件哈希保持一致。未将环境失败或整组跳过算成通过。

验收报告另有 stage2_probe：在临时目录中真实执行两次 Bash/unittest，记录“failed + fresh → passed + fresh → 再次编辑后 passed + stale”，附快照扫描字节数和耗时。它不调用模型，不请求网络，完整账本留在报告中；临时进程日志随临时目录清理，不宣称报告中的临时日志 ID 长期可访问。

验证中两个原有 Windows 清理测试在沙箱内失败，独立运行可复现；允许正常 taskkill 后通过，未修改 BashTool 或放宽其测试。另修正 `test_read_only.py` 的旧检查点夹具：补齐当前 Todo 恢复所需的 todos 和 metadata，与真实检查点结构一致，没有删减只读断言。工作区原有 Todo、前端和文档改动均保留。

结果文件：验收 JSON（本地生成文件 `eval-results/cvm-stage2/stage2-acceptance.json`，不随仓库发布）、测试日志（本地生成文件 `eval-results/cvm-stage2/stage2-tests.log`，不随仓库发布）。本阶段没有调用真实模型服务，没有运行全库或前端构建，不将离线边界验证等同于真实模型效果评测。
