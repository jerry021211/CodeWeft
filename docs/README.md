# CodeWeft 文档

从 [项目首页](../README.md) 开始安装和体验；按下面的目标继续阅读。当前使用说明与历史设计记录分开列出，带日期的验收结果只代表对应版本和实验条件。

## 使用 CodeWeft

| 想做什么 | 阅读入口 |
| --- | --- |
| 安装、启动并完成第一个任务 | [README](../README.md) |
| 使用 Web、CLI、Python SDK、附件和工具 | [使用指南](usage-guide.md) |
| 接入模型，配置协议、推理、语音和向量服务 | [模型服务](model-services.md) |
| 先调查、提交方案，再批准执行 | [规划模式](plan-mode.md) |
| 委派子任务，执行依赖计划，查看验证与恢复结果 | [多 Agent 协作](collaboration.md) |
| 了解只读保护与本地数据边界 | [只读权限](usage-guide.md#只读权限) |
| 管理同时运行的会话 | [多会话并发](concurrent-sessions.md) |
| 按指定时间或固定间隔自动执行项目任务 | [定时任务](scheduled-tasks.md) |

## 理解和扩展

| 主题 | 文档 |
| --- | --- |
| 运行流程、模块职责与源码阅读顺序 | [项目架构](architecture.md) |
| 修改代码后自动更新前端和重启后端 | [开发热更新](hot-reload.md) |
| 关键词、符号、结构化代码和可选向量检索 | [代码检索](code-search.md) |
| 工具结果分页、归档与原文回读 | [工具输出策略](tool-output-policy.md) |
| 上下文预算、摘要和历史投影 | [上下文管理](context-management-porting.md) · [摘要提示词](context-summary-prompts.md) |
| 读取并行与普通运行时的只读子 Agent | [普通 Agent 并行](parallel-execution.md) |
| 长期记忆的保存和召回 | [记忆检索](memory-retrieval.md) |
| 安装开发技能、测试触发与流程遵循 | [技能库与评测](skills.md) |
| 防循环、取消、重试和执行预算 | [执行预算](loop-guard.md) |
| 缓存组装与观测 | [缓存优化](cache-optimization.md) · [DeepSeek 前缀缓存](deepseek-prefix-cache.md) |
| 原生协作、共享运行时和旧 Team 的代码边界 | [多 Agent 目录](multiagent-layout.md) |

Skills、MCP 与 Python SDK 的扩展示例统一放在 [使用指南](usage-guide.md)，避免复制多份配置说明。

## 开发和评测

开发环境、按改动选择测试以及提交要求见 [贡献指南](../CONTRIBUTING.md)；前端入口见 [Web 开发说明](../web/README.md)。

| 目标 | 文档 |
| --- | --- |
| 区分离线接线测试与真实模型评测 | [评测入口](../evals/README.md) · [最小评测基线](minimal-evaluation-baseline.md) |
| 逐步运行上下文评测并读取报告 | [操作指南](context-evaluation-walkthrough.md) · [材料包](context-evaluation-kit.md) |
| 评估连续任务中的上下文行为 | [连续任务评测](context-journey-walkthrough.md) |
| 评估代码检索和 Agent 使用检索的效果 | [检索评测](code-retrieval-evaluation.md) · [三组对照](agent-search-three-arm-evaluation.md) · [LSP 评测](agent-search-lsp-evaluation.md) |
| 参考项目级基准的设计方案和步骤示例 | [待实施设计](benchmark-design.md) · [步骤示例](benchmark-walkthrough.md)；不代表已有通用跑分工具 |
| 试用任务契约和验证监督 | [CVM 验证指南](cvm-verification-guide.md) · [对照评测](cvm-evaluation-guide.md) |

CVM 默认关闭，验证监督目前只覆盖明确配置的检查范围；它不保证任务正确，也不自动执行检查。[已公开的真实对照结果](cvm-step2-results-2026-10-13.md)尚未证明提醒提高任务成功率。真实模型评测会产生 API 费用，运行前请阅读对应指南；常规贡献验证不要求运行它们。

## 历史设计和兼容

以下资料用于追溯设计、迁移和实验，不作为新用户的默认操作路径。旧文件保留原路径，方便已有链接继续访问。

| 主题 | 历史或兼容资料 |
| --- | --- |
| 原生协作迁移 | [编排迁移方案](team-orchestration-migration-plan-2026-10-13.md) · [工具拆分记录](native-collaboration-2026-10-13.md) |
| 旧 Team 会话 | [协作边界](team-collaboration-boundaries.md) · [集成与本地交付](team-managed-integration.md) · [执行观察面板](team-observer.md) |
| 已移除的 Discuss 模式 | [历史研究](discuss-mode.md)；当前入口见 [只读权限](usage-guide.md#只读权限) |
| 代码检索迭代 | [升级方案](code-search-upgrade-plan-2026-09-29.md) · [首轮实现](code-search-implementation-report-2026-09-29.md) · [第二轮](code-search-round2-report-2026-09-29.md) · [第三轮](code-search-round3-report-2026-09-29.md) · [第四轮](code-search-round4-report-2026-09-29.md) |
| 上下文与并行设计 | [压缩评测方案](context-compression-evaluation-plan.md) · [压缩预检修复](context-compaction-preflight-fix-20260927.md) · [并行方案](parallel-execution-plan-2026-09-29.md) · [循环保护设计](loop-guard-design.md) |
| CVM 阶段记录 | [阶段 0](cvm-stage0-acceptance-2026-10-10.md) · [阶段 1](cvm-stage1-acceptance-2026-10-11.md) · [阶段 2](cvm-stage2-acceptance-2026-10-11.md) · [后续方案](cvm-next-plan-2026-10-13.md) · [真实对照结果](cvm-step2-results-2026-10-13.md) |
| 工程对照 | [评测协议](engineering-ab-protocol-2026-10-13.md) · [结果记录](engineering-ab-results-2026-10-13.md) |
| 提示词与参考来源 | [优化策略](prompt-optimization-strategy.md) · [进展记录](prompt-optimization-progress.md) · [来源说明](source-citations.md) |

实验中的测试数量、速度、Token 和费用都有版本及样本限制。查看结果时同时阅读方法、失败样本和适用范围，不把单次验收当作当前版本的完整保证。
