from __future__ import annotations

import http.server
import socket
import threading
import unittest

from dingtalk_ops_bridge.checks import MonitorDefinition, run_check


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"healthy")

    def log_message(self, _format, *_args):
        pass


class CheckTests(unittest.TestCase):
    def test_command_check_success_and_failure(self):
        success = MonitorDefinition(
            name="ok",
            description="",
            check_type="command",
            check={"type": "command", "command": ["printf", "ready"], "stdoutContains": ["ready"]},
        )
        self.assertTrue(run_check(success).ok)

        failure = MonitorDefinition(
            name="fail",
            description="",
            check_type="command",
            check={"type": "command", "command": ["sh", "-c", "exit 3"]},
        )
        self.assertFalse(run_check(failure).ok)

    def test_tcp_check(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def accept_once():
            connection, _ = server.accept()
            connection.close()
            server.close()

        thread = threading.Thread(target=accept_once, daemon=True)
        thread.start()
        monitor = MonitorDefinition(
            name="tcp",
            description="",
            check_type="tcp",
            check={"type": "tcp", "host": "127.0.0.1", "port": port},
            timeout_seconds=2,
        )
        result = run_check(monitor)
        thread.join(timeout=2)
        self.assertTrue(result.ok, result.message)

    def test_http_check(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            monitor = MonitorDefinition(
                name="http",
                description="",
                check_type="http",
                check={
                    "type": "http",
                    "url": f"http://127.0.0.1:{port}/health",
                    "expectedStatus": [200],
                    "bodyContains": ["healthy"],
                },
                timeout_seconds=2,
            )
            result = run_check(monitor)
            self.assertTrue(result.ok, result.message)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
