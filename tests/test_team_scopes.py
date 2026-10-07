import os
import unittest

from codeagent.teams.scopes import normalize_scopes, path_is_allowed, path_is_scope_parent, scope_is_within
from codeagent.teams.tasks import validate_task_execution
from codeagent.runtime.background import _path_allowed_for_recovery
from codeagent.web.storage import _scope_is_within, _team_resources_overlap


class TeamScopeTests(unittest.TestCase):
    def test_execution_submission_and_recovery_share_recursive_scope_semantics(self):
        for scopes in [("server/**", "DESIGN.md"), ("server", "DESIGN.md")]:
            for path, allowed in [("server/src/app.ts", True), (r"server\src\app.ts", True),
                                  ("DESIGN.md", True), ("web/app.ts", False),
                                  ("server-old/app.ts", False), ("server/../web/app.ts", False)]:
                with self.subTest(scopes=scopes, path=path):
                    self.assertEqual(path_is_allowed(path, scopes), allowed)
                    self.assertEqual(_scope_is_within(path, scopes), allowed)
                    self.assertEqual(_path_allowed_for_recovery(path, scopes, repository_lease=False), allowed)

    def test_plan_containment_does_not_expand_permissions(self):
        self.assertTrue(scope_is_within("server/src/**", ["server/**"]))
        self.assertTrue(scope_is_within("server/**", ["server"]))
        self.assertFalse(scope_is_within("server/**", ["server/src/**"]))
        self.assertFalse(scope_is_within("web/**", ["server/**"]))
        self.assertFalse(path_is_allowed("server/**", ["server/**"]))
        self.assertTrue(path_is_scope_parent("server", ["server/src/**"]))
        self.assertFalse(path_is_scope_parent("web", ["server/src/**"]))

    def test_invalid_scopes_are_rejected_at_task_admission(self):
        for scope in ["", ".", "../server", "server/../web", "/server", r"C:\server", r"\\host\share",
                      "server/*", "server/**/*.ts", "server?.ts", "server/[ab]", "server//src"]:
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                validate_task_execution({"kind": "code", "write_scopes": [scope]})
        self.assertEqual(normalize_scopes([r"server\**", "server/**", "DESIGN.md"]), ["server/**", "DESIGN.md"])

    def test_case_semantics_follow_host_filesystem_convention(self):
        self.assertEqual(path_is_allowed("SERVER/src/app.ts", ["server/**"]), os.name == "nt")
        self.assertFalse(path_is_allowed(" server/app.ts", ["server/**"]))
        self.assertTrue(path_is_allowed("web/app/[id]/page.tsx", ["web/**"]))

    def test_recursive_and_legacy_lease_keys_overlap(self):
        for first, second in [("server/**", "server/src"), ("server", "server/**"), ("server/**", "server/src/**")]:
            self.assertTrue(_team_resources_overlap("path", first, "path", second))
            self.assertTrue(_team_resources_overlap("path", second, "path", first))
        self.assertFalse(_team_resources_overlap("path", "server/**", "path", "server-old/**"))
