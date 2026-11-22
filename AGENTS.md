# 项目维护约定

- 修改前先理解相关入口、调用链、数据流和相邻测试，不凭单个报错直接打补丁。
- Bug 修复保持小范围，避免无关重构、格式整理和顺带改动。
- 优先修复通用机制，覆盖同类场景；不要为单个案例堆叠特殊分支或提示词。
- 保持既有接口和其他功能的行为，复用项目已有抽象，不重复建设。
- 不用叠加运行时提示词替代代码层面的根因修复。
- 保留工作区已有改动；按实际影响范围运行回归测试，说明未验证的边界。
- 必须扩大修改范围时，先说明根因、影响面及为什么局部修复不足。

## Comments and GitHub sync

- Write new or revised code comments and Git commit messages in English.
- Follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/). Start the title with a lowercase type and an optional component scope, for example `fix(mobile): keep paired devices connected after restart`. Use `feat` for new features, `fix` for bug fixes, and types such as `docs`, `refactor`, `perf`, or `test` for their respective changes. Mark breaking changes with `!` before the colon or a `BREAKING CHANGE:` footer.
- After each completed change, run the relevant checks and stage only the files or hunks that belong to the task.
- Commit and push the change to the current branch on `origin` as part of the same task. Verify the remote commit and use its full tagged title as the GitHub link text in the sync summary.
