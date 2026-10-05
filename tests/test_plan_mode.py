import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codeagent import ModelResponse
from codeagent.config import EnvironmentConfig
from codeagent.events import EventEmitter, ExecutionContext
from codeagent.memory import MemoryConfig
from codeagent.messages import ToolUse
from codeagent.permissions import WaitingPermissionBroker
from codeagent.plan_mode import PlanTool, PlannedTeamSubmitTool, PlanModeGate, InspectProjectTool
from codeagent.prompts import PromptMode
from codeagent.runtime import CancellationToken
from codeagent.teams.lead import LeadTeamPlanTool
from codeagent.web.factory import WebAgentFactory
from codeagent.web.scheduler import RunScheduler
from codeagent.web.storage import SQLiteRepository, StorageConflictError
from codeagent.worktrees import WorktreeManagerRegistry

BODY = '# Implementation\n\nRead the existing modules, implement the requested feature in src, and verify with unit tests.\n'


class Client:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def create_message(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0) if self.responses else ModelResponse('end_turn', [{'type': 'text', 'text': 'done'}])


class PlanModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'source'
        self.workspace.mkdir()
        self.repo = SQLiteRepository(self.root / 'state.db', recover_incomplete=False)
        self.conv = self.repo.create_conversation(workspace=self.workspace)
        self.run = self.repo.create_run(self.conv.id, status='completed')
        self.repo.set_planning_mode(self.conv.id, 'planning')
        self.tool = PlanTool(self.repo, self.conv.id, self.run.id)
        self.env = EnvironmentConfig(model_id='test', enable_skills=False,
            memory_config=MemoryConfig(enabled=False), team_runtime_enabled=True,
            team_worktree_root=self.root / 'worktrees')
        self.factory = WebAgentFactory(self.env, self.workspace, self.repo)
        self.scheduler = RunScheduler(self.repo, self.factory)
        self.scheduler.configure_team_runtime(WorktreeManagerRegistry(self.repo, self.root / 'worktrees'))

    def tearDown(self):
        self.scheduler.stop()
        self.repo.close()
        self.temp.cleanup()

    def plan(self, submit=True):
        self.tool.run('submit' if submit else 'update', title='Implementation', markdown=BODY)
        return self.repo.list_planning_revisions(self.conv.id)[0]

    def agent(self, client, *, planning=True, team=False, read_only=False, checkpoint=None):
        with patch.object(EnvironmentConfig, 'create_model_client', return_value=client):
            return self.factory.create(event_emitter=EventEmitter(context=ExecutionContext(
                conversation_id=self.conv.id, run_id=self.run.id)), cancellation=CancellationToken(),
                permission_broker=WaitingPermissionBroker(), plan_mode=planning, read_only=read_only,
                root_prompt_mode=PromptMode.TEAM_PLANNER if team else None, checkpoint=checkpoint)

    def team_plan(self):
        self.repo.set_planning_mode(self.conv.id, 'planning', 'team')
        task = self.repo.create_task(self.conv.active_task_list_id, subject='Implement feature', description=BODY,
            metadata={'kind': 'code', 'write_scopes': ['src'], 'risk_level': 'low',
                      'plan_required': False, 'validation_commands': ['python -m unittest']})
        raw = {'tasks': [{'task_id': task.task.id, 'kind': 'code', 'write_scopes': ['src'], 'risk_level': 'low'}],
               'integration_validation_commands': ['python -m unittest']}
        original = LeadTeamPlanTool(self.repo, None, self.conv.id, self.run.id, self.conv.active_task_list_id)
        PlannedTeamSubmitTool(original, self.tool).run(plan=raw, teammateCount=1, markdown=BODY)
        return self.repo.list_planning_revisions(self.conv.id)[0]

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.workspace), *args], stderr=subprocess.STDOUT).decode().strip()

    def init_git(self):
        self.git('init')
        self.git('config', 'user.name', 'Tests')
        self.git('config', 'user.email', 'tests@example.invalid')
        (self.workspace / 'base.txt').write_text('base')
        self.git('add', '.')
        self.git('commit', '-m', 'base')

    def test_draft_updates_then_submission_freezes_version(self):
        draft = self.plan(False)
        submitted = self.plan()
        self.assertEqual(draft['id'], submitted['id'])
        self.tool.run('update', title='Revised', markdown=BODY + 'Revision')
        revisions = self.repo.list_planning_revisions(self.conv.id)
        self.assertEqual([p['revision'] for p in revisions], [2, 1])
        self.assertEqual(revisions[1]['status'], 'superseded')
        with self.assertRaises(StorageConflictError):
            self.repo.decide_planning_revision(submitted['id'], submitted['content_hash'], 'approve')

    def test_hash_mismatch_cannot_approve(self):
        plan = self.plan()
        with self.assertRaises(StorageConflictError):
            self.repo.decide_planning_revision(plan['id'], 'stale', 'approve')

    def test_read_only_approval_does_not_grant_write(self):
        self.tool.read_only = True
        plan = self.plan()
        with self.assertRaises(StorageConflictError):
            self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')

    def test_restore_mode_and_submitted_revision(self):
        plan = self.plan()
        self.repo.close()
        self.repo = SQLiteRepository(self.root / 'state.db')
        self.assertEqual(self.repo.planning_state(self.conv.id)['mode'], 'planning')
        self.assertEqual(self.repo.get_planning_revision(plan['id'])['markdown'], BODY)

    def test_non_git_team_plan_only_initializes_repository_after_approval(self):
        plan = self.team_plan()
        self.assertEqual(plan['status'], 'submitted')
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual(self.repo.list_team_runs(), [])
        started = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
        self.assertEqual(started['status'], 'started', started.get('error'))
        self.assertTrue((self.workspace / '.git').is_dir())
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '1')
        self.assertEqual(self.git('ls-tree', '--name-only', 'HEAD'), '')
        self.assertEqual(self.git('remote'), '')

    def test_unborn_repository_keeps_staging_and_working_files_when_starting_team(self):
        self.git('init')
        source = self.workspace / 'base.txt'
        source.write_text('staged version')
        self.git('add', 'base.txt')
        source.write_text('working version')
        (self.workspace / 'other.txt').write_text('untracked')
        index = (self.workspace / '.git' / 'index').read_bytes()
        plan = self.team_plan()
        started = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
        self.assertEqual(started['status'], 'started', started.get('error'))
        self.assertEqual((self.workspace / '.git' / 'index').read_bytes(), index)
        self.assertEqual(source.read_text(), 'working version')
        self.assertEqual(self.git('ls-tree', '--name-only', 'HEAD'), '')
        team = self.repo.get_team_run(started['team_run_id'])
        view = Path(self.scheduler._team_worktrees.read_view(team.id, team.integration_head))
        self.assertEqual((view / 'base.txt').read_text(), 'working version')
        self.assertEqual((view / 'other.txt').read_text(), 'untracked')

    def test_bootstrap_retry_after_failure_does_not_add_another_initial_commit(self):
        (self.workspace / 'existing.txt').write_text('keep me')
        plan = self.team_plan()
        with patch.object(LeadTeamPlanTool, 'run', side_effect=RuntimeError('interrupted preparation')):
            blocked = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
        self.assertEqual(blocked['status'], 'blocked')
        initial = self.git('rev-parse', 'HEAD')
        started = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
        self.assertEqual(started['status'], 'started', started.get('error'))
        self.assertEqual(self.git('rev-parse', 'HEAD'), initial)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '1')
        self.assertEqual((self.workspace / 'existing.txt').read_text(), 'keep me')

    def test_bootstrap_does_not_create_nested_repo_or_repair_broken_metadata(self):
        subprocess.run(['git', '-C', str(self.root), 'init'], check=True, capture_output=True)
        plan = self.team_plan()
        blocked = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
        self.assertEqual(blocked['status'], 'blocked')
        self.assertFalse((self.workspace / '.git').exists())
        (self.workspace / '.git').write_text('gitdir: missing-repository')
        blocked = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual((self.workspace / '.git').read_text(), 'gitdir: missing-repository')

    def test_missing_git_produces_actionable_error_without_initializing(self):
        from codeagent.worktrees.snapshots import LocalGit
        plan = self.team_plan()
        with patch.object(LocalGit, 'run', side_effect=FileNotFoundError('git')):
            blocked = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
        self.assertEqual(blocked['status'], 'blocked')
        self.assertIn('未找到 Git', blocked['error'])
        self.assertFalse((self.workspace / '.git').exists())

    def test_team_approval_once_starts_one_team_preserves_head_and_index(self):
        self.init_git()
        before_head, before_index = self.git('rev-parse', 'HEAD'), self.git('diff', '--cached')
        plan = self.team_plan()
        result = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
        self.assertEqual(result['status'], 'started', result.get('error'))
        again = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
        self.assertEqual(again['team_run_id'], result['team_run_id'])
        team = self.repo.get_team_run(result['team_run_id'])
        self.assertEqual(team.state.value, 'running')
        self.assertEqual(len(self.repo.list_team_runs()), 1)
        self.assertEqual(self.git('rev-parse', 'HEAD'), before_head)
        self.assertEqual(self.git('diff', '--cached'), before_index)
        self.assertEqual(self.repo.planning_state(self.conv.id)['mode'], 'off')

    def test_task_mutation_after_submission_blocks_launch(self):
        self.init_git()
        plan = self.team_plan()
        task_id = plan['payload']['team_input']['plan']['tasks'][0]['task_id']
        self.repo.update_task(self.conv.active_task_list_id, task_id, changes={'description': 'changed scope'})
        result = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.repo.list_team_runs(), [])

    def test_gate_rejects_shell_unknown_tool_and_write_capable_child(self):
        gate = PlanModeGate(self.repo, self.conv.id, team=True)
        for name, args in [('bash', {'command': 'node --version'}), ('write_file', {'path': 'src/a'}),
                           ('mcp__x', {}), ('subagent', {'access': 'inherit'}), ('plan_exit', {})]:
            self.assertIsNotNone(gate.check(name, args))
        self.assertIsNone(gate.check('subagent', {'access': 'read_only'}))
        plan = self.team_plan()
        self.assertIsNotNone(gate.check('TaskCreate', {}))
        self.assertEqual(plan['status'], 'submitted')

    def test_model_submits_plan_and_yields_without_another_call(self):
        client = Client([ModelResponse('tool_use', [{'type': 'tool_use', 'id': 'p1', 'name': 'plan',
            'input': {'action': 'submit', 'title': 'Implementation', 'markdown': BODY}}])])
        agent = self.agent(client)
        result = agent.run('Plan this feature first')
        self.assertEqual(result.stop_reason, 'waiting:plan_approval')
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.repo.list_planning_revisions(self.conv.id)[0]['status'], 'submitted')
        self.assertNotIn('bash', agent.tools)
        self.assertNotIn('write_file', agent.tools)

    def test_normal_factory_tools_and_prompt_unchanged_by_overlay(self):
        normal = self.agent(Client([]), planning=False)
        normal2 = self.agent(Client([]), planning=False)
        self.assertNotIn('plan', normal.tools)
        self.assertNotIn('inspect_project', normal.tools)
        self.assertEqual(normal.tools.schemas(), normal2.tools.schemas())
        self.assertEqual(normal._assemble_prompt(normal.tools.schemas()).system_prompt,
                         normal2._assemble_prompt(normal2.tools.schemas()).system_prompt)
        self.assertNotIn('plan_mode', normal.context.state.runtime_reminders)

    def test_draft_updates_do_not_change_system_or_tool_prefix(self):
        agent = self.agent(Client([]))
        schemas = agent.tools.schemas()
        first = agent._assemble_prompt(schemas).system_prompt
        self.plan(False)
        self.tool.run('update', title='Changed', markdown=BODY + 'More detail')
        self.assertEqual(agent.tools.schemas(), schemas)
        self.assertEqual(agent._assemble_prompt(schemas).system_prompt, first)

    def test_approved_single_plan_launch_idempotent(self):
        plan = self.plan()
        client = Client([])
        with patch.object(EnvironmentConfig, 'create_model_client', return_value=client):
            result = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
            self.assertEqual(result['status'], 'started', result.get('error'))
            for _ in range(300):
                run = self.repo.get_run(result['execution_run_id'])
                if run.status in {'completed', 'failed'}:
                    break
                time.sleep(.01)
            again = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
        self.assertEqual(run.status, 'completed', run.error)
        self.assertEqual(result['execution_run_id'], again['execution_run_id'])
        self.assertEqual(len(client.calls), 1)

    def test_inspect_empty_directory_is_success_without_git_initialization(self):
        result = json.loads(InspectProjectTool(self.workspace).run())
        self.assertTrue(result['empty'])
        self.assertFalse(result['git']['available'])
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_user_reject_or_withdraw_never_launches(self):
        plan = self.plan()
        self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'reject')
        self.assertEqual(self.repo.planning_state(self.conv.id)['mode'], 'planning')
        self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'withdraw')
        self.assertEqual(self.repo.planning_state(self.conv.id)['mode'], 'off')
        self.assertEqual(len(self.repo.list_runs(conversation_id=self.conv.id)), 1)

    def test_approved_launch_recovers_after_restart_without_duplicate_run(self):
        plan = self.plan()
        self.repo.decide_planning_revision(plan['id'], plan['content_hash'], 'approve')
        client = Client([])
        with patch.object(EnvironmentConfig, 'create_model_client', return_value=client):
            self.scheduler.recover_plan_launches()
            started = self.repo.get_planning_revision(plan['id'])
            self.assertEqual(started['status'], 'started', started.get('error'))
            self.scheduler.recover_plan_launches()
            for _ in range(300):
                if self.repo.get_run(started['execution_run_id']).status in {'completed', 'failed'}:
                    break
                time.sleep(.01)
        self.assertEqual(len([r for r in self.repo.list_runs() if r.metadata.get('approved_plan_id')]), 1)

    def test_team_recovery_after_approval_before_final_receipt(self):
        self.init_git()
        plan = self.team_plan()
        started = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
        self.repo.update_plan_execution(plan['id'], status='starting')
        self.scheduler.recover_plan_launches()
        recovered = self.repo.get_planning_revision(plan['id'])
        self.assertEqual(recovered['status'], 'started')
        self.assertEqual(recovered['team_run_id'], started['team_run_id'])
        self.assertEqual(len(self.repo.list_team_runs()), 1)

    def test_direct_registry_cannot_delegate_with_write_access(self):
        agent = self.agent(Client([]))
        result = agent.tools.execute('subagent', {'description': 'write code', 'prompt': 'write src/test.py', 'access': 'inherit'})
        self.assertIn('Blocked:', result)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_read_only_can_save_plan_but_cannot_approve_it(self):
        client = Client([ModelResponse('tool_use', [{'type': 'tool_use', 'id': 'p1', 'name': 'plan',
            'input': {'action': 'submit', 'title': 'Implementation', 'markdown': BODY}}])])
        agent = self.agent(client, read_only=True)
        self.assertEqual(agent.run('Plan only').stop_reason, 'waiting:plan_approval')
        plan = self.repo.list_planning_revisions(self.conv.id)[0]
        with self.assertRaises(StorageConflictError):
            self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')

    def test_http_preview_revision_guard_and_cross_conversation_boundary(self):
        from fastapi.testclient import TestClient
        from codeagent.web.api import create_app
        plan = self.plan()
        other = self.repo.create_conversation(workspace=self.workspace)
        app = create_app(workspace=self.workspace, repository=self.repo, scheduler=self.scheduler,
                         env=SimpleNamespace(model_id='test', data_dir=self.root / 'api-data', team_runtime_enabled=False))
        with TestClient(app) as client:
            preview = client.get(f'/api/conversations/{self.conv.id}/plans')
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.json()['plans'][0]['markdown'], BODY)
            url = f'/api/conversations/{self.conv.id}/plans/{plan["id"]}/decision'
            stale = client.post(url, json={'decision': 'approve', 'contentHash': 'old'})
            self.assertEqual(stale.status_code, 409)
            denied = client.post(f'/api/conversations/{other.id}/plans/{plan["id"]}/decision',
                                json={'decision': 'reject', 'contentHash': plan['content_hash']})
            self.assertEqual(denied.status_code, 404)
            rejected = client.post(url, json={'decision': 'reject', 'contentHash': plan['content_hash']})
            self.assertEqual(rejected.json()['status'], 'rejected')
            restored = client.post(url, json={'decision': 'restore', 'contentHash': plan['content_hash']})
            self.assertEqual(restored.status_code, 200)
            self.assertEqual(restored.json()['status'], 'submitted')
            exited = client.post(f'/api/conversations/{self.conv.id}/plans/exit', json={})
            self.assertEqual(exited.json()['mode'], 'off')

    def test_restore_rejected_plan_preserves_version_without_starting_execution(self):
        plan = self.plan()
        self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'reject')
        for _ in range(2):
            restored = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'restore')
            self.assertEqual(restored['status'], 'submitted')
            for field in ('id', 'markdown', 'revision', 'content_hash'):
                self.assertEqual(restored[field], plan[field])
        self.assertEqual(len(self.repo.list_runs()), 1)
        self.assertEqual(self.repo.list_team_runs(), [])
        self.assertEqual(self.repo.planning_state(self.conv.id)['mode'], 'planning')

    def test_restore_cannot_replace_a_new_draft_or_change_read_only_protection(self):
        self.tool.read_only = True
        plan = self.plan()
        self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'reject')
        with self.assertRaises(StorageConflictError):
            self.scheduler.decide_plan(plan['id'], 'stale', 'restore')
        restored = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'restore')
        self.assertTrue(restored['read_only'])
        with self.assertRaises(StorageConflictError):
            self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
        self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'reject')
        self.tool.run('update', title='New draft', markdown=BODY + 'New revision')
        with self.assertRaises(StorageConflictError):
            self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'restore')

    def test_restore_waits_for_active_planning_run(self):
        plan = self.plan()
        self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'reject')
        self.repo.create_run(self.conv.id, status='running')
        with self.assertRaises(StorageConflictError):
            self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'restore')
        self.assertEqual(self.repo.get_planning_revision(plan['id'])['status'], 'rejected')

    def test_read_file_snapshot_blocks_approval_after_source_changes(self):
        source = self.workspace / 'example.py'
        source.write_text('value = 1\n')
        client = Client([
            ModelResponse('tool_use', [{'type': 'tool_use', 'id': 'read1', 'name': 'read_file',
                'input': {'file_path': str(source)}}]),
            ModelResponse('tool_use', [{'type': 'tool_use', 'id': 'plan1', 'name': 'plan',
                'input': {'action': 'submit', 'title': 'Implementation', 'markdown': BODY}}]),
        ])
        self.agent(client).run('Read example.py and plan the change')
        plan = self.repo.list_planning_revisions(self.conv.id)[0]
        self.assertEqual(list(plan['payload']['source_files']), ['example.py'])
        source.write_text('value = 2\n')
        result = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('文件已变化', result['error'])

    def test_task_edit_during_git_preparation_cannot_bypass_approval(self):
        self.init_git()
        plan = self.team_plan()
        task_id = plan['payload']['team_input']['plan']['tasks'][0]['task_id']
        registry = self.scheduler._team_worktrees
        prepare = registry.ensure_baseline_ready

        def changed_during_prepare(team_id):
            result = prepare(team_id)
            self.repo.update_task(self.conv.active_task_list_id, task_id,
                                  changes={'description': 'unreviewed scope'})
            return result

        with patch.object(registry, 'ensure_baseline_ready', side_effect=changed_during_prepare):
            result = self.scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
        self.assertEqual(result['status'], 'blocked', result.get('error'))
        self.assertEqual(self.repo.list_team_runs()[0].state.value, 'waiting_approval')


if __name__ == '__main__':
    unittest.main()
