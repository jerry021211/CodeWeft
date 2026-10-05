from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from codeagent.providers import OpenAIModelClient
from codeagent.runtime.activity import ExecutionActivity
from codeagent.runtime.cancellation import CancellationToken, CancelledError


class ModelStreamCancellationTests(unittest.TestCase):
    def test_cancel_interrupts_real_http_stream_without_waiting_for_next_chunk(self):
        ready, release = threading.Event(), threading.Event()
        errors = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"index":0,"delta":{"content":"start"},"finish_reason":null}]}\n\n')
                self.wfile.flush()
                release.wait(10)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        token = CancellationToken()
        activity = ExecutionActivity(token)
        client = OpenAIModelClient(
            base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key="",
            stream=True, activity=activity, on_text=lambda _: ready.set(),
        )

        def call():
            try:
                client.create_message(model="local-test", system="", messages=[], tools=[], max_tokens=8)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=call, daemon=True)
        closer = threading.Thread(target=activity.interrupt_request, daemon=True)
        worker.start()
        try:
            self.assertTrue(ready.wait(3), "Stream never produced its first chunk")
            token.cancel()
            closer.start()
            worker.join(2)
            self.assertFalse(worker.is_alive(), "Cancelled reader still waits for server data")
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], CancelledError)
        finally:
            release.set()
            worker.join(3)
            if closer.ident is not None:
                closer.join(3)
            server.shutdown()
            server.server_close()
            server_thread.join(3)
