"""Source-reviewed local questions; inspired by RepoQA/CodeSearchNet, not copied.

References are private grader inputs. Only id/query/split/category reach the agent.
Needles identify minimal behavioral evidence inside an AST-resolved function.
"""

SOURCES = {
    "repoqa": "https://github.com/evalplus/repoqa#-search-needle-function-snf",
    "codesearchnet": "https://github.com/github/CodeSearchNet#evaluation",
    "beir": "https://github.com/beir-cellar/beir",
    "coderag": "https://aclanthology.org/2025.findings-naacl.176/",
}


def ref(path, symbol, *needles):
    return {"path": "codeagent/" + path, "symbol": symbol, "needles": list(needles)}


def case(identifier, category, query, *targets, split="test", rationale=""):
    return {"id": identifier, "split": split, "category": category, "query": query,
            "groups": [[target] for target in targets], "rationale": rationale,
            "method": "repoqa" if category == "natural" else "local_extension" if category in {"cross_file", "absent"} else "codesearchnet"}


CASES = [
    case("E01", "exact", "定位 tool_schema_hash：工具列表换了排列顺序时，哪里保证得到的摘要保持一致？请给出实际计算位置。",
         ref("tools/registry.py", "tool_schema_hash", "ordered = sorted", "hashlib.sha256")),
    case("E02", "exact", "定位 retry_delay_seconds：服务已经给出 retry_after 时，哪里优先采用这个等待时长？",
         ref("recovery/backoff.py", "retry_delay_seconds", "if retry_after is not None:", "return max(0.0, retry_after)")),
    case("E03", "exact", "定位 _public_tool_name：外部工具的公开名称在哪里加入服务前缀、替换不允许的字符并限制长度？",
         ref("mcp/router.py", "_public_tool_name", "safe = re.sub", "return safe[:64]")),
    case("E04", "exact", "定位 RuntimeDataPaths.workspace_id：同名但位于不同路径的项目在哪里得到不同的数据目录标识？",
         ref("runtime/data_paths.py", "RuntimeDataPaths.workspace_id", "os.path.normcase", "hashlib.sha256")),
    case("N01", "natural", "模型提交计划时同时标记了两件正在做的事。请定位拒绝这种计划的输入检查，并给出判断依据。",
         ref("tools/todo.py", "normalize_todos", "if in_progress_count > 1:", "only one todo can be in_progress")),
    case("N02", "natural", "调试数据是多层字典和列表，里面可能藏着密钥。哪里负责递归处理这些值，把敏感字段替换掉，而不是只处理最外层？",
         ref("events/redaction.py", "_redact", "if _is_sensitive_key(name)", "else _redact(item, secrets=secrets, depth=depth + 1)")),
    case("N03", "natural", "旧对话里留下了工具调用，却没保存执行结果。哪里会补一条结果未知的错误占位记录，使对话可以恢复，而不直接重放那个工具？",
         ref("messages.py", "reconcile_tool_history", '"tool_use_id": value, "is_error": True', 'history.insert(index + 1')),
    case("N04", "natural", "模型接口拒绝了身份认证时，程序不应继续盲目重试。请定位把这类响应归为不可重试错误的位置。",
         ref("recovery/classifier.py", "classify_exception", 'status in {401, 403}', "return RecoveryReason.NON_RETRYABLE_ERROR")),
    case("N05", "natural", "准备把选中的长期记忆放进本轮输入时，如果一条记忆太长装不下，哪里会整条跳过并继续尝试后面的记录？",
         ref("memory/manager.py", "MemoryManager._load_selected_context", "if cost > remaining:", '"reason": "budget"')),
    case("N06", "natural", "外部工具没有返回普通文字，只给了结构化对象。哪里会把这个对象转成可交给模型阅读的文本？",
         ref("mcp/router.py", "_tool_result_text", "structured_json = result.structured_content is not None", "json.dumps(payload, ensure_ascii=False)")),
    case("N07", "natural", "只读权限下，模型要求执行一串带管道或重定向的命令。请定位在命令运行前拒绝这类组合语法的检查函数。",
         ref("permissions/read_only.py", "is_safe_read_only_command", "_SHELL_SYNTAX.search(command)")),
    case("N08", "natural", "升级时需要搬入旧运行数据，但目标位置已有数据就不能覆盖，而且源目录不能夹带符号链接。请定位负责这些检查和复制的实现。",
         ref("runtime/data_paths.py", "RuntimeDataPaths.import_legacy_directory", "if destination.exists() or not source.is_dir():", "shutil.copytree(source, destination)")),
    case("C01", "cross_file", "某个工具执行时抛出了异常。请分别定位：把异常变成工具返回文本的地方，以及根据这种文本标记执行失败的地方。两个环节都要找。",
         ref("tools/registry.py", "ToolRegistry.execute", 'output = f"Error: {type(exc).__name__}: {exc}"'),
         ref("tools/base.py", "normalize_tool_output", 'elif text.startswith(("Error:"', 'status = "error"')),
    case("C02", "cross_file", "运行事件送到记录器之前，哪里接入数据清洗？清洗后的内容如果仍然过大，哪里将它改成带原始大小的截断预览？请给出这两个文件中的实现。",
         ref("events/sink.py", "EventEmitter.emit", "payload=redact_payload(payload or {})"),
         ref("events/redaction.py", "redact_payload", '"original_bytes": len(encoded)', '"preview": preview')),
    case("C03", "cross_file", "接口返回了要求稍后再试的响应头。请分别找到提取等待秒数的位置，以及最终优先使用这个秒数而非自行计算退避时间的位置。",
         ref("recovery/classifier.py", "retry_after_seconds", 'getter("retry-after")', "return max(0.0, float(value))"),
         ref("recovery/backoff.py", "retry_delay_seconds", "if retry_after is not None:", "return max(0.0, retry_after)")),
    case("C04", "cross_file", "模型选记忆时可能编造文件名，磁盘上的记忆也可能在选择期间被修改。请分别定位：拒绝非候选文件名、加载原文时核对版本并跳过变化记录、以及根据记忆内容生成版本标识的实现。",
         ref("memory/manager.py", "MemoryManager._select_memory_filenames", "if filename not in valid or filename in filenames:"),
         ref("memory/manager.py", "MemoryManager._load_selected_context", "version(record) != expected_versions.get(filename)", '"reason": "changed_since_retrieval"'),
         ref("memory/retrieval.py", "version", "hashlib.sha256", "asdict(record)")),
    case("Z01", "absent", "当前项目是否内置了向 Redis 申请分布式锁来防止任务被多台机器重复领取的实现？如果有，定位实际连接和加锁代码；如果没有，明确说明。", rationale="源码未出现 Redis 客户端/协议实现；不能把本机线程锁或 SQLite 事务当作 Redis 分布式锁。"),
    case("Z02", "absent", "当前项目是否内置了连接 LDAP 服务器校验用户名和密码的登录实现？请定位实际连接与认证代码，没有就明确说明。", rationale="未发现 LDAP 客户端导入、连接或 bind 实现；外部 MCP 的潜在能力不算内置实现。"),
    case("Z03", "absent", "当前项目是否内置了把源代码上传到 Amazon S3 存储桶的实现？请定位实际上传调用，没有就明确说明。", rationale="未发现 boto3/S3 上传调用；本地归档不能作为 S3 上传实现。"),
    case("Z04", "absent", "当前项目是否内置了连接 Elasticsearch 并查询其索引来搜索代码的实现？请定位实际客户端和查询调用，没有就明确说明。", rationale="未发现 Elasticsearch/OpenSearch 客户端与查询实现；SQLite 记忆索引不是 Elasticsearch 代码搜索。"),
    case("D01", "natural", "等待用户回答期间取消运行，哪里检查取消并清理等待中的问题？",
         ref("web/questions.py", "WebUserQuestions.ask", "self.cancellation.raise_if_cancelled()", "self.repository.cancel_user_question(run_id, question_id)"), split="dev"),
    case("D02", "exact", "read_file 的 offset 从 1 起算。定位流式跳过起始行之前内容的代码。",
         ref("tools/read.py", "_page", "line = 1", "while line < offset:"), split="dev"),
    case("D03", "natural", "文件路径可能通过符号链接绕到工作区外，哪里解析实际路径并检查仍在工作区里？",
         ref("tools/workspace.py", "WorkspaceGuard.ensure_within", "Path(path).expanduser().resolve()", "candidate.relative_to(self.root)"), split="dev"),
    case("D04", "natural", "提供方没报告 token 用量时，哪里记录一次用量未知的调用，并保留已有累计数？",
         ref("events/models.py", "UsageTracker.record", "if not usage.available:", "unavailable_calls=current.unavailable_calls + 1"), split="dev"),
]

ABSENCE_PATTERNS = {
    "Z01": r"\b(redis|aioredis|redlock)\b",
    "Z02": r"\b(ldap|ldap3|ldap_initialize)\b",
    "Z03": r"\b(boto3|aioboto3|botocore|s3transfer|upload_fileobj|put_object)\b",
    "Z04": r"\b(elasticsearch|opensearch|opensearchpy)\b",
}
