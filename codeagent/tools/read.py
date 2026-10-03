"""File reading tool with line numbers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path

from codeagent.tools.base import ToolDefinition, ToolOutput, parameter_error
from codeagent.tools.workspace import WorkspaceGuard


@dataclass(frozen=True, slots=True)
class ReadFileTool:
    """Read a file's contents with 1-based line numbers."""

    definition: ToolDefinition = ToolDefinition(
        name="read_file",
        effect="read", reentrant=True,
        description=(
            "读取文件并返回行号；修改前读取相关上下文。用 offset/limit 限定行段，截断时按需继续读取。"
            "引用代码时，选取能说明行为的连续原文，去掉行号前缀；报告该片段第一行实际显示的行号，"
            "不要用函数定义行、搜索命中行或读取起点代替。提交前按末行号−首行号+1核对用户要求的引用行数上限；"
            "若超限，选取更短且仍含关键行为的连续片段，勿拼接省略中间行。"
            "相同文件和范围的重复读取可能返回当前上下文内的原文引用。"
            "用户要求重新提供原文、或需要再次展开该范围时，设置 force_full=true；仍受输出预算限制。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件路径",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "起始行号，从 1 开始；默认 1。",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "最多读取行数，默认 2000。",
                },
                "force_full": {
                    "type": "boolean",
                    "description": "禁用本次重复读取短引用，返回所请求范围的原文；默认 false。",
                },
            },
            "required": ["file_path"],
        },
    )
    workspace_guard: WorkspaceGuard | None = None

    def run(self, file_path: str, offset: int = 1, limit: int = 2000, force_full: bool = False) -> str:
        if type(offset) is not int or offset < 1 or type(limit) is not int or limit < 1:
            return parameter_error("offset 和 limit 必须为正整数。", "read_file:positive_range")
        if type(force_full) is not bool:
            return parameter_error("force_full 必须为布尔值。", "read_file:force_full:boolean")
        try:
            path = (
                self.workspace_guard.resolve(file_path)
                if self.workspace_guard is not None
                else Path(file_path).expanduser().resolve()
            )
            if not path.exists():
                return f"Error: {file_path} not found"
            if not path.is_file():
                return f"Error: {file_path} is a directory, not a file"

            # Always execute the real read after the ordinary permission checks.
            # Neither stat equality nor a cached tool return proves byte identity.
            path_before = _stamp(path.stat())
            with path.open("rb") as handle:
                before = _stamp(os.fstat(handle.fileno()))
                raw = handle.read()
                after = _stamp(os.fstat(handle.fileno()))
            current = _stamp(path.stat())
            text = raw.decode("utf-8", errors="replace")
            lines = text.splitlines()
            total = len(lines)

            start = max(0, offset - 1)
            chunk = lines[start : start + limit]
            numbered = [f"{start + index + 1}\t{line}" for index, line in enumerate(chunk)]
            result = "\n".join(numbered)

            if total > start + limit:
                result += (
                    f"\n... ({total} lines total, "
                    f"showing {start + 1}-{start + len(chunk)})"
                )
            output = ToolOutput(result or "(empty file)")
            # Windows stat/fstat can expose different ctime semantics. Compare
            # each clock to itself, and identity/size/mtime across the two APIs.
            if (before == after and path_before == current and after[:4] == current[:4]
                    and len(raw) == after[2]):
                output.file_read_snapshot = {
                    "version": 1, "path": str(path), "file_hash": hashlib.sha256(raw).hexdigest(),
                    "file_stamp": current, "offset": offset, "limit": limit,
                    "force_full": force_full,
                    "output_hash": hashlib.sha256(str(output).encode("utf-8")).hexdigest(),
                }
            return output
        except Exception as exc:
            return f"Error: {exc}"


def _stamp(info: os.stat_result) -> list[int]:
    # Including identity and timestamps also invalidates change-and-revert reads.
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
