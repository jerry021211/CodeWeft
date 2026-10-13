"""Prevent native collaboration from growing back onto the old Team runtime."""
import ast
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MultiagentBoundaryTests(unittest.TestCase):
    def test_native_and_shared_modules_have_no_legacy_team_dependency(self):
        paths = [
            * (ROOT / 'codeagent/multiagent').glob('*.py'),
            * (ROOT / 'codeagent/worktrees').glob('*.py'),
            ROOT / 'codeagent/tasks/execution.py',
            ROOT / 'codeagent/runtime/validation.py',
            ROOT / 'codeagent/runtime/__init__.py',
        ]
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                modules = ([node.module or ''] if isinstance(node, ast.ImportFrom)
                           else [n.name for n in node.names] if isinstance(node, ast.Import) else [])
                for module in modules:
                    self.assertFalse(module.startswith(('codeagent.teams', 'codeagent.legacy.teams')),
                                     f'{path.relative_to(ROOT)} imports {module}')

    def test_importing_native_execution_does_not_initialize_legacy_runtime(self):
        result = subprocess.run([sys.executable, '-c', '''
import sys
import codeagent.multiagent.adapter
import codeagent.multiagent.coordinator
import codeagent.multiagent.verification
import codeagent.worktrees.snapshots
loaded = [name for name in sys.modules if name.startswith(('codeagent.teams', 'codeagent.legacy.teams'))]
assert not loaded, loaded
'''], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_application_has_no_imports_of_removed_locations(self):
        removed = ('codeagent.teams', 'codeagent.runtime.background', 'codeagent.worktrees.manager',
                   'codeagent.web.team_lifecycle', 'codeagent.web.team_integration_storage',
                   'codeagent.web.team_coordination', 'codeagent.web.team_observation')
        for path in (ROOT / 'codeagent').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                modules = ([node.module or ''] if isinstance(node, ast.ImportFrom)
                           else [n.name for n in node.names] if isinstance(node, ast.Import) else [])
                for module in modules:
                    self.assertFalse(module.startswith(removed), f'{path.relative_to(ROOT)}: {module}')


if __name__ == '__main__':
    unittest.main()
