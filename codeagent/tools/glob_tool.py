"""File pattern matching tool."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codeagent.tools.base import ToolDefinition
from codeagent.tools.search_files import (
    PAGE_PROPERTIES, matches_path, page_footer, search_files, validate_page, validate_pattern,
)
from codeagent.tools.workspace import WorkspaceGuard


@dataclass(frozen=True, slots=True)
class GlobTool:
    """Find files matching a glob pattern."""

    definition: ToolDefinition = ToolDefinition(
        name="glob",
        effect="read", reentrant=True,
        description=(
            "按 glob 模式定位路径，支持 ** 递归匹配，例如 **/*.py。默认遵守 Git 忽略规则（含未提交的新文件）。"
            "每页默认 100 条，按路径稳定排序；has_more 时用 next_offset 继续，文件变化后从 offset=0 重搜。"
            "查旧副本设 include_ignored=true。scan_complete=false 表示有目录未搜到。未知路径时先定位，避免连续猜测。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "glob 模式，例如 **/*.py 或 src/**/*.ts",
                },
                "path": {
                    "type": "string",
                    "description": "搜索目录，默认当前目录",
                },
                **PAGE_PROPERTIES,
            },
            "required": ["pattern"],
        },
    )
    workspace_guard: WorkspaceGuard | None = None

    def run(
        self, pattern: str, path: str = ".", offset: int = 0,
        limit: int = 100, include_ignored: bool = False,
    ) -> str:
        try:
            validate_page(offset, limit)
            validate_pattern(pattern)
            if self.workspace_guard is not None:
                base = self.workspace_guard.resolve(path)
                pattern = self.workspace_guard.validate_pattern(pattern)
            else:
                base = Path(path).expanduser().resolve()
            if not base.is_dir():
                return f"Error: {path} is not a directory"

            inventory = search_files(base, self.workspace_guard, include_ignored=include_ignored)
            candidates = set(inventory.paths) | inventory.directories
            directories_only = pattern.endswith(("/", "\\"))
            hits = sorted(
                (candidate for candidate in candidates
                 if (not directories_only or candidate in inventory.directories)
                 and matches_path(candidate.relative_to(base), pattern)),
                key=lambda p: (str(p).casefold(), str(p)),
            )
            total = len(hits)
            shown = hits[offset:offset + limit]
            output = [str(hit) for hit in shown] or ["No files matched on this page."]
            output.append(page_footer(
                offset=offset, count=len(shown), has_more=total > offset + len(shown),
                complete=not inventory.incomplete,
            ))
            output.extend(inventory.notes)
            return "\n".join(output)
        except Exception as exc:
            return f"Error: {exc}"
