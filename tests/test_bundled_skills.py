from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeagent.cli import create_skill_loader
from codeagent.config import EnvironmentConfig
from codeagent.skills import SkillLoader
from codeagent.skills.bundled import bundled_root, install_bundled
from codeagent.tools import LoadSkillTool
from codeagent.web.factory import WebAgentFactory


class BundledSkillTests(unittest.TestCase):
    def test_install_is_discoverable_by_cli_and_web_across_workspaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = EnvironmentConfig(model_id="offline", data_dir=root / "data")
            result = install_bundled(env.data_dir / "skills")
            self.assertEqual(len(result["installed"]), 12)
            for project in ("a", "b"):
                workspace = root / project
                workspace.mkdir()
                loaders = (create_skill_loader(env, workspace),
                           WebAgentFactory(env, workspace, None)._skill_loader())
                for loader in loaders:
                    self.assertEqual(len(loader.list_skills()), 12)
                    tool = LoadSkillTool(loader)
                    for name in result["installed"]:
                        response = tool.run(name)
                        self.assertEqual(response.status, "success")
                        self.assertEqual(response.skill, name)
                        self.assertIn(loader.load(name).content, str(response))
                    self.assertNotIn("# 故障修复", loader.catalog_prompt())

    def test_existing_skills_and_partial_directories_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "code-review"
            directory.mkdir()
            manifest = directory / "SKILL.md"
            manifest.write_bytes(b"user customization\r\n")
            (root / "bug-fix").mkdir()
            first = install_bundled(root)
            self.assertEqual(first["skipped"], ["bug-fix", "code-review"])
            self.assertEqual(manifest.read_bytes(), b"user customization\r\n")
            self.assertFalse((root / "bug-fix" / "SKILL.md").exists())
            self.assertEqual(install_bundled(root)["installed"], [])

    def test_unknown_or_path_names_fail_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "absent"
            with self.assertRaises(ValueError):
                install_bundled(root, ["bug-fix", "../other"])
            self.assertFalse(root.exists())

    def test_selected_install_and_catalog_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(install_bundled(root, ["bug-fix", "bug-fix"])["installed"], ["bug-fix"])
            self.assertEqual([s.name for s in SkillLoader([root]).list_skills()], ["bug-fix"])
        self.assertLess(len(SkillLoader([bundled_root()]).catalog_prompt()), 12000)


if __name__ == "__main__":
    unittest.main()
