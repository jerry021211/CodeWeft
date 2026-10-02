# Team 自动集成与本地交付

新建 Team 默认 `managed`。现有数据库里的 Team 保留 `manual`；API 也可显式创建 manual Team。
仍需原有 Team Plan 审批、高风险 Attempt Plan 审批和 Candidate 语义审查。
正常集成、组合验证、下游启动和最后回写由 Runtime 自动执行。

## 本地基线和版本

创建时从当前本地 HEAD 与工作目录生成内部快照 S，包含已跟踪文件的修改/删除和未忽略的新文件。
临时 index 与 `commit-tree` 不改变用户 HEAD 或暂存区。内部 Git commit 不是 GitHub 提交：整个流程不 fetch/pull/push。
新 Team 的显式 baseCommit 必须解析为当前 HEAD；旧提交应另建 checkout 后启动 Team。

- `team.base_commit`：不可变的本地快照 S。
- `team.integration_head` / `integration_revision`：当前已验证版本 Cn。
- `attempt.attempt_base_commit` / `base_integration_revision`：认领事务固定的版本。
- `candidate.team_integrated_revision`：内部集成版本，与旧人工交付的 `integrated_at` 分开。

代码依赖只有在有效 Candidate 已集成后才满足；分析依赖沿用报告完成条件。
领取记录保存消费的上游 Candidate，调度展示和认领使用同一判断。
例如 A/D 都从 C0 开始，A 先集成成 C1，依赖 A 的 B 从 C1 启动；D 保留 C0，完成后再与最新版本组合验证。
恢复旧 Attempt 时核对版本是否属于 Team 已发布历史，不要求它等于最新版本。
Lead/分析 Agent 读取固定的集成快照；Candidate 审查读取对应候选。

## 集成和交付

Candidate 通过 Lead 审查及原有验证并 commit 后进入队列。
每个 Team 的操作通过 OS 文件锁串行执行；成员开发继续并行，集成也计入 Supervisor 并发上限。
Runtime 在新的临时 Worktree 中 merge 确切的候选 SHA，运行以下去重后的命令：

1. Git 空白错误检查。
2. Team mandatory_validation_commands。
3. 批准方案的 integration_validation_commands。
4. 已集成任务与当前候选任务的 validation_commands。

命令必须针对冻结的试合并 SHA，通过后还要核对 HEAD、文件树、Team 状态和批准方案版本。
请在方案里填写项目实际的构建/测试命令；仅有 Git 检查不能证明业务正确。
测试不得改动参与版本的文件；生成文件应按项目约定忽略。

发布先记录 SQLite 意图，再 CAS 推进本地 `codex/<team>/integration` 分支，最后确认数据库版本。
进程在 Git 更新后退出时，重启会核对同一 SHA 并补齐记录，不重复集成。
全部计划任务完成/取消且所有有效候选已集成后，进入自动本地交付：

- 以最初快照 S 为共同祖先，将当前本地文件 L 与 Team 成果 T 三方合并。
- 在临时 Worktree 再次验证实际将回写的组合结果。
- 记录文件前后内容哈希、备份、用户 HEAD/index；核对主目录没有同期变化。
- 按文件原子替换/删除，留下未暂存修改；用户原有暂存内容不变，HEAD 不变。
- 全部核对成功后 Team 才标记 completed。用户继续本地测试，再自行 commit/push。

## 失败与恢复

`team_integrations` 保存候选 SHA、父版本、试合并 SHA、命令、日志、状态和交付恢复清单。
界面显示每个操作与验证结果，可查看当前版本相对初始本地快照的累计差异，观察接口可检查日志记录。

| 状态 | 行为 |
| --- | --- |
| conflicted / validation_failed | 不推进版本、不解锁代码依赖；保留现场和日志 |
| interrupted | 操作停止且未发布/回写，可重试 |
| publishing | 根据持久化意图自动核对 Git 与 SQLite |
| applying | 重启后只接受文件精确处于已记录 before/after 状态，再继续回写 |
| recovery_required | 暂停该 Team 后续集成；用户检查旧进程和现场后，界面“检查并恢复”仍会重新校验 |

Lead 的 `team_resolve_integration` 可以查看日志、重试，或将失败候选标记为 superseded，
在原任务范围内创建替代 Attempt。新 Attempt 从当前集成版本开始，并获得旧候选 SHA、差异引用和失败原因。
这不允许扩大任务范围或绕过原有审批和 Attempt 重试上限。
本地用户编辑与成果发生冲突时，Lead 保留两边并告知用户，由用户处理后重试。
结果不明、验证超时、回写过程中出现新编辑时，不自动覆盖；明确检查后仍不匹配则继续保留现场。

当前自动回写覆盖普通文件新增、修改、删除、二进制和普通重命名（作为删除/新增处理）。
遇到符号链接、junction、子模块或文件/目录类型转换会停止并保留现场，需先处理这些路径。
文件替换逐个原子执行，但整个目录不是单次原子事务；恢复清单用于识别部分回写。
未忽略的本地文件会进入快照；忽略的依赖目录不会自动复制到 Worktree，验证环境需按项目命令准备。

## 验证

`tests/test_team_managed_integration.py` 使用临时 Git 仓库，覆盖依赖可见性、并行旧基线集成、失败阻断、
发布中断恢复、旧版本 Attempt 恢复、本地同期编辑、HEAD/index 保留、部分回写恢复和 Supervisor 自动调度。
没有修改、提交或推送用户源仓库的历史。
