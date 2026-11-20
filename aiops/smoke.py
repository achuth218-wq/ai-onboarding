"""Bounded protocol probes. No automatic retry of side-effecting requests."""
import json
import os
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .common import Error, required


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, method="GET", body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})}
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with build_opener(NoRedirects()).open(req, timeout=30) as response:
            content_type = response.headers.get("Content-Type", "")
            # Read SSE by event rather than waiting forever for a persistent HTTP stream.
            if "text/event-stream" in content_type:
                chunks, size, started = [], 0, time.monotonic()
                for _ in range(2048):
                    raw = response.readline(65536)
                    if not raw:
                        break
                    size += len(raw)
                    required(size <= 1048576 and time.monotonic() - started < 30, "SSE probe exceeded limits")
                    line = raw.decode().strip()
                    if line.startswith("data:"):
                        event = line[5:].strip()
                        if event == "[DONE]":
                            break
                        try:
                            obj = json.loads(event)
                        except ValueError:
                            raise Error("Invalid JSON in SSE event") from None
                        chunks.append(obj)
                        if "result" in obj or "error" in obj or any(c.get("finish_reason") for c in obj.get("choices", [])):
                            break
                return response.status, dict(response.headers), {"events": chunks}
            raw = response.read(1048577)
            required(len(raw) <= 1048576, "Probe response too large")
            return response.status, dict(response.headers), json.loads(raw) if raw else {}
    except HTTPError as exc:
        return exc.code, dict(exc.headers), {}


def rpc(url, method, params, headers, identifier="onboarding"):
    body = {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
    status, response_headers, result = request(url, "POST", body, headers)
    required(status == 200, f"{method} returned HTTP {status}")
    if "events" in result:
        result = next((x for x in result["events"] if x.get("id") == identifier), {})
    required(result.get("id") == identifier and "result" in result and "error" not in result,
             f"{method} returned an invalid JSON-RPC result")
    return result["result"], response_headers


def probe(resource, env, auth):
    spec = resource["spec"]
    base = env["proxy_url"].rstrip("/") + spec["path"]
    kind = resource["kind"]
    if kind == "LLM":
        body = {"model": resource["metadata"]["name"], "messages": [{"role": "user", "content": spec["smoke"]["prompt"]}]}
        status, _, _ = request(base + "/chat/completions", "POST", body)
        required(status in (401, 403), "LLM route did not reject anonymous access")
        status, _, result = request(base + "/chat/completions", "POST", body, auth)
        required(status == 200 and result.get("choices"), "LLM generation failed")
        if spec["smoke"]["stream"]:
            status, _, result = request(base + "/chat/completions", "POST", {**body, "stream": True}, auth)
            required(status == 200 and any(x.get("choices") for x in result.get("events", [])), "LLM streaming failed")
    elif kind == "MCP":
        body = {"jsonrpc": "2.0", "id": "anonymous", "method": "initialize", "params": {
            "protocolVersion": spec["protocol_version"], "capabilities": {}, "clientInfo": {"name": "aiops", "version": "1.0"}}}
        status, _, _ = request(base, "POST", body)
        required(status in (401, 403), "MCP route did not reject anonymous access")
        result, response_headers = rpc(base, "initialize", body["params"], auth)
        required(result.get("protocolVersion") == spec["protocol_version"], "MCP negotiated an unexpected revision")
        headers = {**auth, "MCP-Protocol-Version": result["protocolVersion"]}
        session = next((v for k, v in response_headers.items() if k.lower() == "mcp-session-id"), None)
        if session:
            headers["Mcp-Session-Id"] = session
        try:
            status, _, _ = request(base, "POST", {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers)
            required(status in (200, 202, 204), "MCP initialized notification failed")
            discovered = []
            params = {}
            for _ in range(20):
                result, _ = rpc(base, "tools/list", params, headers)
                discovered.extend(x["name"] for x in result.get("tools", []))
                if not result.get("nextCursor"):
                    break
                params = {"cursor": result["nextCursor"]}
            else:
                raise Error("MCP tools pagination exceeded limit")
            required(set(discovered) == set(spec["smoke"]["expected_tools"]), "MCP visible tool set differs from the approved contract")
            if "readonly_call" in spec["smoke"]:
                result, _ = rpc(base, "tools/call", spec["smoke"]["readonly_call"], headers)
                required(not result.get("isError"), "Read-only MCP call failed")
        finally:
            if session:
                request(base, "DELETE", headers=headers)
    elif kind == "A2A":
        status, _, _ = request(base + "/.well-known/agent-card.json")
        required(status in (401, 403), "Agent Card did not reject anonymous access")
        status, _, card = request(base + "/.well-known/agent-card.json", headers=auth)
        required(status == 200 and card.get("protocolVersion") == spec["protocol_version"], "A2A Agent Card version mismatch")
        required(set(spec["smoke"]["expected_skills"]) <= {x["id"] for x in card.get("skills", [])}, "A2A skills missing")
        urls = [card.get("url", "")] + [x["url"] for x in card.get("additionalInterfaces", [])]
        expected = urlsplit(base)
        for url in urls:
            parsed = urlsplit(url)
            required(parsed.scheme == expected.scheme and parsed.netloc == expected.netloc
                     and (parsed.path == expected.path or parsed.path.startswith(expected.path + "/")), "Agent Card exposes an endpoint outside its gateway route")
        if "readonly_message" in spec["smoke"]:
            rpc(base + "/", "message/send", {"message": {"kind": "message", "messageId": str(uuid.uuid4()),
                "role": "user", "parts": [{"kind": "text", "text": spec["smoke"]["readonly_message"]}]}}, auth)
    return {"resource": resource["metadata"]["name"], "kind": kind, "status": "passed"}


def smoke(bundle, environment):
    env = json.loads((Path(bundle) / environment / "environment.json").read_text())
    value = os.environ.get("SMOKE_AUTH_VALUE", "")
    required(value and "\n" not in value and "\r" not in value, "SMOKE_AUTH_VALUE is required")
    auth = {env["smoke_auth_header"]: value}
    resources = json.loads((Path(bundle) / "catalog.json").read_text())
    return [probe(resource, env, auth) for resource in resources]
