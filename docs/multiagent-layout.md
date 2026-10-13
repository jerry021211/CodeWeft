# 多 Agent 目录整理

[文档导航](README.md) · [项目架构](architecture.md) · [当前协作指南](collaboration.md)

本文说明模块依赖与目录迁移；末尾验收记录对应注明的版本和日期。新用户使用原生协作，旧 Team 的兼容代码仍保留。

## 当前结构

```text
codeagent/
  tasks/                    原 Task、依赖和执行配置
  multiagent/               原生委派、调度、执行日志、恢复、验证与交付
  runtime/validation.py     共享命令执行、取消和诊断
  worktrees/                共享 Git 快照、路径规则、文件校验
  legacy/teams/             旧 Team 的历史兼容与执行实现
  web/                     Web 宿主，同时接入新执行与旧记录
web/src/features/multiagent/
                            原生执行图、成员详情和状态核对
```

## 清理内容

原 `codeagent/teams/` 的仍在使用的代码移到 `legacy/teams/`。
原 `runtime/background.py` 和旧 Attempt Worktree manager 也归入旧 Team，
不再由通用 runtime / worktrees 包隐式导出。旧 Team 的四个存储/观测模块随之集中。
删除未使用的 `BackgroundTaskRunner` 别名、旧模块位置和重复的验证/文件校验实现；
所有应用调用点和测试直接使用新位置，不保留转发文件。

原生验证不再构造 `CandidateService(None, None)`；改用独立 `ValidationRunner`。
原生文件交付不再引用 `IntegrationService` 的私有方法；新旧执行共用 `worktrees/files.py`。
绑定成员工作目录的方法统一称为 `for_execution_workspace`，不再暗示所有成员属于旧 Team。

## 保留边界

旧 Team 的 HTTP 接口、执行/审批/恢复、SQLite 表、历史记录和工作现场仍可用。
`web/storage.py` 中仍有旧 Team 方法；Web factory/scheduler/API 仍是兼容接入点。
因此，这是依赖解耦和结构整理，不是宣布旧 Team 已完全退役。

普通 Agent 的模型客户端、提示词、工具 schema、上下文和缓存算法不变；
原生三条执行入口及四个管理工具也保持原协议。工具接口精简应另做迁移，
避免目录整理同时改变检查点和缓存前缀。

没有读取或修改用户运行数据库，没有删除历史任务、Worktree 或生成文件。

## 验证

`tests/test_multiagent_boundaries.py` 约束原生与共享模块不能导入旧 Team，
并在独立 Python 进程中核对实际加载模块。共享命令测试在
`tests/test_validation_runtime.py`；旧 Team 的验证计划测试仍在
`tests/test_team_validation.py`。

其他回归覆盖原生委派/计划/恢复、旧 Team 的集成和恢复、普通 Agent、Task、
权限、事件、Web API、缓存前缀、前端组件测试及 TypeScript/Vite 构建。

本次验证记录（2026-10-14，以下分组包含重复用例，不相加作为唯一测试数）：

- 新协作、普通 Agent、Task、Plan Mode、缓存、事件和 Web：195 项通过。
- 新执行与旧 Team 大组：执行 239 项，1 项条件跳过。初跑有一个 LSP 用例因
  沙箱拒绝加载 Anaconda `_ctypes` DLL 失败并产生临时目录清理错误；同一用例
  在正常权限环境复核通过。另一个旧观察器测试误把所有新增表都算作必须观察的表，
  已按原实现的显式允许列表校正覆盖断言，观察器 13 项复核通过。
- 共享验证、模块边界、推理设置和代码智能：35 项在正常权限环境通过。
- 最后复核旧 Team 授权/取消、共享验证和模块边界：27 项通过。
- 前端：120 项通过；TypeScript + Vite 构建通过，保留既有的 bundle 大于 500 kB 提示。
- Python 源码语法检查、`git diff --check` 通过。

未调用付费模型重新运行真实长任务，也未变更用户的运行数据库。
