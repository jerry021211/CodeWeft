"""LSP entry point with explicit availability and diagnostic freshness."""
import json

from codeagent.lsp import LspService
from codeagent.tools.base import ToolDefinition, parameter_error


class LspTool:
    definition = ToolDefinition('lsp',
        'Python、Java、TS/JS 语言服务器：definition 跳转定义、references 查找引用、diagnostics 文件诊断、sync 同步磁盘内容。'
        '按需启动已安装服务器；line/character 从1开始，character 使用 UTF-16 单位。服务器缺失或超时会明确报告。'
        '诊断 range 保留 LSP 的零起点；locations 使用1起点。',
        {'type': 'object', 'properties': {
            'operation': {'type': 'string', 'enum': ['definition', 'references', 'diagnostics', 'sync']},
            'file_path': {'type': 'string'}, 'line': {'type': 'integer'}, 'character': {'type': 'integer'},
        }, 'required': ['operation', 'file_path']}, effect='read', reentrant=True)

    def __init__(self, workspace, config=None):
        self.service = LspService(workspace, config)

    def bind_runtime(self, **kwargs):
        self.service.bind_runtime(**kwargs)

    def isolated_copy(self):
        return LspTool(self.service.guard.root, self.service.config)

    def close(self):
        self.service.close()

    def run(self, operation, file_path, line=1, character=1):
        try:
            return json.dumps(self.service.execute(operation, file_path, line, character), ensure_ascii=False)
        except ValueError as exc:
            return parameter_error(str(exc), 'lsp:parameters')
