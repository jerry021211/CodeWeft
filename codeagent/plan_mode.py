"""Planning capabilities, deliberately absent from ordinary Agent requests."""
import copy
import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from codeagent.tools.base import ToolDefinition


PLAN_READERS = frozenset({'read_file', 'repo_map', 'glob', 'grep', 'search_code',
    'web_search', 'load_skill', 'search_memory', 'load_memory', 'load_tool_output',
    'load_context_history', 'compact', 'TaskGet', 'TaskList', 'ask_user',
    'inspect_project', 'project_diff', 'subagent_result', 'subagent_cancel'})

PLAN_REMINDER = """[Plan Mode]
当前只进行调查和方案设计。禁止修改项目源码、运行任意 shell、安装依赖或初始化 Git。
使用 inspect_project 检查目录、Git 和环境，使用项目读取工具调查。
使用 plan(action="update", title=..., markdown=...) 增量保存可预览的 Markdown 草稿。
方案应包含需求与范围、现状依据、实现步骤、验证方式和待确认假设；不要将推测写成已验证事实。
单 Agent 使用 plan(action="submit", ...) 提交；Team 先创建 pending Tasks，声明 kind、write_scopes、
risk_level、plan_required、validation_commands，然后调用 TeamPlanSubmit 提交同一份方案。
TeamPlanSubmit 在本模式仅保存方案，不创建 Git 快照；baseCommit 可省略。不要探查 .git/HEAD。
新 Team 缺少本地 Git 仓库或初始提交时，用户批准后由 Runtime 自动准备；无需因此阻塞方案或安排初始化任务。
提交后结束本轮，等待用户在方案预览中批准。不能自行批准或退出 Plan Mode。
仅可使用 access="read_only" 的子 Agent 做调查，子 Agent 不得改变方案或权限。
用户的修改意见仍属于规划；批准和启动由 Runtime 负责。
"""


class InspectProjectTool:
    definition = ToolDefinition('inspect_project', 'Inspect local project, Git state and runtime versions without shell scripts. A non-Git/empty directory is a normal result.',
                                {'type': 'object', 'properties': {}}, effect='read', reentrant=True)

    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.check = lambda: None

    def bind_runtime(self, cancellation_check, remaining_seconds):
        self.check = cancellation_check

    def _command(self, command):
        self.check()
        executable = shutil.which(command[0])
        if not executable:
            return None
        # No shell expansion or project scripts. Poll so cancellation reaches probes.
        with subprocess.Popen([executable, *command[1:]], cwd=self.workspace,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
            try:
                for _ in range(30):
                    self.check()
                    try:
                        out, _ = process.communicate(timeout=0.1)
                        return out.decode('utf-8', errors='replace').strip() if process.returncode == 0 else None
                    except subprocess.TimeoutExpired:
                        continue
                return None
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def run(self):
        from itertools import islice
        entries = sorted(p.name for p in islice(self.workspace.iterdir(), 201))
        root = self._command(['git', 'rev-parse', '--show-toplevel'])
        manifests = [p for p in ('package.json', 'pyproject.toml', 'Cargo.toml', 'go.mod', 'pom.xml')
                     if (self.workspace / p).is_file()]
        return json.dumps({'workspace': str(self.workspace), 'empty': not entries,
            'entries': entries[:200], 'entries_truncated': len(entries) > 200, 'manifests': manifests,
            'git': {'available': root is not None, 'root': root,
                    'head': self._command(['git', 'rev-parse', '--verify', 'HEAD']) if root else None},
            'versions': {name: self._command([name, '--version']) for name in ('git', 'node', 'python')}}, ensure_ascii=False)


class ProjectDiffTool(InspectProjectTool):
    definition = ToolDefinition('project_diff', 'Read a bounded local Git diff; no external diff or text conversion helpers.',
        {'type': 'object', 'properties': {'staged': {'type': 'boolean'}}}, effect='read', reentrant=True)

    def run(self, staged=False):
        output = self._command(['git', '--no-pager', 'diff', '--no-ext-diff', '--no-textconv',
                               *(['--cached'] if staged else [])])
        return json.dumps({'diff': (output or '')[:40000], 'available': output is not None,
                           'truncated': len(output or '') > 40000}, ensure_ascii=False)


def task_snapshot(repository, task_list_id, plan):
    return [repository.get_task_resource(task_list_id, str(task['task_id'])).to_dict(camel_case=True)
            for task in plan['tasks']]


def source_fingerprints(workspace, paths):
    """Recheck only files actually investigated, following the workspace guard."""
    from codeagent.tools.workspace import WorkspaceGuard
    guard = WorkspaceGuard(workspace)
    result = {}
    for name in list(paths)[:200]:
        path = guard.resolve(name)
        name = path.relative_to(guard.root).as_posix()
        if not path.exists():
            result[name] = None
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(128 * 1024), b''):
                    digest.update(chunk)
            result[name] = digest.hexdigest()
    return result


class PlanTool:
    definition = ToolDefinition('plan', 'Save, read or submit the current versioned Markdown plan. This tool cannot approve a plan or grant execution permissions.',
        {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['get', 'update', 'submit']},
            'title': {'type': 'string'}, 'markdown': {'type': 'string'}}, 'required': ['action']})

    def __init__(self, repository, conversation_id, run_id, *, read_only=False, emitter=None):
        self.repository, self.conversation_id, self.run_id = repository, conversation_id, run_id
        self.read_only, self.emitter = read_only, emitter
        self.on_submit = lambda: None
        self.source_files = lambda: {}

    def run(self, action, title='', markdown=''):
        state = self.repository.planning_state(self.conversation_id)
        current = self.repository.get_planning_revision(state['active_plan_id']) if state['active_plan_id'] else None
        if action == 'get':
            return json.dumps(current, ensure_ascii=False)
        if action not in {'update', 'submit'}:
            raise ValueError('Unknown plan action')
        if action == 'submit' and state['target'] == 'team':
            raise ValueError('Use TeamPlanSubmit to include the verified task DAG in the submitted plan')
        return self.save(title or (current or {}).get('title', ''), markdown or (current or {}).get('markdown', ''),
                         {'target': state['target']}, submit=action == 'submit')

    def save(self, title, markdown, payload, *, submit):
        if submit and len(markdown.strip()) < 40:
            raise ValueError('请提供包含具体步骤和验证方式的完整方案，不能提交空白或仅标题的草稿')
        if submit:
            payload = {**payload, 'source_files': self.source_files()}
        result = self.repository.save_planning_revision(self.conversation_id, run_id=self.run_id,
            title=title, markdown=markdown, payload=payload, submit=submit, read_only=self.read_only)
        if self.emitter:
            self.emitter.emit('plan.updated', {'plan_id': result['id'], 'revision': result['revision'], 'status': result['status']})
        if submit:
            self.on_submit()
        return json.dumps({'plan_id': result['id'], 'revision': result['revision'], 'status': result['status'],
                           'message': '方案已保存，可在预览面板查看。' + ('等待用户批准后执行。' if submit else '')}, ensure_ascii=False)


class PlannedTeamSubmitTool:
    """Reuse Team validation, defer every Git/scheduling effect until approval."""
    def __init__(self, original, plan_tool):
        self.original, self.plan_tool = original, plan_tool
        schema = copy.deepcopy(original.definition.input_schema)
        schema['required'].remove('baseCommit')
        schema['properties']['baseCommit']['description'] = 'Optional existing Team baseline; new Teams resolve HEAD at execution preparation.'
        schema['properties'].update({'title': {'type': 'string'}, 'markdown': {'type': 'string'}})
        self.definition = replace(original.definition, input_schema=schema,
            description='Submit the previewable Team plan and its existing pending task DAG for one user approval. No Git or Worktree operations occur now. Save prose with plan first, or provide markdown here.')

    def run(self, title='', markdown='', **kwargs):
        repository = self.plan_tool.repository
        if not 1 <= kwargs['teammateCount'] <= kwargs.get('maxTeammates', 3) <= 8:
            raise ValueError('Invalid teammate counts')
        self.original._validated_plan(kwargs['plan'], kwargs.get('baseCommit', 'HEAD'))
        repository.validate_team_plan_tasks(self.original.task_list_id, kwargs['plan'], require_validation_commands=True)
        state = repository.planning_state(self.plan_tool.conversation_id)
        current = repository.get_planning_revision(state['active_plan_id']) if state['active_plan_id'] else None
        snapshots = task_snapshot(repository, self.original.task_list_id, kwargs['plan'])
        active_team = repository.get_active_team_run_for_conversation(self.plan_tool.conversation_id)
        title = title or (current or {}).get('title') or '团队实施方案'
        markdown = markdown or (current or {}).get('markdown') or ('# ' + title + '\n\n' +
            '\n\n'.join('## ' + item['task']['subject'] + '\n\n' + item['task']['description'] for item in snapshots))
        return self.plan_tool.save(title, markdown, {'target': 'team', 'team_input': kwargs,
            'task_list_id': self.original.task_list_id, 'task_snapshot': snapshots,
            'existing_team_id': active_team.id if active_team else None}, submit=True)


class PlanModeGate:
    def __init__(self, repository, conversation_id, *, team=False):
        self.repository, self.conversation_id, self.team = repository, conversation_id, team

    def check(self, name, args):
        if name in PLAN_READERS or name == 'plan':
            return None
        if name == 'subagent' and args.get('access') == 'read_only':
            return None
        if self.team and name in {'TaskCreate', 'TaskUpdate', 'TeamPlanSubmit', 'team_get_status'}:
            state = self.repository.planning_state(self.conversation_id)
            current = self.repository.get_planning_revision(state['active_plan_id']) if state['active_plan_id'] else None
            if current and current['status'] in {'submitted', 'approved', 'starting', 'blocked'}:
                return 'Blocked: 方案已提交，任务已冻结。请先根据用户反馈保存新版本草稿。'
            if name == 'TaskUpdate' and ('status' in args or 'owner' in args):
                return 'Blocked: 规划期间不能领取、分配或完成任务'
            return None
        return 'Blocked: Plan Mode 仅允许项目调查和方案编辑。请使用 inspect_project/read_file/plan；执行需要用户批准。'

    def guard(self, tool):
        return self.check(tool.name, tool.input)

    def filter(self, registry):
        allowed = PLAN_READERS | {'plan', 'subagent'}
        if self.team:
            allowed |= {'TaskCreate', 'TaskUpdate', 'TeamPlanSubmit', 'team_get_status'}
        return registry.copy_without(s['name'] for s in registry.schemas() if s['name'] not in allowed)

    def wrap(self, registry):
        # Retain existing role/scope wrapper, and also guard direct registry callers.
        return registry.with_execution_wrapper(
            lambda name, args, handler: self.check(name, args) or registry.execute(name, args))
