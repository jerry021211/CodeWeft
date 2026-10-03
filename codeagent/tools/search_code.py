"""Read-only multi-language code retrieval tool."""
from codeagent.code_search.service import CodeSearch
from codeagent.tools.base import ToolDefinition, parameter_error


class SearchCodeTool:
    definition = ToolDefinition(
        name="search_code",
        description=("根据需求定位 Python、Java、TypeScript、JavaScript 等项目实现的搜索入口。直接输入用户的行为描述或符号名，"
                     "一次返回相关函数的路径、完整函数名、准确行号和连续源码，可直接据此判断实现位置。"
                     "支持中文需求，必要时最多一次模型扩展关键词。"
                     "keywords 可提供至多三组英文行为/标识符关键词，避免额外改写。path 可缩小目录范围。"
                     "默认遵守 Git 忽略规则；结果不保证相关或找全，零匹配不能证明未实现。未配置语法解析的语言使用明确标记的文本切块。"
                     "quote 最多20行；较长函数可继续 read_file。"),
        input_schema={"type": "object", "properties": {
            "query": {"type": "string", "description": "需求、行为描述或精确符号名"},
            "path": {"type": "string", "description": "工作区内目录或文件，默认 ."},
            "top_k": {"type": "integer", "description": "返回不同代码实体数，默认5，1至10"},
            "keywords": {"type": "array", "items": {"type": "string"}, "description": "可选，至多三组简短搜索词；原始 query 始终保留"},
        }, "required": ["query"]}, effect="read", reentrant=True)

    def __init__(self, workspace, index_dir, *, client=None, model=None, event_emitter=None,
                 embedding_provider=None, embedding_config=None):
        from codeagent.code_search.embedding import EmbeddingConfig
        self.embedding_config = embedding_config or EmbeddingConfig()
        self.service = CodeSearch(workspace, index_dir, client=client, model=model, event_emitter=event_emitter,
            embedding_provider=embedding_provider or self.embedding_config.create_provider(),
            max_vector_chunks=self.embedding_config.max_chunks)

    @property
    def workspace(self):
        return self.service.index.guard.root

    def isolated_copy(self):
        # Runtime callbacks, mutable index state and rewrite client belong to the
        # child. Do not share a parent's budgeted rewrite client across Agents.
        return SearchCodeTool(self.service.index.guard.root, self.service.index.directory,
            embedding_provider=self.service.embedding_provider, embedding_config=self.embedding_config)

    def bind_agent(self, client, model, emitter):
        self.service.client, self.service.model, self.service.emitter = client, model, emitter

    def bind_runtime(self, *, cancellation_check=None, remaining_seconds=None):
        self.service.bind_runtime(cancellation_check=cancellation_check, remaining_seconds=remaining_seconds)

    def run(self, query, path=".", top_k=5, keywords=None):
        try:
            return self.service.search(query, path, top_k, keywords)
        except ValueError as exc:
            return parameter_error(str(exc), "search_code:parameters")
