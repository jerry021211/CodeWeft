# 摘要检查点提示词

本次接入用户提供的 system、首次、更新和主模型交接提示。有限回读规则是本项目的适配，不是 Pi 官方原文。

## 实际链路

- Markdown 模板位于 `codeagent/prompts/templates/context_summary_*.md`，由共用的 UTF-8 模板加载函数读取，按项目已有的 `str.format` 方式一次展开。历史材料里的花括号不会被再次当作模板变量展开。
- `context_summary_system.md` 专门约束摘要模型；`context_summary_format.md` 是两分支共用的完整格式。
- `context_summary_initial.md` 用于无有效旧摘要的首次压缩；`context_summary_update.md` 用于已有有效旧摘要的更新。
- `compact_history` 先沿用已有哈希与切点校验。校验通过且旧摘要非空时走更新，否则首次。更新材料仍是 `messages[compacted_message_count:end]`，旧摘要单独传入一次。
- `_summary_params` 只提供旧摘要和本次待压缩对话，不再追加任务快照、runtime-state、文件清单或工具归档清单，也不调用任务状态 provider。原对话中的工具参数、执行结果和归档位置仍保留；运行状态继续正常记录和持久化。
- 输入预算不足时仍对本批对话生成带省略标记的字段预览；system 提示要求区分缺失内容与事实不存在，不因无关字段截断而要求全面回读。
- `_model_summary` 继续直接调用独立摘要客户端，`tools=[]`，不进入主 Agent 执行循环。沿用现有模型、输入预算、字符预算、超长摘要修复及失败回退。`CONTEXT_SUMMARY_MAX_TOKENS` 独立设置输出安全预算，默认 8192；首轮和修复轮都在发送前预留。它不替代 4000 字符的摘要验收限制，推理模型可能将思考 token 计入该上限，需要按实际模型调节；输出未完整结束时保留旧摘要与水位。
- 新摘要成功后替换旧摘要，近期消息与工具配对不变。旧格式会话无需迁移或批量重写。
- `context_summary_handoff.md` 替换实际发送位置的旧核实要求，仍在原来的 user 摘要消息中，不改变主 system 或当前用户指令的优先级。
- 归档工具说明区分对话归档、工具输出归档与工作区文件；分页的“还有内容”不表示必须全部读取。没有新增读取拦截器、硬次数限制或循环判定器。
- 评测的 `request_kinds.py` 同时识别三代摘要提示，避免重评旧记录时把摘要费用算入主模型。

必要补充只有：明确传入已有字符预算；保留结论来源与验证范围；区分历史阻塞与本次交付；正确选择归档工具。压缩阈值、切点算法和迭代上限没有调整。

## 免费请求构建验证

在项目根目录运行：

```powershell
python -m unittest tests.test_context_summary_prompts tests.test_summary_character_budget tests.test_context_limits -v
```

这些测试用固定回复验证模板选择、展开、增量范围、模型和预算参数、单个交接包装、原消息配对、旧格式恢复及失败回退；不证明真实模型的摘要质量。

## 可复用行为样例（真实模型尚未执行）

材料：`evals/context_suite/summary_behavior_cases.json`。

| 样例 | 要核查的行为 |
|---|---|
| B01 | 问题已解决后进入 Done，不再创建相同调查 |
| B02 | 修改成功、测试未运行，不能总结成测试通过 |
| B03 | 缺少 error_code，仅保留该字段的定向补查 |
| B04 | 用户取消 CSV 后更新为 JSON，不保留旧待办 |
| B05 | 只需交接时直接交付，不去完成历史开发任务 |
| B06 | 新旧证据冲突时标明冲突，允许针对性验证 |

每条材料包含 `previous_summary`、`conversation` 和独立的 `expected_behavior` / `forbidden_behavior`。后两项仅用于人工评分，不能注入模型请求。旧摘要为空的样例测试首次生成，其余测试更新；B05 同时包含旧格式摘要。

获准真实回放后，使用同一摘要请求构建和独立摘要客户端，只传入旧摘要和本次待压缩对话。保留实际请求、原始输出、模型、预算、用量及逐项人工判定；不要把与期望文字相同当作自动语义评分。也要检查更新后的摘要在主 Agent 恢复时是否正确完成交付。

已有固定历史的 S02/S06 可用于验证重复回读和答案正确性；下面命令会产生真实模型费用，本次没有执行：

```powershell
python -m evals.context_suite run --mode live --scale stress --cases S02 S06 --variants A D --repeats 5 --max-iterations 12 --max-api-calls 16 --max-trials 20 --max-total-api-calls 320
```

观察通过数、轮数耗尽、回读次数及其内容、总 token 和估算费用。保留旧实验，不混合不同提示词版本；S04 压缩余量问题另行评测。样例已参与提示词设计，不能把这些题上的结果当作未见场景的泛化证明。
