"""Extensible language server definitions with explicit executable discovery."""
from dataclasses import dataclass, field, replace
import math
import json
import shutil
import os
import sys
from pathlib import Path


@dataclass(frozen=True)
class ServerDefinition:
    name: str
    extensions: tuple[str, ...]
    language: str
    command: tuple[str, ...]
    settings: dict = field(default_factory=dict)
    initialization_options: dict = field(default_factory=dict)
    startup_seconds: float = 45.
    project_markers: tuple[str, ...] = ()
    aggregate_root: bool = False

    def __post_init__(self):
        if not self.name or not self.language or not self.extensions:
            raise ValueError('LSP server name, language and extensions are required')
        if not isinstance(self.command, tuple) or not self.command or any(not isinstance(v, str) or not v for v in self.command):
            raise ValueError('LSP command must be a non-empty tuple of arguments')
        if not math.isfinite(self.startup_seconds) or self.startup_seconds <= 0:
            raise ValueError('LSP startup timeout must be positive')
        if not isinstance(self.settings, dict) or not isinstance(self.initialization_options, dict):
            raise ValueError('LSP settings and initialization options must be objects')


PYTHON_SERVERS = (
    ServerDefinition('pyright', ('.py', '.pyi'), 'python', ('pyright-langserver', '--stdio')),
    ServerDefinition('pylsp', ('.py', '.pyi'), 'python', ('pylsp',)),
)
POLYGLOT_SERVERS = (
    ServerDefinition('typescript', ('.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs'),
                     'typescript', ('typescript-language-server', '--stdio')),
    ServerDefinition('jdtls', ('.java',), 'java', ('jdtls',)),
)
DEFAULT_SERVERS = PYTHON_SERVERS + POLYGLOT_SERVERS


def installed_jdtls():
    """Reuse an already installed VS Code Java runtime, without downloads or writes to it."""
    extension_root = Path.home() / '.vscode/extensions'
    if not extension_root.is_dir():
        return None
    for extension in sorted(extension_root.glob('redhat.java-*'), key=lambda p: p.stat().st_mtime, reverse=True):
        launcher = extension / 'server/bin/jdtls'
        runtimes = list((extension / 'jre').glob('*/bin/java.exe' if os.name == 'nt' else '*/bin/java'))
        if launcher.is_file() and runtimes:
            return (sys.executable, str(launcher), '--java-executable', str(runtimes[0]),
                    '--jvm-arg=-Xms128m', '-configuration', '{data_dir}/configuration')
    return None


def configured_servers():
    servers = list(python_servers(os.getenv('CODEAGENT_PYTHON_LSP_COMMAND')) + POLYGLOT_SERVERS)
    for index, server in enumerate(servers):
        prefix = 'CODEAGENT_' + ('JAVA' if server.language == 'java' else 'TYPESCRIPT' if server.name == 'typescript' else 'PYTHON') + '_LSP_'
        command = os.getenv(prefix + 'COMMAND')
        parsed = json.loads(command) if command else list(server.command)
        if not isinstance(parsed, list) or not all(isinstance(value, str) for value in parsed):
            raise ValueError(prefix + 'COMMAND must be a JSON array of strings')
        servers[index] = replace(server,
            command=tuple(parsed),
            settings=json.loads(os.getenv(prefix + 'SETTINGS', '{}')),
            initialization_options=json.loads(os.getenv(prefix + 'INITIALIZATION_OPTIONS', '{}')))
    return tuple(servers)


def python_servers(command_json):
    if not command_json:
        return PYTHON_SERVERS
    command = json.loads(command_json)
    if not isinstance(command, list) or not command or any(not isinstance(v, str) or not v for v in command):
        raise ValueError('CODEAGENT_PYTHON_LSP_COMMAND must be a JSON array of non-empty strings')
    return (ServerDefinition('python-custom', ('.py', '.pyi'), 'python', tuple(command)),)


@dataclass(frozen=True)
class LspConfig:
    enabled: bool = True
    servers: tuple[ServerDefinition, ...] = DEFAULT_SERVERS
    timeout_seconds: float = 5.0
    feedback_seconds: float = 1.5

    def __post_init__(self):
        if len({server.name for server in self.servers}) != len(self.servers):
            raise ValueError('LSP server names must be unique')
        for value in (self.timeout_seconds, self.feedback_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError('LSP timeouts must be finite and positive')

    def select(self, path):
        for server in self.servers:
            if path.suffix.lower() in server.extensions and server.command and shutil.which(server.command[0]):
                return server
            if path.suffix.lower() in server.extensions and server.command == ('typescript-language-server', '--stdio'):
                from codeagent.runtime.data_paths import RuntimeDataPaths
                script = RuntimeDataPaths.default().root / 'tooling/typescript-lsp/node_modules/typescript-language-server/lib/cli.mjs'
                node = shutil.which('node')
                if node and script.is_file():
                    compiler = script.parents[2] / 'typescript/lib/tsserver.js'
                    options = dict(server.initialization_options)
                    if compiler.is_file() and 'tsserver' not in options:
                        options['tsserver'] = {'path': str(compiler), 'useSyntaxServer': 'never'}
                    return replace(server, command=(node, str(script), '--stdio'), initialization_options=options)
            if path.suffix.lower() in server.extensions and server.command == ('jdtls',):
                command = installed_jdtls()
                if command:
                    return replace(server, command=command)
        return None
