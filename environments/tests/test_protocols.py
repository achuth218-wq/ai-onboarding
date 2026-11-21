import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aiops.build import ROOT, load_source
from aiops.common import Error
from aiops.smoke import probe


class MockGateway(BaseHTTPRequestHandler):
    anonymous_allowed = False
    unexpected_tool = False
    leak_agent_url = False
    calls = []

    def log_message(self, *args):
        pass

    def respond(self, status, value=None, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, val in (headers or {}).items():
            self.send_header(key, val)
        self.end_headers()
        if value is not None:
            self.wfile.write(json.dumps(value).encode())

    def authorized(self):
        if self.headers.get("Authorization") == "Bearer test" or self.anonymous_allowed:
            return True
        self.respond(401)
        return False

    def do_GET(self):
        if not self.authorized():
            return
        self.respond(200, {"protocolVersion": "0.3.0", "skills": [{"id": "answer_support_question"}],
                           "url": "https://upstream.invalid/" if self.leak_agent_url else
                           f"http://127.0.0.1:{self.server.server_port}/a2a/support/"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if not self.authorized():
            return
        self.calls.append(body.get("method", "chat"))
        if "chat/completions" in self.path:
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"ready"},"finish_reason":null}]}\n\n')
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
                return
            self.respond(200, {"choices": [{"message": {"content": "ready"}}]})
            return
        method = body["method"]
        if method == "initialize":
            self.respond(200, {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-11-25"}}, {"Mcp-Session-Id": "session-one"})
        elif method == "notifications/initialized":
            if self.headers.get("Mcp-Session-Id") != "session-one":
                self.respond(400)
            else:
                self.respond(202)
        elif method == "tools/list":
            if self.headers.get("MCP-Protocol-Version") != "2025-11-25" or self.headers.get("Mcp-Session-Id") != "session-one":
                self.respond(400)
                return
            names = ["ticket_get", "ticket_search"] + (["ticket_delete"] if self.unexpected_tool else [])
            self.respond(200, {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": x} for x in names]}})
        elif method == "message/send":
            self.respond(200, {"jsonrpc": "2.0", "id": body["id"], "result": {"kind": "message"}})
        else:
            self.respond(400)

    def do_DELETE(self):
        self.calls.append("session/delete")
        self.respond(204)


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockGateway)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        resources, envs = load_source(ROOT)
        self.resources = {x["kind"]: x for x in resources}
        self.env = {**envs["aws-internal-dev"], "proxy_url": f"http://127.0.0.1:{self.server.server_port}"}
        self.auth = {"Authorization": "Bearer test"}
        MockGateway.calls = []
        MockGateway.anonymous_allowed = MockGateway.unexpected_tool = MockGateway.leak_agent_url = False

    def test_llm_generation_and_streaming(self):
        self.assertEqual(probe(self.resources["LLM"], self.env, self.auth)["status"], "passed")
        self.assertEqual(MockGateway.calls, ["chat", "chat"])

    def test_mcp_handshake_session_and_discovery(self):
        probe(self.resources["MCP"], self.env, self.auth)
        self.assertEqual(MockGateway.calls, ["initialize", "notifications/initialized", "tools/list", "session/delete"])

    def test_mcp_detects_unapproved_visible_tool(self):
        MockGateway.unexpected_tool = True
        with self.assertRaisesRegex(Error, "tool set"):
            probe(self.resources["MCP"], self.env, self.auth)
        self.assertEqual(MockGateway.calls[-1], "session/delete")

    def test_a2a_card_and_opt_in_message(self):
        self.resources["A2A"]["spec"]["smoke"]["readonly_message"] = "Read status"
        probe(self.resources["A2A"], self.env, self.auth)
        self.assertEqual(MockGateway.calls, ["message/send"])

    def test_a2a_rejects_upstream_url_leak(self):
        MockGateway.leak_agent_url = True
        with self.assertRaisesRegex(Error, "outside its gateway"):
            probe(self.resources["A2A"], self.env, self.auth)

    def test_authentication_bypass_fails_verification(self):
        MockGateway.anonymous_allowed = True
        with self.assertRaisesRegex(Error, "anonymous"):
            probe(self.resources["LLM"], self.env, self.auth)
