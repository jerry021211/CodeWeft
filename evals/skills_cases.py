"""Synthetic, credential-free fixtures for skill routing and behavior smoke tests."""
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Case:
    id: str
    skill: str | None
    prompt: str
    files: dict[str, str]
    reads: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()
    repair: bool = False
    missing: str = ""
    kind: str = "implicit"


BROKEN = "def total(values):\n    return sum(values) / len(values)\n"
CORRECT = "def total(values):\n    return sum(values)\n"
CHECKS = """import unittest
from totals import total

class TotalsTests(unittest.TestCase):
    def test_total(self):
        self.assertEqual(total([2, 4]), 6)
    def test_empty(self):
        self.assertEqual(total([]), 0)
    def test_negative(self):
        self.assertEqual(total([-2, 5]), 3)
"""


def cases() -> list[Case]:
    items = [
        Case("repository", "repository-map", "只读梳理这个小仓库的入口到计算函数调用路径，并给出测试命令。文件为 README.md、main.py、calc.py。",
             {"README.md": "CLI: python main.py\nTests: python -m unittest discover -s tests\n",
              "main.py": "from calc import double\nprint(double(21))\n", "calc.py": "def double(value):\n    return value * 2\n"},
             ("README.md", "main.py", "calc.py"), ("main.py", "double", "unittest")),
        Case("repair", "bug-fix", "修复 totals.py：total 应返回总和，但 [2,4] 得到 3，空输入也异常。现有 test_totals.py 已覆盖回归场景，只修改 totals.py。请运行现有检查确认修复。",
             {"totals.py": BROKEN, "test_totals.py": CHECKS}, ("totals.py", "test_totals.py"), (), True),
        Case("tests", "test-design", "为 pagination.py 的 page 函数设计测试计划，不修改代码。按正常、边界、错误输入给出输入与确定的预期结果。",
             {"pagination.py": "def page(items, limit):\n    if limit < 1:\n        raise ValueError('limit')\n    return items[:limit]\n"},
             ("pagination.py",), ("ValueError", "0")),
        Case("refactor", "python-refactor", "只读分析如何对 defaults.py 的 choose 做保持行为的 Python 重构，指出哪些输入不能混为一谈，给出兼容性验证。",
             {"defaults.py": "def choose(value):\n    if value is None:\n        return 9\n    else:\n        return value\n",
              "test_defaults.py": "from defaults import choose\nassert choose(None) == 9\nassert choose(0) == 0\n"},
             ("defaults.py", "test_defaults.py"), ("None", "0", "9")),
        Case("review", "code-review", "审查 cache.py，找可实际触发的错误，给出触发方式及位置。只审查，不修改。",
             {"cache.py": "def remember(item, cache=[]):\n    cache.append(item)\n    return cache\n",
              "test_cache.py": "from cache import remember\nassert remember('first') == ['first']\n"},
             ("cache.py",), ("cache.py", "remember")),
        Case("api", "api-contract", "只读核对 server.py 与 client.ts 的 HTTP API 响应契约，定位可空性或字段不一致，提出兼容方案。",
             {"server.py": "def get_user():\n    return {'user_id': 7, 'display_name': None}\n",
              "client.ts": "type User = { user_id: number; display_name: string };\nfunction label(u: User) { return u.display_name.toUpperCase(); }\n"},
             ("server.py", "client.ts"), ("display_name", "toUpperCase")),
        Case("react", "react-ui", "诊断 Profile.tsx：快速切换 userId 后偶尔显示旧用户，指出异步交互根因和修复思路。仅分析。",
             {"Profile.tsx": "import {useState, useEffect} from 'react';\nexport function Profile({userId}) {\n const [user, setUser] = useState(null);\n useEffect(() => {fetch('/users/' + userId).then(r => r.json()).then(setUser);}, [userId]);\n return <span>{user?.name}</span>;\n}\n"},
             ("Profile.tsx",), ("userId", "setUser")),
        Case("sqlite", "sqlite-migration", "检查 schema.sql 与 migration.sql：旧 SQLite 库有两条 users 数据，该升级能否成功？给出保留旧数据的迁移与验证方案，不执行迁移。",
             {"schema.sql": "CREATE TABLE users(id INTEGER PRIMARY KEY);\nINSERT INTO users VALUES (1), (2);\n",
              "migration.sql": "ALTER TABLE users ADD COLUMN email TEXT NOT NULL;\n"},
             ("schema.sql", "migration.sql"), ("email", "NOT NULL")),
        Case("dependencies", "dependency-audit", "只读检查 package.json 与 package-lock.json 的依赖一致性，指出冲突版本。不要安装或联网审计。",
             {"package.json": '{"name":"fixture","dependencies":{"widget":"^2.0.0"}}',
              "package-lock.json": '{"name":"fixture","lockfileVersion":3,"packages":{"":{"dependencies":{"widget":"^1.0.0"}},"node_modules/widget":{"version":"1.2.0"}}}'},
             ("package.json", "package-lock.json"), ("widget", "1.2.0", "2.0.0")),
        Case("performance", "performance-diagnosis", "根据 profile.json 分析请求瓶颈和下一步性能实验，只读分析，不宣称已完成优化。",
             {"profile.json": '{"samples":40,"request_ms":100,"db_wait_ms":85,"serialize_ms":5,"other_ms":10,"cache":"cold"}'},
             ("profile.json",), ("85", "40")),
        Case("runtime", "agent-runtime", "排查 runtime.py 的 Agent 工具执行链：读模式下仍会写文件。指出真正需要强制权限的边界，只分析。",
             {"runtime.py": "TOOLS = {'write_file': lambda path, text: open(path, 'w').write(text)}\ndef execute(name, args, read_only=False):\n    return TOOLS[name](**args)\n"},
             ("runtime.py",), ("read_only", "execute")),
        Case("skills", "skill-evaluation", "评估 candidate/SKILL.md 的测试是否能证明模型遵循流程，阅读 results.json，设计触发、过程、结果的改进验证。只读。",
             {"candidate/SKILL.md": "---\nname: sample\ndescription: Repair a calculation.\n---\nRead input, run tests before and after fixing.\n",
              "results.json": '{"mode":"offline","scripted":true,"passed":10,"total":10,"trace":null}'},
             ("candidate/SKILL.md", "results.json"), ("offline",)),
        Case("arithmetic", None, "只计算 17 + 25，直接回答数值即可。", {}, facts=("42",), kind="negative"),
        Case("wording", None, "将‘会议延期到周五’改成一句礼貌的通知，不需要访问任何文件。", {}, facts=("周五",), kind="negative"),
        Case("missing", "skill-evaluation", "使用 skill-evaluation 评估 missing/SKILL.md 的遵循情况。如果该文件不存在，直接报告缺失，不编造内容。",
             {}, missing="missing/SKILL.md", kind="missing"),
    ]
    for name in ("repository", "repair"):
        base = next(case for case in items if case.id == name)
        items.append(replace(base, id=name + "-explicit", kind="explicit",
                             prompt=f"使用 ${base.skill} 完成下面任务。\n{base.prompt}"))
    return items
