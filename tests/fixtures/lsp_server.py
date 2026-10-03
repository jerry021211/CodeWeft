"""Deterministic test-only stdio server. This is NOT a Python type checker."""
import json
import sys
import time
import os
import subprocess

child = None

mode = sys.argv[1] if len(sys.argv) > 1 else 'push'
documents = {}
if mode == 'child':
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
    if len(sys.argv) > 2:
        with open(sys.argv[2], 'a', encoding='utf-8') as log:
            log.write(json.dumps({'child_pid': child.pid}) + '\n')


def send(message):
    raw = json.dumps({'jsonrpc': '2.0', **message}, ensure_ascii=False).encode()
    sys.stdout.buffer.write(f'Content-Length: {len(raw)}\r\n\r\n'.encode() + raw)
    sys.stdout.buffer.flush()


def diagnostics(text):
    return [{'range': {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 1}},
             'severity': 1, 'source': 'mock-python', 'message': 'simulated diagnostic: BROKEN 语法'}] if 'BROKEN' in text else []


while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line == b'\r\n':
            break
        key, value = line.decode().split(':', 1)
        headers[key.lower()] = value.strip()
    message = json.loads(sys.stdin.buffer.read(int(headers['content-length'])))
    method = message.get('method')
    params = message.get('params') or {}
    if len(sys.argv) > 2:
        with open(sys.argv[2], 'a', encoding='utf-8') as log:
            log.write(json.dumps(message, ensure_ascii=False) + '\n')
    if method == 'initialize':
        if mode == 'slow_init':
            time.sleep(.3)
        if mode == 'stall_init':
            time.sleep(30)
        if mode == 'exit':
            sys.exit(3)
        if mode == 'malformed':
            sys.stdout.buffer.write(b'Content-Length: 999999999\r\n\r\n')
            sys.stdout.buffer.flush()
            continue
        send({'id': 'server-config', 'method': 'workspace/configuration', 'params': {'items': [{'section': 'python'}]}})
        capabilities = {'definitionProvider': True, 'referencesProvider': True, 'textDocumentSync': 2}
        if mode == 'dynamic':
            capabilities = {'textDocumentSync': 2}
        if mode == 'options':
            capabilities.update(definitionProvider={}, referencesProvider={})
        if mode == 'pull':
            capabilities['diagnosticProvider'] = {}
        send({'id': message['id'], 'result': {'capabilities': capabilities}})
    elif method == 'initialized' and mode == 'dynamic':
        send({'id': 'dynamic-registration', 'method': 'client/registerCapability', 'params': {
            'registrations': [{'id': 'definitions', 'method': 'textDocument/definition', 'registerOptions': {}},
                              {'id': 'references', 'method': 'textDocument/references', 'registerOptions': {}}]}})
    elif method in ('textDocument/didOpen', 'textDocument/didChange'):
        doc = params['textDocument']
        text = doc.get('text', '') if method.endswith('didOpen') else params['contentChanges'][0]['text']
        documents[doc['uri']] = text
        if mode not in ('pull', 'silent'):
            send({'method': 'textDocument/publishDiagnostics', 'params': {'uri': doc['uri'],
                  'version': doc['version'] - 1, 'diagnostics': [{'message': 'STALE'}]}})
            send({'method': 'textDocument/publishDiagnostics', 'params': {'uri': doc['uri'],
                  'version': doc['version'], 'diagnostics': diagnostics(text)}})
    elif method == 'textDocument/didClose':
        documents.pop(params['textDocument']['uri'], None)
    elif method == 'textDocument/diagnostic':
        send({'id': message['id'], 'result': {'kind': 'full', 'items': diagnostics(documents[params['textDocument']['uri']])}})
    elif method in ('textDocument/definition', 'textDocument/references'):
        if mode == 'stall_request':
            time.sleep(30)
        uri = params['textDocument']['uri']
        span = {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 3}}
        send({'id': message['id'], 'result': [{'targetUri': uri, 'targetRange': span, 'targetSelectionRange': span}]})
    elif method == 'shutdown':
        send({'id': message['id'], 'result': None})
    elif method == 'exit':
        sys.exit(0)
