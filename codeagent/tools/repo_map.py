"""A local file overview: no model calls, embeddings, or inferred call graph."""
from pathlib import Path

from codeagent.tools.base import ToolDefinition, ToolOutput, parameter_error
from codeagent.tools.output_limits import SEARCH_BODY_CHARS
from codeagent.tools.output_pages import page
from codeagent.tools.search_files import search_files
from codeagent.tools.workspace import WorkspaceGuard


def annotate(path):
    name = path.name.lower()
    parts = {part.lower() for part in path.parts[:-1]}
    if (parts & {'tests', 'test', '__tests__', 'e2e'} or name.startswith('test_')
            or '.test.' in name or '.spec.' in name or name.endswith('_test.go')):
        return '测试'
    if name in {'main.py', '__main__.py', 'app.py', 'server.py', 'cli.py', 'main.go',
                'main.rs', 'main.ts', 'main.tsx', 'main.js', 'index.ts', 'index.tsx',
                'index.js', 'server.ts', 'server.js', 'app.tsx', 'program.cs'}:
        return '入口候选'
    if (name in {'package.json', 'pyproject.toml', 'cargo.toml', 'go.mod', 'pom.xml',
                 'build.gradle', 'tsconfig.json', 'dockerfile', '.env.example', '.gitignore'}
            or '.config.' in name or path.suffix in {'.yaml', '.yml', '.toml'}):
        return '配置'
    if name.startswith('readme') or path.suffix.lower() in {'.md', '.rst'}:
        return '文档'
    return ''


class RepoMapTool:
    definition = ToolDefinition(
        name='repo_map', effect='read', reentrant=True,
        description=('查看项目概览：精简目录树、文件大小，按路径/文件名标注入口候选、测试、配置和文档。'
                     '不读源码、不调用模型或embedding，不是调用关系图。未知项目先用depth=2，再用path下钻；'
                     '已知实现行为用search_code，已知多个文件用read_file(file_paths)。遵守与glob相同的文件过滤规则。'),
        input_schema={'type': 'object', 'properties': {
            'path': {'type': 'string', 'default': '.', 'description': '工作区内要概览的目录'},
            'depth': {'type': 'integer', 'minimum': 1, 'maximum': 8, 'default': 4},
            'max_files': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 200},
        }})

    def __init__(self, workspace_guard=None):
        self.workspace_guard = workspace_guard
        self.check = lambda: None

    def bind_runtime(self, *, cancellation_check=None, remaining_seconds=None):
        self.check = cancellation_check or (lambda: None)

    def isolated_copy(self):
        return RepoMapTool(self.workspace_guard)

    def run(self, path='.', depth=4, max_files=200):
        if (not isinstance(path, str) or not path.strip() or type(depth) is not int
                or not 1 <= depth <= 8 or type(max_files) is not int or not 1 <= max_files <= 1000):
            return parameter_error('path必须为目录；depth为1至8；max_files为1至1000。', 'repo_map:parameters')
        guard = self.workspace_guard or WorkspaceGuard(Path.cwd())
        try:
            root = guard.resolve(path)
            if not root.is_dir():
                return ToolOutput(f'Error: {path} is not a directory', status='error')
            inventory = search_files(root, guard, check=self.check)
            candidates = [p for p in inventory.paths if len(p.relative_to(root).parts) <= depth]
            # Root manifests and shallow entry points survive large nested trees.
            candidates.sort(key=lambda p: (len(p.relative_to(root).parts), not bool(annotate(p.relative_to(root))),
                                           p.as_posix().casefold(), p.as_posix()))
            selected = candidates[:max_files]
            omitted = len(inventory.paths) - len(selected)
            tree = {}
            for directory in inventory.directories:
                relative = directory.relative_to(root)
                if len(relative.parts) <= depth:
                    node = tree
                    for part in relative.parts:
                        node = node.setdefault(part, {})
            for candidate in selected:
                self.check()
                relative = candidate.relative_to(root)
                try:
                    size = guard.ensure_within(candidate).stat().st_size
                except (OSError, ValueError):
                    inventory.warning(f'Cannot inspect file: {relative.as_posix()}')
                    continue
                node = tree
                for part in relative.parts[:-1]:
                    node = node.setdefault(part, {})
                label = annotate(relative)
                node[relative.name] = f'{size} B' + (f' [{label}]' if label else '')
            lines = [f'{root}/ （标签仅按路径/文件名推断）']

            def walk(node, prefix=''):
                for name, child in sorted(node.items(), key=lambda item: (not isinstance(item[1], dict), item[0].casefold(), item[0])):
                    self.check()
                    lines.append(prefix + name + ('/' if isinstance(child, dict) else '  ' + child))
                    if isinstance(child, dict):
                        walk(child, prefix + '  ')

            walk(tree)
            footer = (f'\n范围内发现{len(inventory.paths)}个文件；选取{len(selected)}个，深度/数量限制省略{omitted}个。'
                      '\n目录较大或结果截断时，请用repo_map(path="子目录")下钻或glob定位。')
            if inventory.notes:
                footer += '\n' + '\n'.join(inventory.notes)
            complete = not inventory.incomplete

            def render(allowance):
                allowance = max(0, min(SEARCH_BODY_CHARS, allowance))
                shown, used = [], 0
                for line in lines:
                    if used + len(line) + 1 + len(footer) > allowance:
                        break
                    shown.append(line)
                    used += len(line) + 1
                clipped = len(shown) < len(lines)
                body = '\n'.join(shown) + footer if len(footer) <= allowance else ''
                result = page(ToolOutput(''), body, has_more=bool(omitted or clipped),
                              scan_complete=complete, source_complete=complete,
                              truncated_reason='overview_limit' if omitted or clipped else None)
                result.page_renderer = render
                result.source_text = '\n'.join(lines) + footer
                return result

            return render(SEARCH_BODY_CHARS)
        except (OSError, ValueError) as exc:
            return ToolOutput(f'Error: {exc}', status='error')
