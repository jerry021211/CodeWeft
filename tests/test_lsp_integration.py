"""Opt-in REAL server smoke test; protocol/failure tests use a separate fake server."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from codeagent.lsp import LspService, LspConfig, ServerDefinition


@unittest.skipUnless(os.getenv('CODEAGENT_TEST_REAL_LSP') == '1' and shutil.which('pylsp'),
                     'Set CODEAGENT_TEST_REAL_LSP=1 with pylsp installed for real integration')
class RealPythonLspTests(unittest.TestCase):
    def test_definition_references_changed_diagnostics_and_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'sample.py'
            path.write_text('def double(value):\n    return value * 2\n\n\nanswer = double(3)\n', encoding='utf-8')
            config = LspConfig(servers=(ServerDefinition('pylsp', ('.py',), 'python', (shutil.which('pylsp'),)),), timeout_seconds=20)
            service = LspService(root, config)
            try:
                definition = service.execute('definition', path, 5, 11)
                self.assertEqual(definition['status'], 'ok', definition)
                self.assertEqual(definition['locations'][0]['line'], 1)
                references = service.execute('references', path, 5, 11)
                self.assertEqual(references['status'], 'ok', references)
                self.assertEqual({r['line'] for r in references['locations']}, {1, 5})
                path.write_text('def broken(:\n    pass\n', encoding='utf-8')
                report = service.execute('diagnostics', path)
                self.assertEqual(report['status'], 'ok', report)
                self.assertTrue(any(d.get('severity') == 1 for d in report['diagnostics']))
                processes = [s['rpc'].process for s in service.sessions.values()]
            finally:
                service.close()
            self.assertTrue(all(p.poll() is not None for p in processes))


@unittest.skipUnless(os.getenv('CODEAGENT_TEST_REAL_LSP') == '1', 'Opt-in real TypeScript server integration')
class RealTypeScriptLspTests(unittest.TestCase):
    def test_cross_file_definition_references_diagnostics_and_release(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = temporary.name
        root = Path(directory)
        (root / 'tsconfig.json').write_text('{"compilerOptions":{"strict":true,"noEmit":true}}')
        library = root / 'lib.ts'
        library.write_text('export function double(value: number): number { return value * 2; }\n')
        caller = root / 'app.ts'
        caller.write_text('import { double } from "./lib";\nconst result = double(3);\n')
        config = LspConfig(timeout_seconds=20)
        if config.select(caller) is None:
            self.skipTest('TypeScript language server is not installed')
        service = LspService(root, config)
        self.addCleanup(service.close)
        definition = service.execute('definition', caller, 2, 17)
        self.assertEqual(definition['status'], 'ok', definition)
        self.assertTrue(any(item['path'] == 'lib.ts' for item in definition['locations']), definition)
        refs = service.execute('references', caller, 2, 17)
        self.assertEqual(refs['status'], 'ok', refs)
        self.assertTrue({'lib.ts', 'app.ts'} <= {r['path'] for r in refs['locations']})
        caller.write_text('import { double } from "./lib";\nconst result = double("wrong");\n')
        diagnostics = service.execute('diagnostics', caller)
        self.assertEqual(diagnostics['status'], 'ok', diagnostics)
        self.assertTrue(any(d.get('severity') == 1 for d in diagnostics['diagnostics']), diagnostics)
        processes = [s['rpc'].process for s in service.sessions.values()]
        service.close()
        self.assertTrue(all(p.poll() is not None for p in processes))


@unittest.skipUnless(os.getenv('CODEAGENT_TEST_REAL_LSP') == '1', 'Opt-in real Java server integration')
class RealJavaLspTests(unittest.TestCase):
    def test_definition_references_and_release(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        path = root / 'Main.java'
        path.write_text('public class Main {\n  static int twice(int x) { return x * 2; }\n  int result = twice(3);\n}\n')
        config = LspConfig(timeout_seconds=40)
        if config.select(path) is None:
            self.skipTest('jdtls is not installed')
        service = LspService(root, config)
        self.addCleanup(service.close)
        definition = service.execute('definition', path, 3, 17)
        self.assertEqual(definition['status'], 'ok', definition)
        self.assertTrue(any(r['line'] == 2 for r in definition['locations']), definition)
        references = service.execute('references', path, 3, 17)
        self.assertEqual(references['status'], 'ok', references)
        self.assertTrue(any(r['line'] == 3 for r in references['locations']), references)
        path.write_text('public class Main {\n  void broken( {\n}\n')
        diagnostics = service.execute('diagnostics', path)
        self.assertEqual(diagnostics['status'], 'ok', diagnostics)
        self.assertTrue(any(d.get('severity') == 1 for d in diagnostics['diagnostics']), diagnostics)
        processes = [s['rpc'].process for s in service.sessions.values()]
        service.close()
        self.assertTrue(all(p.poll() is not None for p in processes))
