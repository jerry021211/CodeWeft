"""User-owned, workspace-scoped approval of exact MCP launch configurations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path

from codeagent.mcp.config import McpServerConfig


def resolved_server(server: McpServerConfig) -> McpServerConfig:
    """Freeze expansions once, so the approved values are the ones launched."""
    if server.resolved:
        return server
    return replace(
        server, command=os.path.expandvars(server.command),
        args=tuple(os.path.expandvars(arg) for arg in server.args),
        env={key: os.path.expandvars(value) for key, value in server.env.items()},
        headers={key: os.path.expandvars(value) for key, value in server.headers.items()},
        url=os.path.expandvars(server.url),
        resolved=True,
    )


def server_fingerprint(server: McpServerConfig) -> str:
    data = asdict(resolved_server(server))
    data.pop('resolved')
    # Preserve existing approvals when the new optional behavior is not enabled.
    if data['lifecycle'] == 'shared':
        data.pop('lifecycle')
    data['cwd'] = str(server.cwd.resolve()) if server.cwd else None
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class McpTrustStore:
    """Only hashes are persisted; repository configuration cannot grant trust."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @staticmethod
    def _workspace(workspace: Path) -> str:
        return os.path.normcase(str(Path(workspace).resolve()))

    def is_trusted(self, workspace: Path, server: McpServerConfig) -> bool:
        if not self.path.is_file():
            return False
        try:
            with closing(sqlite3.connect(self.path)) as connection:
                row = connection.execute(
                    'SELECT fingerprint FROM mcp_trust WHERE workspace=? AND name=?',
                    (self._workspace(workspace), server.name),
                ).fetchone()
            return bool(row and row[0] == server_fingerprint(server))
        except sqlite3.Error:
            return False

    def approve(self, workspace: Path, server: McpServerConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS mcp_trust ('
                               'workspace TEXT, name TEXT, fingerprint TEXT, PRIMARY KEY(workspace, name))')
            connection.execute('INSERT OR REPLACE INTO mcp_trust VALUES (?, ?, ?)',
                               (self._workspace(workspace), server.name, server_fingerprint(server)))

    def revoke(self, workspace: Path, name: str) -> None:
        if not self.path.is_file():
            return
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('DELETE FROM mcp_trust WHERE workspace=? AND name=?',
                               (self._workspace(workspace), name))


def request_cli_trust(servers, store, workspace, broker, *, interactive: bool) -> None:
    """Prompt on the CLI thread, before any transport or child process exists."""
    for server in servers:
        if server.config_error:
            print(f'MCP {server.name}: invalid configuration; skipped.')
            continue
        if store.is_trusted(workspace, server):
            continue
        if not interactive:
            print(f'MCP {server.name}: awaiting trust; approve in MCP settings or an interactive terminal.')
            continue
        details = {'server': server.name, 'transport': server.transport,
                   'command': server.command, 'args': list(server.args),
                   'cwd': str(server.cwd or workspace), 'url': server.url,
                   'env_keys': sorted(server.env), 'header_keys': sorted(server.headers)}
        details['lifecycle'] = server.lifecycle
        try:
            approved = broker.request('mcp_connect', details,
                                      'Trust this MCP configuration before starting its process or connection?')
        except (EOFError, KeyboardInterrupt):
            approved = False
        if approved:
            store.approve(workspace, server)
