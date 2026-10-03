"""One LSP service per Agent/workspace, with serialized document versions."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from threading import RLock
import time
from urllib.parse import urlparse
from urllib.request import url2pathname

from codeagent.code_intelligence.models import source_text
from codeagent.lsp.projects import project_root, server_command
import uuid
import re
import json
from threading import Timer


def uri_path(uri):
    # Python's Windows url2pathname does not recognize percent-encoded drive
    # colons. Normalize only that colon, then decode the rest exactly once.
    return url2pathname(re.sub(r'^/([A-Za-z])%3[aA]/', r'/\1:/', urlparse(uri).path))
from codeagent.lsp.registry import LspConfig
from codeagent.lsp.rpc import RpcClient, RpcError
from codeagent.runtime.cancellation import CancelledError
from codeagent.runtime.execution import ExecutionStopped
from codeagent.tools.workspace import WorkspaceGuard


class Initializing(TimeoutError):
    pass


class LspService:
    def __init__(self, workspace, config=None):
        self.guard = WorkspaceGuard(workspace)
        self.config = config or LspConfig()
        self.sessions = {}
        self.session_id = uuid.uuid4().hex
        self.failures = {}
        self.lock = RLock()
        self.cancellation_check = lambda: None
        self.remaining_seconds = lambda: 120.

    def bind_runtime(self, *, cancellation_check=None, remaining_seconds=None):
        self.cancellation_check = cancellation_check or (lambda: None)
        self.remaining_seconds = remaining_seconds or (lambda: 120.)

    def check(self):
        self.cancellation_check()
        if self.remaining_seconds() <= 0:
            raise ExecutionStopped('budget_exceeded:active_time')

    def _session(self, server, root, key, deadline):
        session = self.sessions.get(key)
        fingerprint = json.dumps([server.command, server.settings, server.initialization_options], sort_keys=True)
        if session is not None and session['fingerprint'] != fingerprint:
            self._drop(key, graceful=True)
            session = None
        if session is None:
            if self.failures.get(key, 0) > time.monotonic():
                raise RpcError('Server startup failed; retry is cooling down')
            session = {'documents': {}, 'diagnostics': {}, 'diagnostic_times': {}, 'sync_times': {}, 'capabilities': {}, 'registrations': {},
                       'started': time.monotonic(), 'root': root, 'ready': False, 'fingerprint': fingerprint}
            def notification(method, params):
                if method == 'textDocument/publishDiagnostics':
                    uri = params.get('uri')
                    if uri not in session['documents']:
                        try:
                            target = self.guard.resolve(uri_path(uri))
                            uri = next((opened for opened in session['documents']
                                        if self.guard.resolve(uri_path(opened)) == target), uri)
                        except (ValueError, OSError, TypeError):
                            return
                    document = session['documents'].get(uri)
                    if document and params.get('version', document[0]) == document[0]:
                        session['diagnostics'][uri] = (document[0], params.get('diagnostics', []), 'version' in params)
                        session['diagnostic_times'][uri] = time.monotonic()
            def client_request(method, params):
                if method == 'workspace/configuration':
                    result = []
                    for item in params.get('items', []):
                        value = server.settings
                        for part in item.get('section', '').split('.'):
                            if part:
                                value = value.get(part, {}) if isinstance(value, dict) else {}
                        result.append(value)
                    return result
                if method == 'workspace/workspaceFolders':
                    return [{'uri': root.as_uri(), 'name': root.name}]
                if method == 'client/registerCapability':
                    for registration in params.get('registrations', []):
                        session['registrations'][registration['id']] = registration
                        name = registration.get('method', '').removeprefix('textDocument/')
                        if name in ('definition', 'references'):
                            session['capabilities'][name + 'Provider'] = True
                if method == 'client/unregisterCapability':
                    for item in params.get('unregisterations', params.get('unregistrations', [])):
                        registered = session['registrations'].pop(item['id'], {})
                        name = registered.get('method', '').removeprefix('textDocument/')
                        if name in ('definition', 'references'):
                            session['capabilities'][name + 'Provider'] = session.get('static_capabilities', {}).get(name + 'Provider', False)
                return None
            try:
                rpc = RpcClient(server_command(server, self.guard.root, root, self.session_id), root, notification, client_request)
            except Exception:
                self.failures[key] = time.monotonic() + 30
                raise
            session['rpc'] = rpc
            self.sessions[key] = session
            def expire_startup():
                with self.lock:
                    if self.sessions.get(key) is session and not session['ready']:
                        self._drop(key)
                        self.failures[key] = time.monotonic() + 30
            timer = Timer(server.startup_seconds, expire_startup)
            timer.daemon = True
            session['startup_timer'] = timer
            timer.start()
            session['initialization_id'] = rpc.start_request('initialize', {
                'processId': os.getpid(), 'rootUri': root.as_uri(),
                'workspaceFolders': [{'uri': root.as_uri(), 'name': root.name}],
                'initializationOptions': server.initialization_options,
                'capabilities': {'general': {'positionEncodings': ['utf-16']},
                    'textDocument': {'publishDiagnostics': {'versionSupport': True}, 'diagnostic': {},
                                     'definition': {'dynamicRegistration': True}, 'references': {'dynamicRegistration': True}},
                    'workspace': {'configuration': True, 'workspaceFolders': True,
                                  'didChangeWatchedFiles': {'dynamicRegistration': False}}}}, deadline, self.check)
        if session['ready']:
            return session
        startup_deadline = session['started'] + server.startup_seconds
        try:
            initialized = session['rpc'].await_response(session['initialization_id'], 'initialize',
                min(deadline, startup_deadline), self.check, cancel_on_timeout=False)
        except TimeoutError:
            if time.monotonic() < startup_deadline:
                raise Initializing('Language server is initializing; retry a later request')
            self.failures[key] = time.monotonic() + 30
            raise
        session['static_capabilities'] = (initialized or {}).get('capabilities', {})
        session['capabilities'] = {**session['static_capabilities'], **session['capabilities']}
        if session['capabilities'].get('positionEncoding', 'utf-16') != 'utf-16':
            raise RpcError('Unsupported server position encoding')
        session['rpc'].notify('initialized', {}, deadline, self.check)
        if server.settings:
            session['rpc'].notify('workspace/didChangeConfiguration', {'settings': server.settings}, deadline, self.check)
        session['ready'] = True
        session['startup_timer'].cancel()
        return session

    def _sync(self, session, server, path, deadline):
        uri = path.as_uri()
        rpc = session['rpc']
        previous = session['documents'].get(uri)
        if not path.exists():
            if previous:
                rpc.notify('textDocument/didClose', {'textDocument': {'uri': uri}}, deadline, self.check)
            session['documents'].pop(uri, None)
            session['diagnostics'].pop(uri, None)
            rpc.notify('workspace/didChangeWatchedFiles', {'changes': [{'uri': uri, 'type': 3}]}, deadline, self.check)
            return None
        path = self.guard.ensure_within(path)
        if path.stat().st_size > 512 * 1024:
            raise ValueError('LSP document exceeds 512 KiB')
        raw = path.read_bytes()
        text = source_text(raw, str(path))
        digest = hashlib.sha256(raw).hexdigest()
        if previous and previous[1] == digest:
            return previous
        # Drain previously queued diagnostics before advancing the document version.
        while not rpc.incoming.empty():
            rpc.pump(deadline, self.check)
        version = previous[0] + 1 if previous else 1
        document = (version, digest, text)
        session['documents'][uri] = document
        session['sync_times'][uri] = time.monotonic()
        # Dependency diagnostics may also have changed after this edit.
        session['diagnostics'].clear()
        if previous:
            change = {'text': text}
            sync = session['capabilities'].get('textDocumentSync', 1)
            kind = sync.get('change', 1) if isinstance(sync, dict) else sync
            if kind == 2:
                old_lines = previous[2].split('\n')
                change['range'] = {'start': {'line': 0, 'character': 0}, 'end': {
                    'line': len(old_lines) - 1, 'character': len(old_lines[-1].encode('utf-16-le')) // 2}}
            rpc.notify('textDocument/didChange', {'textDocument': {'uri': uri, 'version': version},
                       'contentChanges': [change]}, deadline, self.check)
        else:
            rpc.notify('textDocument/didOpen', {'textDocument': {'uri': uri, 'languageId': ({'.tsx': 'typescriptreact', '.jsx': 'javascriptreact', '.js': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript'}.get(path.suffix.lower(), server.language)),
                       'version': version, 'text': text}}, deadline, self.check)
        rpc.notify('workspace/didChangeWatchedFiles', {'changes': [{'uri': uri, 'type': 2 if previous else 1}]}, deadline, self.check)
        sync = session['capabilities'].get('textDocumentSync')
        if isinstance(sync, dict) and sync.get('save'):
            save = {'textDocument': {'uri': uri}}
            if isinstance(sync['save'], dict) and sync['save'].get('includeText'):
                save['text'] = text
            rpc.notify('textDocument/didSave', save, deadline, self.check)
        return document

    def _locations(self, value):
        result = []
        items = [value] if isinstance(value, dict) else value or []
        omitted = max(0, len(items) - 100)
        for item in items[:100]:
            self.check()
            try:
                uri = urlparse(item.get('uri') or item['targetUri'])
                if uri.scheme != 'file' or uri.netloc not in ('', 'localhost'):
                    raise ValueError('Non-local location')
                path = self.guard.resolve(uri_path(item.get('uri') or item['targetUri']))
                span = item.get('range') or item['targetSelectionRange']
                if not path.is_file() or path.stat().st_size > 512 * 1024:
                    raise ValueError('Location source unavailable')
                raw = path.read_bytes()
                lines = source_text(raw, str(path)).split('\n')
                for position in (span['start'], span['end']):
                    row, column = position['line'], position['character']
                    if type(row) is not int or type(column) is not int or not 0 <= row < len(lines):
                        raise ValueError('Location outside source')
                    if not 0 <= column <= len(lines[row].rstrip('\r').encode('utf-16-le')) // 2:
                        raise ValueError('Location outside source')
                result.append({'path': path.relative_to(self.guard.root).as_posix(),
                    'line': span['start']['line'] + 1, 'character': span['start']['character'] + 1,
                    'end_line': span['end']['line'] + 1, 'end_character': span['end']['character'] + 1,
                    'content_hash': hashlib.sha256(raw).hexdigest()})
            except (OSError, ValueError, KeyError, UnicodeError):
                omitted += 1
        return result, omitted

    def execute(self, operation, file_path, line=1, character=1, *, timeout=None):
        self.check()
        if operation not in ('definition', 'references', 'diagnostics', 'sync'):
            raise ValueError('Unknown LSP operation')
        if type(line) is not int or type(character) is not int or min(line, character) < 1:
            raise ValueError('LSP line and character must be positive integers (1-based UTF-16)')
        path = self.guard.resolve(file_path)
        deadline = time.monotonic() + min(timeout or self.config.timeout_seconds, self.remaining_seconds())
        base = {'operation': operation, 'path': path.relative_to(self.guard.root).as_posix(),
                'backend': 'lsp', 'position_encoding': 'utf-16', 'diagnostics': [], 'locations': []}
        if not self.config.enabled:
            return {**base, 'status': 'unavailable', 'reason': 'LSP disabled by configuration'}
        server = self.config.select(path)
        if server is None:
            return {**base, 'status': 'unavailable', 'reason': 'No installed language server for ' + path.suffix}
        root = project_root(self.guard, path, server.language, markers=server.project_markers, aggregate=server.aggregate_root)
        key = server.name if root == self.guard.root else server.name + '@' + str(root)
        base.update(server=server.name, project_root=root.relative_to(self.guard.root).as_posix())
        while not self.lock.acquire(timeout=.02):
            self.check()
            if time.monotonic() >= deadline:
                return {**base, 'status': 'timeout', 'reason': 'LSP service busy'}
        try:
            session = self._session(server, root, key, deadline)
            # A query on B must not observe an old opened buffer for externally
            # edited A. Disk is authoritative for every previously opened file.
            for opened_uri in list(session['documents']):
                if opened_uri != path.as_uri():
                    opened_path = self.guard.resolve(uri_path(opened_uri))
                    self._sync(session, server, opened_path, deadline)
            document = self._sync(session, server, path, deadline)
            if document is None:
                return {**base, 'status': 'deleted'}
            base.update(version=document[0], content_hash=document[1])
            uri = path.as_uri()
            rpc = session['rpc']
            if operation == 'diagnostics':
                if session['capabilities'].get('diagnosticProvider') is not None:
                    report = rpc.request('textDocument/diagnostic', {'textDocument': {'uri': uri}}, deadline, self.check)
                    diagnostics = (report or {}).get('items', [])
                    # Pull responses correlate with a request sent after sync,
                    # but the LSP result carries no document-version proof.
                    base['diagnostics_versioned'] = False
                    base['diagnostic_freshness'] = 'requested_after_sync'
                else:
                    while uri not in session['diagnostics']:
                        rpc.pump(deadline, self.check)
                    # Servers without versioned diagnostics can publish syntax,
                    # semantic and suggestion batches separately. Allow a bounded
                    # settling interval, without pretending this proves freshness.
                    while not session['diagnostics'][uri][2] and time.monotonic() < max(
                            session['sync_times'][uri] + .5, session['diagnostic_times'][uri] + .2):
                        rpc.pump(deadline, self.check)
                    _, diagnostics, versioned = session['diagnostics'][uri]
                    base['diagnostics_versioned'] = versioned
                base['diagnostics'] = [{key: item[key] for key in ('range', 'severity', 'message', 'source', 'code')
                                       if key in item} for item in diagnostics[:20]]
                for item in base['diagnostics']:
                    item['message'] = str(item.get('message', ''))[:1000]
                base['truncated'] = len(diagnostics) > 20
                base.setdefault('diagnostic_freshness', 'server_versioned' if base['diagnostics_versioned']
                                else 'unversioned_after_sync')
            elif operation in ('definition', 'references'):
                capability = session['capabilities'].get(operation + 'Provider')
                if capability is None or capability is False:
                    try:
                        while capability is None or capability is False:
                            rpc.pump(deadline, self.check)
                            capability = session['capabilities'].get(operation + 'Provider')
                    except TimeoutError:
                        return {**base, 'status': 'initializing',
                                'reason': 'Server has not advertised ' + operation + ' capability yet'}
                lines = document[2].split('\n')
                if line > len(lines) or character - 1 > len(lines[line - 1].encode('utf-16-le')) // 2:
                    raise ValueError('Position outside document')
                params = {'textDocument': {'uri': uri}, 'position': {'line': line - 1, 'character': character - 1}}
                if operation == 'references':
                    params['context'] = {'includeDeclaration': True}
                value = rpc.request('textDocument/' + operation, params, deadline, self.check)
                base['locations'], base['omitted_locations'] = self._locations(value)
            self.check()
            if hashlib.sha256(self.guard.ensure_within(path).read_bytes()).hexdigest() != document[1]:
                return {**base, 'status': 'source_changed', 'diagnostics': [], 'locations': []}
            return {**base, 'status': 'ok'}
        except Initializing as exc:
            return {**base, 'status': 'initializing', 'reason': str(exc)}
        except (CancelledError, ExecutionStopped):
            self._drop(key)
            raise
        except Exception as exc:
            self._drop(key)
            self.check()
            return {**base, 'status': 'timeout' if isinstance(exc, TimeoutError) else 'unavailable',
                    'reason': f'{type(exc).__name__}: {exc}', 'diagnostics': [], 'locations': []}
        finally:
            self.lock.release()

    def _drop(self, name, *, graceful=False):
        session = self.sessions.pop(name, None)
        if session:
            session['startup_timer'].cancel()
            session['rpc'].close(graceful=graceful)

    def close(self):
        with self.lock:
            for name in list(self.sessions):
                self._drop(name, graceful=True)
