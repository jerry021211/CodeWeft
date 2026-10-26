# 参与 CodeWeft 开发

欢迎提交可复现的问题、文档改进和代码修改。先阅读 [项目首页](README.md) 和 [架构说明](docs/architecture.md)，再从需要改动的模块及相邻测试开始。

## 准备开发环境

需要 Python 3.11+ 和 Git；开发 Web 界面推荐 Node.js 22.12+ 与 npm。以下命令在仓库根目录执行。

```bash
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[web]"
npm.cmd --prefix web ci
```

macOS / Linux：

```bash
source .venv/bin/activate
python -m pip install -e ".[web]"
npm --prefix web ci
```

仅修改核心 Python 包时可安装 `python -m pip install -e .`；涉及 Web 后端或完整测试时使用 `[web]`。模型配置见 [模型服务](docs/model-services.md)，不需要为了运行脚本化测试配置真实模型密钥。

## 启动本地开发

第一个终端启动后端：

```bash
python -m codeagent.web.cli --workspace . --port 8765 --reload
```

第二个终端启动前端：

```powershell
npm.cmd --prefix web run dev
```

打开 [http://127.0.0.1:5173](http://127.0.0.1:5173)。开发服务器将 `/api` 代理到本机 `8765`；React/样式保存后由 Vite 热更新，`codeagent/` 中的 Python 源码保存后自动重启后端。macOS / Linux 将 `npm.cmd` 换成 `npm`。

`--reload` 仅用于开发，默认关闭；重启会中断运行中的 Agent 任务，记录按原有持久化和恢复规则处理，不保证无损续跑。监听范围是 CodeWeft 的 `codeagent/` 源码目录，不是 `--workspace` 指定的业务项目，也不监听日志、数据库、`web/dist/` 或 `.env`。详情见 [开发热更新](docs/hot-reload.md)。

也可运行 `npm.cmd --prefix web run build`，由后端提供构建后的界面。该命令已包含 TypeScript 检查。

## 按改动选择验证

后端使用标准库 `unittest`。从相关文件开始，下面是可按模块选择的例子：

```bash
python -m unittest discover -s tests -p "test_tools.py"
python -m unittest discover -s tests -p "test_context_runtime.py"
python -m unittest discover -s tests -p "test_native_execution.py"
python -m unittest discover -s tests -p "test_web_api.py"
```

修改共享接口、权限、上下文或执行逻辑时，扩大到受影响的相邻测试；需要后端完整回归时运行：

```bash
python -m unittest discover -s tests
```

前端改动运行：

```powershell
npm.cmd --prefix web test
npm.cmd --prefix web run build
```

测试包含临时 SQLite、Git 工作目录和本地子进程。平台、依赖或进程权限导致跳过或失败时，提交中说明具体用例和原因，不把未运行的检查写成通过。行为修复应覆盖真实失败场景；纯文档修改检查命令、相对链接、锚点和页面展示即可。

常规验证使用脚本化客户端和本地夹具，不要求调用付费模型。需要评估真实模型时另行阅读 [评测指南](evals/README.md)，明确模型、费用与停止条件，并保留失败样本。离线测试通过只能证明实现和接线，不能用作真实模型效果的证据。

提交前检查改动范围和空白错误：

```bash
git diff --check
git diff --stat
```

## 代码和文档约定

- 沿用所在模块的命名、类型和测试风格，优先复用现有工具、事件、权限与持久化能力。
- 原生协作复用 `tasks`，共享模块不要反向依赖 `legacy/teams`；详见 [模块边界](docs/multiagent-layout.md)。
- 公开入口、配置或用户行为改变时，同步更新使用文档和相关测试；首页中英文保持相同事实。
- 当前说明放在 [文档导航](docs/README.md) 的使用或开发入口，带日期的设计与验收记录保留为历史，不替代当前指南。
- 不提交密钥、个人模型配置、运行数据库、私有工作区或评测原始请求。新增文件前检查内容和 `.gitignore`，评测产物默认放在忽略的 `eval-results/` 中。

## 提交 Issue 或 Pull Request

Issue 请提供版本或提交号、操作系统、最小复现步骤、预期与实际行为，以及脱敏后的错误信息。模型问题可补充协议和模型名称，勿附 API 密钥或完整私人会话。

Pull Request 说明解决的问题、修改后的行为和实际执行的验证；存在未验证部分时直接写明。保持改动聚焦，避免混入无关格式调整、运行产物或本地配置。新功能和较大改动可以先在 [Issues](https://github.com/jerry021211/CodeWeft/issues) 讨论范围。
