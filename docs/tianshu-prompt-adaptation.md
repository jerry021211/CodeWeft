# Tianshu 提示词设计适配

本次将 Tianshu 的部分提示词设计适配到 CodeWeft 的 Python 提示词装配器，保留现有权限、角色、模型设置与上下文协议。不是完整 CVM 移植，也不声称仅改提示词就能获得上游的运行效果。

## 来源

2026-10-13 取得公开仓库 `main` 快照 `05cef587f24157360059e62cd7ed7146cbb87c86`，未修改本地 Tianshu 仓库。实现与文案按本项目独立编写，参考来源为 Apache-2.0 许可的 [Tianshu-harness](https://github.com/huiliyi37/Tianshu-harness/tree/05cef587f24157360059e62cd7ed7146cbb87c86)：

- [static.ts](https://github.com/huiliyi37/Tianshu-harness/blob/05cef587f24157360059e62cd7ed7146cbb87c86/src/prompt/static.ts)：任务分类、工程判断、证据、交付、委派和模型校准。
- [static-subagent.ts](https://github.com/huiliyi37/Tianshu-harness/blob/05cef587f24157360059e62cd7ed7146cbb87c86/src/prompt/static-subagent.ts)：保留子助手必须自己执行的规则，按工具能力筛选指引，去除主控专属流程。
- [block-policy.ts](https://github.com/huiliyi37/Tianshu-harness/blob/05cef587f24157360059e62cd7ed7146cbb87c86/src/prompt/block-policy.ts)：参考材料可以限量，行为规则应常驻。
- [engine.ts](https://github.com/huiliyi37/Tianshu-harness/blob/05cef587f24157360059e62cd7ed7146cbb87c86/src/prompt/engine.ts)：稳定前缀与运行状态分离。

## 对应实现

| 设计 | CodeWeft 的适配 |
| --- | --- |
| 共同证据与权限规则 | `core.md` 对所有角色必选；关键结论核对实现或回执，外部报告不能自行成为事实或授权。 |
| 工程判断与用户协作 | `identity.md` 允许提出有证据的异议，常规细节自行决定，但不能替换用户明确目标。 |
| 按任务选择执行方式 | `execution.md` 区分查询、方案与实施；关键约束明确后落地最小可验证步骤。 |
| 写入者自己的验证责任 | 新增 `verification.md`，用于具备文件编辑或 Shell 工具的普通助手、子助手和实施成员。只读角色、规划者和团队主控不加载这套实施流程。工具存在不授予执行权限。 |
| 工具相关指引 | `tool_repo_map.md`、`tool_search_code.md`、`tool_grep.md`、`tool_read_file.md` 只在对应工具出现时注入，顺序固定。 |
| 实际协作工具的契约 | 仅普通主助手拥有 `collaborate` 时注入 `collaborate.md`；保留 `subagent` 原有串行、只读并行与后台语义。 |
| 必要规则不能被材料挤走 | 新规则为必选静态片段；项目约定对所有角色必选。预算不足明确报错，不能静默丢弃后继续。大型记忆和技能目录仍按原预算处理。 |
| 稳定前缀 | 继续使用现有模板来源、内容哈希和缓存生命周期；不修改历史消息或注入另一套上下文增量格式。 |

子助手不接收主助手的全局实施规划或用户交付流程，保持原有简短报告契约。团队角色的候选提交、只读规划与审批边界保留。

## 与上游有意不同的地方

- 不移植星域身份、固定思考语言、`recall_capsule`、`deliver_task`、自动 Git 提交、专用浏览器工具或上下文标签；本项目没有相同的运行时契约。
- 不采用每个任务强制六阶段、穷尽全部调用方、固定多轮后重读文件等规则。保留此前的规划收敛要求和与改动风险相称的验证，减少重复规划。
- 上游明确限制脑内预演的长段校准主要针对 GLM；本次提炼为通用执行原则，不冒称为 DeepSeek 专有优化，不按模型名称猜参数。
- 不新增 `lean/full` 配置。当前系统提示词规模较小，已有角色、能力筛选和材料预算；进一步压缩必须先做质量对照。
- 不降低思考等级或输出上限，不自动启用 CVM，不引入新的权限审批流程。

## 验证与使用

提示词装配测试覆盖角色和工具组合、只读与写入者区别、无协作工具时不注入协作指引、参考材料压力下的规则保留、项目约定超预算时失败，以及工具注册顺序变化时哈希稳定。现有前缀缓存和 Agent 集成测试用于检查调用链兼容。

默认模板会直接生效，无需新开关；重启服务并用新会话比较，避免旧会话已有长规划影响结果。若 `PROMPT_TEMPLATE_DIR` 下存在同名自定义模板，它会覆盖对应内置模板，需要同步该文件才能获得相应文案；装配 trace 中的 `source` 可确认来源。

测试只能验证装配和调用契约，不能证明模型更快或生成质量提高。真实模型验证沿用 [对照方法](model-services.md#减少实现前的长时间规划)：相同任务、相同干净工作区、相同模型与思考等级，重复比较首次实现时间、总耗时、输出 token、重试和独立验收结果，完整执行后再判断收益。
