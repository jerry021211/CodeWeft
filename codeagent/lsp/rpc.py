"""Bounded stdio JSON-RPC transport with cancellable reads AND writes."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time


class RpcError(RuntimeError):
    pass


class RpcClient:
    def __init__(self, command, workspace, on_notification, on_request=None):
        self.process = subprocess.Popen(command, cwd=workspace, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            **({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == 'nt' else {'start_new_session': True}))
        from codeagent.lsp.process_owner import ProcessOwner
        self.process_owner = ProcessOwner(self.process)
        self.on_notification = on_notification
        self.on_request = on_request
        self.incoming = queue.Queue(maxsize=256)
        self.outgoing = queue.Queue()
        self.closed = threading.Event()
        self.counter = 0
        self.reader = threading.Thread(target=self._read, name='lsp-reader', daemon=True)
        self.writer = threading.Thread(target=self._write, name='lsp-writer', daemon=True)
        self.reader.start()
        self.writer.start()

    def _read(self):
        try:
            while not self.closed.is_set():
                headers = {}
                size = 0
                while True:
                    line = self.process.stdout.readline(8193)
                    size += len(line)
                    if not line or size > 8192:
                        raise RpcError('Language server closed or sent invalid headers')
                    if line in (b'\r\n', b'\n'):
                        break
                    key, value = line.decode('ascii').split(':', 1)
                    headers[key.lower()] = value.strip()
                length = int(headers['content-length'])
                if not 0 < length <= 8 * 1024 * 1024:
                    raise RpcError('Language server message exceeds limit')
                body = self.process.stdout.read(length)
                if len(body) != length:
                    raise RpcError('Truncated language server message')
                message = json.loads(body)
                if not isinstance(message, dict):
                    raise RpcError('Invalid JSON-RPC message')
                self.incoming.put_nowait(message)
        except Exception as exc:
            self.read_error = exc

    def _write(self):
        while not self.closed.is_set():
            item = self.outgoing.get()
            if item is None:
                return
            payload, done, errors = item
            try:
                self.process.stdin.write(payload)
                self.process.stdin.flush()
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

    def _check(self, deadline, check):
        check()
        if time.monotonic() >= deadline:
            raise TimeoutError('Language server deadline exceeded')
        if self.process.poll() is not None or self.closed.is_set():
            raise RpcError('Language server exited')

    def send(self, message, deadline, check):
        self._check(deadline, check)
        body = json.dumps({'jsonrpc': '2.0', **message}, ensure_ascii=False).encode('utf-8')
        done, errors = threading.Event(), []
        self.outgoing.put((f'Content-Length: {len(body)}\r\n\r\n'.encode() + body, done, errors))
        while not done.wait(0.02):
            self._check(deadline, check)
        self._check(deadline, check)
        if errors:
            raise RpcError('Language server write failed') from errors[0]

    def notify(self, method, params, deadline, check):
        self.send({'method': method, 'params': params}, deadline, check)

    def pump(self, deadline, check):
        self._check(deadline, check)
        try:
            message = self.incoming.get(timeout=min(0.02, max(0., deadline - time.monotonic())))
        except queue.Empty:
            if hasattr(self, 'read_error'):
                raise RpcError('Language server protocol failed') from self.read_error
            return None
        if 'method' in message:
            if 'id' in message:
                method = message['method']
                if self.on_request and method in ('workspace/configuration', 'workspace/workspaceFolders',
                        'client/registerCapability', 'client/unregisterCapability', 'window/workDoneProgress/create'):
                    result = self.on_request(method, message.get('params', {}))
                elif method == 'workspace/configuration':
                    result = [{} for _ in message.get('params', {}).get('items', [])]
                elif method == 'workspace/workspaceFolders':
                    result = None
                elif method in ('client/registerCapability', 'client/unregisterCapability', 'window/workDoneProgress/create'):
                    result = None
                else:
                    self.send({'id': message['id'], 'error': {'code': -32601, 'message': 'Unsupported client request'}}, deadline, check)
                    return None
                self.send({'id': message['id'], 'result': result}, deadline, check)
            else:
                self.on_notification(message['method'], message.get('params', {}))
            return None
        return message

    def request(self, method, params, deadline, check):
        identifier = self.start_request(method, params, deadline, check)
        return self.await_response(identifier, method, deadline, check)

    def start_request(self, method, params, deadline, check):
        self.counter += 1
        identifier = self.counter
        self.send({'id': identifier, 'method': method, 'params': params}, deadline, check)
        return identifier

    def await_response(self, identifier, method, deadline, check, *, cancel_on_timeout=True):
        try:
            while True:
                message = self.pump(deadline, check)
                if message and message.get('id') == identifier:
                    if 'error' in message:
                        raise RpcError('Language server rejected ' + method + ': ' + str(message['error'].get('message', ''))[:500])
                    return message.get('result')
        except BaseException as exc:
            # Cancellation is best effort; caller closes the process on failure.
            if cancel_on_timeout or not isinstance(exc, TimeoutError):
                try:
                    self.notify('$/cancelRequest', {'id': identifier}, time.monotonic() + .1, lambda: None)
                except Exception:
                    pass
            raise

    def close(self, *, graceful=True):
        if self.closed.is_set():
            return
        self.process_owner.prepare_close()
        try:
            if graceful and self.process.poll() is None:
                deadline = time.monotonic() + .5
                self.request('shutdown', None, deadline, lambda: None)
                self.notify('exit', None, deadline, lambda: None)
                self.process.wait(timeout=.5)
        except Exception:
            pass
        finally:
            self.closed.set()
            self.process_owner.close()
            if self.process.poll() is None:
                from codeagent.tools.bash import BashTool
                BashTool._stop_process(self.process)
                if self.process.poll() is None:
                    self.process.kill()
            self.process.wait(timeout=2)
            self.outgoing.put(None)
            self.reader.join(timeout=1)
            self.writer.join(timeout=1)
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    stream.close()
                except OSError:
                    # Windows may reject the final buffered flush after an
                    # abrupt server exit. The owned process is already reaped.
                    pass
