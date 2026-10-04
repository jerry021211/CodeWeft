"""Content search tool with regex support."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from codeagent.tools.base import ToolDefinition
from codeagent.tools.search_files import (
    PAGE_PROPERTIES, SearchFiles, matches_path, records_page, search_files,
    validate_page, validate_pattern,
)
from codeagent.tools.workspace import WorkspaceGuard
from codeagent.tools.output_limits import SEARCH_RECORDS, SEARCH_BODY_CHARS


@dataclass(frozen=True, slots=True)
class GrepTool:
    """Search file contents with regular expressions."""

    definition: ToolDefinition = ToolDefinition(
        name="grep",
        effect="read", reentrant=True,
        description=(
            "按正则搜索文件内容，返回路径、行号和匹配行，默认遵守 Git 忽略规则（含未提交的新文件）。"
            "每页默认1000条匹配行、正文最多32000字符，按路径和行号排序；has_more 时用 next_offset 继续。文件变化后从 offset=0 重搜。"
            "用 path/include 缩小范围；查忽略文件设 include_ignored=true。结论和修改需读取周边代码。"
            "scan_complete=false 时不能断言已搜遍；零匹配只表示本次范围未命中。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "搜索正则表达式",
                },
                "path": {
                    "type": "string",
                    "description": "搜索文件或目录，默认当前目录",
                },
                "include": {
                    "type": "string",
                    "description": "只搜索匹配该 glob 的文件，例如 *.py",
                },
                **PAGE_PROPERTIES,
            },
            "required": ["pattern"],
        },
    )
    workspace_guard: WorkspaceGuard | None = None

    def run(
        self, pattern: str, path: str = ".", include: str | None = None,
        offset: int = 0, limit: int = SEARCH_RECORDS, include_ignored: bool = False,
    ) -> str:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"Invalid regex: {exc}"

        try:
            validate_page(offset, limit)
            if include is not None:
                validate_pattern(include)
            if self.workspace_guard is not None:
                base = self.workspace_guard.resolve(path)
                if include is not None:
                    include = self.workspace_guard.validate_pattern(include)
            else:
                base = Path(path).expanduser().resolve()
        except Exception as exc:
            return f"Error: {exc}"
        if not base.exists():
            return f"Error: {path} not found"

        # An explicitly named file is an intentional scope, including ignored files.
        single_file = base.is_file()
        inventory = SearchFiles(paths=[base]) if single_file else search_files(
            base, self.workspace_guard, include_ignored=include_ignored,
        )
        matches: list[str] = []
        seen = 0
        has_more = False
        unreadable = 0
        for file_path in inventory.paths:
            if self.workspace_guard is not None and not self.workspace_guard.allows(file_path):
                unreadable += 1
                continue
            if not single_file and include is not None and not matches_path(
                file_path.relative_to(base), include, recursive=True,
            ):
                continue
            try:
                with file_path.open(encoding="utf-8", errors="ignore") as stream:
                    for line_number, line in enumerate(stream, 1):
                        match = regex.search(line.rstrip("\r\n"))
                        if not match:
                            continue
                        if seen < offset:
                            seen += 1
                            continue
                        if len(matches) == limit:
                            has_more = True
                            break
                        text = line.rstrip('\r\n')
                        prefix = f"{file_path}:{line_number}: "
                        if len(prefix) + len(text) > SEARCH_BODY_CHARS:
                            start = max(0, match.start() - 200)
                            end = min(len(text), start + max(1, SEARCH_BODY_CHARS - len(prefix) - 400))
                            text = (f'[match={match.start()}:{match.end()} fragment={start}:{end}; '
                                    f'read_file offset={line_number} char_offset={start}] ' + text[start:end])
                        matches.append(prefix + text)
            except OSError:
                unreadable += 1
                continue
            if has_more:
                break
        complete = not (has_more or unreadable or inventory.incomplete)
        notes = inventory.notes + ([f'Unreadable files: {unreadable}'] if unreadable else [])
        return records_page(matches, offset=offset, limit=limit, more=has_more, complete=complete, notes=notes)
