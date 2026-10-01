# 摘要收益预检与连续任务材料修复

完成日期：2026-09-27。本次未调用真实模型；旧评测证据保留，未重新写入旧报告。

## 修改内容

- [manager.py](D:/new_codeAgent/codeagent/context/manager.py)：实际请求和收益预检复用同一保留消息构造逻辑。候选切点先计算省略整个新摘要包装后的请求长度下界，只有最大可能收益达到原有要求，才进入摘要输入预算检查和模型调用。预检跳过记录 `insufficient_compressible_history`，不更新游标、不写归档、不启动失败冷却。
- 摘要生成后的实际收益检查、输入 Token 下降检查、原有模型失败/截断/收益不足回退与冷却继续生效。预检通过不代表真实摘要必然合格。
- [cases.py](D:/new_codeAgent/evals/context_journey/cases.py)：材料版本为 `context-journey-v2`。原第 0 批背景放入 `evidence/batch-00.txt`，独立准备回合实际读取并结束，再进入正式任务。六种任务、后续背景规模和覆盖要求保留。
- [checker.py](D:/new_codeAgent/evals/context_journey/checker.py)：允许 `_to_number` 这样的普通局部辅助函数；双下划线名称、私有属性访问、导入和执行能力限制仍保留。旧 T04/A 的真实源码用当前评分器复核，功能检查 2/2 通过；没有改动旧评分报告。
- [grading.py](D:/new_codeAgent/evals/context_journey/grading.py) 和 [runner.py](D:/new_codeAgent/evals/context_journey/runner.py)：保存并展示预检跳过原因、候选切点、保留用户消息字符数、最大可能节省和最低要求。跳过不计为成功摘要。报告显示材料版本，避免混合新旧实验。
- [test_context_compaction_preflight.py](D:/new_codeAgent/tests/test_context_compaction_preflight.py) 和 [test_context_journey.py](D:/new_codeAgent/tests/test_context_journey.py)：覆盖预检不调用模型、无冷却、后续重新评估、实际收益不足回退、工具配对、预算、材料覆盖与评分器正反例。

## 本地验证

| 测试组 | 测试数 | 通过 | 跳过 | 失败 |
|---|---:|---:|---:|---:|
| test_context*.py | 222 | 221 | 1 | 0 |
| 工具输出分页、历史观察、摘要字符预算 | 23 | 23 | 0 | 0 |
| test_eval*.py | 17 | 17 | 0 | 0 |
| 合计 | 262 | 261 | 1 | 0 |

跳过的是符号链接创建相关测试：Windows 返回 WinError 1314，当前进程没有创建符号链接的权限；不算通过。
Ruff 和 git diff --check 通过。

## 旧轨迹局部回放

[回放记录](D:/new_codeAgent/eval-results/20260927T031635Z-context-preflight-regression-67d24bd9/result.json)

读取旧试验发生摘要时的规范消息前缀，在当前代码下复核；需要摘要响应时只复用已保存的旧响应，没有发出网络请求。它不是重新测试模型质量。

| 旧场次 | 理论最多可省字符 | 最低要求 | 本地结果 |
|---|---:|---:|---|
| T04/D | 2,717 | 3,435 | 预检跳过，0 次摘要调用，无失败冷却 |
| T05/D | 4,395 | 14,887 | 预检跳过，0 次摘要调用，无失败冷却 |
| T01/D | 4,368 | 3,643 | 预检允许；复用旧摘要后仅省 315 字符，由实际收益检查拒绝并沿用冷却 |

T01 的 315 是当前代码及当前交接包装的回放值，不覆盖原报告的 217。预检只排除必然无收益的候选，不预测模型生成多长的摘要。

## 新材料离线完整链路

[12 场 A/D 离线报告](D:/new_codeAgent/eval-results/20260927T031434Z-context-journey-offline-a3435658/report.md)

- T01–T06，各 A/D 一次，共 12 场，自动检查全部通过。
- 6 场 D 均满足压缩覆盖：首次交付请求前至少两次成功摘要、至少一次更新，并折叠关键任务锚点。
- A/B 不要求摘要，但要求材料读取完整。此次 A 的六场材料覆盖也通过。
- 本次响应由脚本注入，不能将通过率作为真实模型的长期记忆或任务保持效果。
- 离线报告目录的证据封存校验通过。

已生成可浏览的新材料：[T01 任务说明](D:/new_codeAgent/eval-results/context-journey-kit-20260927-v2/T01/任务说明.md)。同一材料目录下也有 T02–T06。

## 后续操作

本地检查和离线链路已经执行，无需为继续评测重复跑一遍。
如果要开始真实模型验证，在项目根目录执行下面命令；会产生模型费用，本次没有代为执行。

```powershell
python -m evals.context_journey run --mode live --cases T01 T04 --variants A D --repeats 1 --max-trials 4 --max-total-api-calls 256
```

先看 D 是否覆盖充分，再看任务自动检查、人工行为审查、Token、费用和耗时。保持新旧材料批次分开；预检跳过本身不是失败，也不是压缩成功。

真实摘要语义质量、循环补查是否减少，以及长期成本收益均待这轮真实模型验证。T05 的字段表达等其他已知评测问题不在本次修复中，后续扩展前应单独核查。
