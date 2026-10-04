from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codeagent.mcp import McpRouter, load_mcp_servers
from codeagent.tools import ToolRegistry


class McpConfigTests(unittest.TestCase):
    def test_missing_config_disables_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "missing.json"

            self.assertEqual(load_mcp_servers(path), [])

    def test_loads_claude_style_mcp_servers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mcp.json"
            path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "demo": {
                                "command": "python",
                                "args": ["server.py"],
                                "env": {"TOKEN": "${DEMO_TOKEN}"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            servers = load_mcp_servers(path)

            self.assertEqual(len(servers), 1)
            self.assertEqual(servers[0].name, "demo")
            self.assertEqual(servers[0].transport, "stdio")
            self.assertEqual(servers[0].args, ("server.py",))


class McpRouterTests(unittest.TestCase):
    def test_binary_media_is_not_dumped_into_text_and_error_status_survives(self):
        from codeagent.mcp.router import _tool_result_text
        result = SimpleNamespace(content=[SimpleNamespace(type='image', data='BASE64SECRET' * 10000)],
                                 structured_content=None, is_error=True)
        output = _tool_result_text(result)
        self.assertEqual(output.status, 'error')
        self.assertNotIn('BASE64SECRET', output)
        self.assertIn('unsupported MCP image', output)
        self.assertEqual(output.media_references, [{'type': 'image', 'uri': None, 'supported': False}])

    def test_mcp_structured_scalar_keeps_json_type(self):
        from codeagent.mcp.router import _tool_result_text
        result = SimpleNamespace(content=[], structured_content='structured string', is_error=False)
        output = _tool_result_text(result)
        self.assertTrue(output.structured_json)
        self.assertEqual(json.loads(output), 'structured string')

    def test_structured_content_is_preserved_alongside_explanatory_text(self):
        from codeagent.mcp.router import _tool_result_text
        result = SimpleNamespace(content=[SimpleNamespace(text='Found two values')],
                                 structured_content=[1, 2], is_error=False)
        output = _tool_result_text(result)
        self.assertEqual(json.loads(output)['structured_content'], [1, 2])
        self.assertEqual(json.loads(output)['text_content'], ['Found two values'])

    def test_discovery_order_does_not_change_registration_order(self):
        from codeagent.mcp.router import McpTool
        with tempfile.TemporaryDirectory() as directory:
            router = McpRouter(Path(directory) / "missing.json")
            router._tools = [McpTool(router, "server", SimpleNamespace(
                name=name, description="test", input_schema={"type": "object"})) for name in ("z", "a")]
            first, second = ToolRegistry(), ToolRegistry()
            router.register_tools(first)
            router._tools.reverse()
            router.register_tools(second)
            self.assertEqual(first.schemas(), second.schemas())
            self.assertEqual(router.list_tools(), ["mcp__server__a", "mcp__server__z"])

    def test_discovers_registers_and_calls_stdio_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server_path = root / "server.py"
            server_path.write_text(
                """from mcp.server import MCPServer

server = MCPServer("test-server")

@server.tool(description="Add two integers")
def add(a: int, b: int) -> int:
    return a + b

server.run()
""",
                encoding="utf-8",
            )
            config_path = root / "mcp.json"
            config_path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "math": {
                                "command": sys.executable,
                                "args": [str(server_path)],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            router = McpRouter(config_path)
            registry = ToolRegistry()

            try:
                router.register_tools(registry)
                names = {schema["name"] for schema in registry.schemas()}
                result = registry.execute("mcp__math__add", {"a": 2, "b": 3})
            finally:
                router.close()

            self.assertEqual(names, {"mcp__math__add"})
            self.assertIn("5", result)


if __name__ == "__main__":
    unittest.main()
