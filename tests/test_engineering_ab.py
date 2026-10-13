import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from codeagent.prompts import PromptRuntime
from evals.engineering_ab.cases import ARMS, TITLES, material
from evals.engineering_ab.checker import check
from evals.engineering_ab.offline import SOLUTIONS
from evals.engineering_ab.runtime import configure, run_check
from evals.engineering_ab.suite import grade, plan
from evals.evidence import hashes, write_json


class EngineeringABTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def project(self, case):
        seed, gold = material(case)
        workspace = self.root / case
        for name, text in seed['workspace_files'].items():
            p = workspace / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding='utf-8')
        return workspace, seed, gold

    def test_schedule_has_equal_inputs_and_balanced_arms(self):
        settings = plan()
        self.assertEqual(settings['trials'], 30)
        for i in range(0, 30, 3):
            triple = settings['schedule'][i:i+3]
            self.assertEqual({r['variant'] for r in triple}, set(ARMS))
            self.assertEqual(len({(r['case'], r['repeat']) for r in triple}), 1)
        self.assertEqual(settings, plan())
        with self.assertRaises(ValueError):
            plan(repeats=99)
        self.assertEqual(plan(cases=['J03'], repeats=1, max_tokens=8192)['trials'], 3)
        with self.assertRaises(ValueError):
            plan(cases=['J03', 'J03'])

    def test_independent_checks_accept_repairs_and_reject_target_failures(self):
        for case in TITLES:
            workspace, _, _ = self.project(case)
            self.assertEqual(check(workspace, case, True)['passed'], case == 'J03')
            for name, code in SOLUTIONS[case].items():
                (workspace / name).write_text(code, encoding='utf-8')
            self.assertTrue(check(workspace, case, True)['passed'], case)
            self.assertTrue(run_check(workspace, case)['passed'], case)
        (self.root / 'J02/src/service.py').write_text(SOLUTIONS['J02']['src/service.py'].replace('key = (tenant, user_id)', 'key = user_id'), encoding='utf-8')
        self.assertFalse(check(self.root / 'J02', 'J02', True)['passed'])
        (self.root / 'J03/src/retry.py').write_text("def handle(responses):\n r=responses[0] if responses else {}\n return {'status':r.get('status',0),'body':r.get('body',''),'delays':[]}\n", encoding='utf-8')
        self.assertFalse(check(self.root / 'J03', 'J03', True)['passed'])

    def test_ablation_preserves_safety_identity_and_current_vs_cvm_prompt(self):
        policies = {}
        for arm in ARMS:
            trial = self.root / arm
            trial.mkdir()
            agent = SimpleNamespace(prompt_runtime=PromptRuntime(workspace=trial), config=SimpleNamespace())
            configure(agent, {'variant': arm}, trial)
            policies[arm] = json.loads((trial / 'policy-diff.json').read_text('utf-8'))
        self.assertEqual(len({p['core'] for p in policies.values()}), 1)
        self.assertEqual(len({p['identity'] for p in policies.values()}), 1)
        self.assertEqual(policies['current']['effective_execution'], policies['cvm']['effective_execution'])
        self.assertNotEqual(policies['ablated']['effective_execution'], policies['current']['effective_execution'])
        self.assertEqual(policies['current']['cvm']['contract'], 'shadow')
        self.assertEqual(policies['cvm']['cvm']['contract'], 'active')

    def test_delivery_checks_are_not_a_substitute_for_functional_success(self):
        workspace, seed, gold = self.project('J03')
        trial = self.root / 'trial'
        trial.mkdir()
        workspace.rename(trial / 'workspace')
        write_json(trial / 'manifest.json', {'case_id': 'J03', 'variant': 'current', 'repeat': 1,
            'profile': {'mode': 'offline'}, 'input_sha256': seed['input_sha256'], 'initial_files': hashes(trial / 'workspace')})
        write_json(trial / 'gold.json', gold)
        result = grade(trial, {'execution_status': 'completed', 'duration_ms': 1}, Path(__file__).resolve().parents[1])
        self.assertTrue(result['functional_pass'])
        self.assertFalse(result['automatic_success'])  # Original code is correct but no requested refactor delivered.
        (trial / 'workspace/src/retry.py').write_text(SOLUTIONS['J03']['src/retry.py'], encoding='utf-8')
        result = grade(trial, {'execution_status': 'completed', 'duration_ms': 1}, Path(__file__).resolve().parents[1])
        self.assertTrue(result['automatic_success'])
        self.assertFalse(result['current_version_checked'])  # Kept separate from task correctness.
        (trial / 'tool-intents.jsonl').write_text(json.dumps({'name': 'edit_file', 'path': 'src/retry.py', 'phase': 'analyze'})+'\n', encoding='utf-8')
        result = grade(trial, {'execution_status': 'completed', 'duration_ms': 1}, Path(__file__).resolve().parents[1])
        self.assertFalse(result['automatic_success'])
