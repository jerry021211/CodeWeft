# 技能库与遵循评测

CodeWeft 使用全局技能库：CLI 和 Web 只将名称、描述和适用场景放入目录，模型通过 `load_skill(name)` 读取正文。技能是流程指导，不是强制执行器；真实调用记录与产物检查才能证明具体步骤发生过。

## 安装随项目提供的技能

在仓库根目录运行，或安装 Python 包后运行：

```powershell
python -m codeagent.skills list
python -m codeagent.skills install
```

安装到第一个配置的 `SKILLS_DIR`；默认是 `CODEAGENT_DATA_DIR/skills`。相对路径相对于数据目录解析，与当前项目无关。安装器输出 `installed` 和 `skipped`，已有同名目录一律保留，不覆盖自定义内容。只安装部分技能或试装到隔离目录：

```powershell
python -m codeagent.skills install bug-fix repository-map
python -m codeagent.skills install --destination .tmp/skills-preview
```

技能源文件位于 `codeagent/skills/library/`，随 wheel 一起分发。安装是显式操作，打开工作区不会自动导入技能。更新项目后再次安装只补充缺少的目录；要更新已安装技能，应先比较和备份本地定制，再人工合并。`ENABLE_SKILLS=false` 仍关闭运行时加载。

| 技能 | 主要用途 |
| --- | --- |
| `repository-map` | 入口、模块、调用路径和测试方式 |
| `bug-fix` | 复现故障、最小修复、回归验证 |
| `test-design` | 行为测试、边界和错误分支 |
| `python-refactor` | 保持签名、返回值和副作用的 Python 重构 |
| `code-review` | 可触发且能定位的代码问题 |
| `api-contract` | 请求响应、可空字段和客户端兼容性 |
| `react-ui` | React 状态、异步请求和交互 |
| `sqlite-migration` | SQLite 旧数据迁移、事务和恢复 |
| `dependency-audit` | 清单、锁文件和运行时一致性 |
| `performance-diagnosis` | 基于 profile 的瓶颈和对照实验 |
| `agent-runtime` | 工具、权限、取消、预算和结果传播 |
| `skill-evaluation` | skill 触发、加载、过程和产物评测 |

安装后重新启动 CLI 会话，或开始新的 Web Agent 运行以刷新目录。例如发送“使用 bug-fix 修复这个空输入异常，并运行已有回归测试”，或直接提出匹配的自然语言任务。无需把技能全文粘贴进提示词。运行记录应出现成功的 `load_skill`；模型仅口头宣布使用技能不算加载证据。

## 先做离线检查

以下命令在源码仓库中执行，不调用模型：

```powershell
python -m unittest discover -s tests -p "test_bundled_skills.py"
python -m unittest discover -s tests -p "test_skills_eval.py"
python -m unittest discover -s tests -p "test_skills.py"
python -m evals.skills --mode offline
```

安装测试检查跨工作区的 CLI/Web 发现、完整加载、重复安装及保留定制。评分器的负向测试检查：只有正确答案但没加载、加载失败、批量部分读取失败、未授权文件改动、伪造通过记录但代码仍错误、缺少修复前失败证据等不能被判为全通过。

离线使用固定响应客户端跑真实 Agent 循环，共 17 例，只验证接线与评分器。固定响应内含预定步骤和答案，成绩不能作为模型能力证据。

## 真实模型测试

真实评测读取现有模型配置并产生供应商调用费用。每例独立进程、独立夹具、最多 8 次模型调用，每次最多 2,048 输出 token，每例 120 秒进程截止时间；循环另有 60,000 累计 token 的停止阈值，可能在最后一次响应后越过该阈值。整套单次 17 例最多 136 次主模型调用，实际账单取决于供应商。没有货币费用硬上限。

```powershell
# 默认测试项目随附的 12 个技能
python -m evals.skills --mode live

# Windows：测试实际全局库，包含保留下来的定制技能与额外候选
python -m evals.skills --mode live --skills-root "$env:LOCALAPPDATA/CodeAgent/data/skills"

# 定点重复：每次生成新目录，不覆盖历史证据
python -m evals.skills --mode live --case repair --case repair-explicit --repeats 3
```

如果修改过 `CODEAGENT_DATA_DIR` 或 `SKILLS_DIR`，请将 `--skills-root` 替换为安装器输出的路径。重复次数允许 1–5，所有重复均计入报告，不能只选择最好结果。

套件包括 12 个自然语言匹配任务、2 个显式指定技能的任务、2 个不应触发的请求和 1 个缺失材料案例。完整技能目录作为干扰项参与选择，不把期望技能名注入自然语言案例。

工具范围仅有 `load_skill`、限制工作区的文件读取；两个修复案例还可写入唯一目标 `totals.py` 并调用固定测试工具。不开 shell、MCP、记忆、子 Agent 或外部追踪。测试是受限工具配置，不代表完整 Web 工具环境，也不是操作系统安全沙箱。

## 怎样解释报告

每次运行在忽略的 `eval-results/skills-<mode>-<time>-<id>/` 中保存：

- `report.json`：逐例检查、模型、用量、耗时与通过分母。
- `skills/` 和 `source/`：技能内容、评测代码和技能提示词快照；报告记录技能 SHA-256。
- 每例 `requests.jsonl`、`responses.jsonl`、`tools.jsonl`：脱敏请求、公开响应和实际工具结果，不保存模型隐藏推理。
- 每例 `workspace/`、`answer.txt`、`result.json`：最终产物、回答和评分；失败或超时保留已写出的证据。

检查分为加载命中、额外技能、必需文件读取、范围保持、正常结束与回答事实。**分析类回答仅检查少量事实锚点，不能证明整个解释正确或所有技能步骤都执行了，仍需人工阅读。** 不将回答中出现技能名作为成功加载的证据。

故障修复案例额外要求：修改前加载 skill、先读取实现及测试、观察失败基线、最后一次修改后观察通过，且独立测试重新验证最终代码。独立检查使用宿主持有的测试内容，模型不能改断言使其通过。

`passed` 要求该例所有检查通过，报告的 `checks` 保留具体失败项。加载错误、运行失败和超时不会被算成通过。`no_unrelated_skills` 严格要求不加载预期之外的任何技能；保留的宽泛自定义技能可能造成此项失败，应检查实际适用范围后判断原因。

修正评分器后可以免费重算已有证据，结果写入新目录，原始记录保持不变。用量从供应商响应重新汇总，缺失用量标为不可用，不视为零费用：

```powershell
python -m evals.skills --regrade eval-results/skills-live-实际目录名
```

首次实际运行记录与限制见[2026-10-20 评测报告](skills-evaluation-2026-10-20.md)。

技能或模型升级后使用相同案例回归。少量冒烟全通过不等于百分之百遵循；确实不能跳过的权限、发布或数据校验条件，应在宿主工具入口强制实施。
