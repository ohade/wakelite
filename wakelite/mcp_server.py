from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib import error, request

from .config import API_HOST, API_PORT, MCP_HTTP_HOST, MCP_HTTP_PORT


SERVER_NAME = "wakelite-mcp"
SERVER_VERSION = "2.0.0"
PROTOCOL_VERSION = "2024-11-05"


TOOLS: list[dict[str, Any]] = [
    {
        "name": "wakelite.v1.health.get",
        "description": "Get WakeLite service health",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "wakelite.v1.timer.list",
        "description": "List timers",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "wakelite.v1.timer.create",
        "description": "Create timer (requires idempotency_key)",
        "inputSchema": {
            "type": "object",
            "required": ["idempotency_key", "timer"],
            "properties": {
                "idempotency_key": {"type": "string"},
                "timer": {"type": "object"},
            },
        },
    },
    {
        "name": "wakelite.v1.timer.update",
        "description": "Update timer (requires idempotency_key)",
        "inputSchema": {
            "type": "object",
            "required": ["idempotency_key", "timer_id", "patch"],
            "properties": {
                "idempotency_key": {"type": "string"},
                "timer_id": {"type": "string"},
                "patch": {"type": "object"},
            },
        },
    },
    {
        "name": "wakelite.v1.timer.delete",
        "description": "Delete timer (requires idempotency_key)",
        "inputSchema": {
            "type": "object",
            "required": ["idempotency_key", "timer_id"],
            "properties": {
                "idempotency_key": {"type": "string"},
                "timer_id": {"type": "string"},
            },
        },
    },
    {
        "name": "wakelite.v1.timer.enable",
        "description": "Enable timer (requires idempotency_key)",
        "inputSchema": {
            "type": "object",
            "required": ["idempotency_key", "timer_id"],
            "properties": {
                "idempotency_key": {"type": "string"},
                "timer_id": {"type": "string"},
            },
        },
    },
    {
        "name": "wakelite.v1.timer.disable",
        "description": "Disable timer (requires idempotency_key)",
        "inputSchema": {
            "type": "object",
            "required": ["idempotency_key", "timer_id"],
            "properties": {
                "idempotency_key": {"type": "string"},
                "timer_id": {"type": "string"},
            },
        },
    },
    {
        "name": "wakelite.v1.timer.run_now",
        "description": "Queue timer run now (requires idempotency_key)",
        "inputSchema": {
            "type": "object",
            "required": ["idempotency_key", "timer_id"],
            "properties": {
                "idempotency_key": {"type": "string"},
                "timer_id": {"type": "string"},
            },
        },
    },
    {
        "name": "wakelite.v1.run.list",
        "description": "List run history",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
                "timer_id": {"type": "string"},
            },
        },
    },
    {
        "name": "wakelite.v1.run.logs.get",
        "description": "Get run logs",
        "inputSchema": {
            "type": "object",
            "required": ["run_id"],
            "properties": {"run_id": {"type": "string"}},
        },
    },
    {
        "name": "wakelite.v1.run.abort",
        "description": "Abort running run (requires idempotency_key)",
        "inputSchema": {
            "type": "object",
            "required": ["run_id", "idempotency_key"],
            "properties": {
                "run_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
    },
    {
        "name": "wakelite.v1.alert.ack",
        "description": "Acknowledge incident (requires idempotency_key)",
        "inputSchema": {
            "type": "object",
            "required": ["idempotency_key", "incident_id"],
            "properties": {
                "idempotency_key": {"type": "string"},
                "incident_id": {"type": "integer"},
            },
        },
    },
]


class ServiceUnavailable(RuntimeError):
    pass


def _api_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"http://{API_HOST}:{API_PORT}{path}"
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = request.Request(url=url, method=method, data=data, headers=headers)
    try:
        with request.urlopen(req, timeout=3.0) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"error": body or str(e)}
        raise RuntimeError(parsed.get("error", f"HTTP {e.code}"))
    except Exception as e:
        raise ServiceUnavailable(
            "WakeLite service unavailable. Start runner first (wakelitectl serve)."
        ) from e


def _tool_result(payload: Dict[str, Any], is_error: bool = False) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
        "isError": is_error,
    }


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if name == "wakelite.v1.health.get":
        return _api_request("GET", "/v1/health")
    if name == "wakelite.v1.timer.list":
        return _api_request("GET", "/v1/timers")
    if name == "wakelite.v1.timer.create":
        return _api_request("POST", "/v1/timers", {
            "idempotency_key": arguments["idempotency_key"],
            **arguments.get("timer", {}),
        })
    if name == "wakelite.v1.timer.update":
        timer_id = arguments["timer_id"]
        patch = arguments.get("patch", {})
        return _api_request("PATCH", f"/v1/timers/{timer_id}", {
            "idempotency_key": arguments["idempotency_key"],
            **patch,
        })
    if name == "wakelite.v1.timer.delete":
        timer_id = arguments["timer_id"]
        return _api_request("DELETE", f"/v1/timers/{timer_id}", {
            "idempotency_key": arguments["idempotency_key"],
        })
    if name == "wakelite.v1.timer.enable":
        timer_id = arguments["timer_id"]
        return _api_request("POST", f"/v1/timers/{timer_id}/enable", {
            "idempotency_key": arguments["idempotency_key"],
        })
    if name == "wakelite.v1.timer.disable":
        timer_id = arguments["timer_id"]
        return _api_request("POST", f"/v1/timers/{timer_id}/disable", {
            "idempotency_key": arguments["idempotency_key"],
        })
    if name == "wakelite.v1.timer.run_now":
        timer_id = arguments["timer_id"]
        return _api_request("POST", f"/v1/timers/{timer_id}/run-now", {
            "idempotency_key": arguments["idempotency_key"],
        })
    if name == "wakelite.v1.run.list":
        limit = arguments.get("limit", 100)
        timer_id = arguments.get("timer_id")
        query = f"?limit={int(limit)}"
        if timer_id:
            query += f"&timer_id={timer_id}"
        return _api_request("GET", f"/v1/runs{query}")
    if name == "wakelite.v1.run.logs.get":
        run_id = arguments["run_id"]
        return _api_request("GET", f"/v1/runs/{run_id}/logs")
    if name == "wakelite.v1.run.abort":
        run_id = arguments["run_id"]
        return _api_request("POST", f"/v1/runs/{run_id}/abort", {
            "idempotency_key": arguments["idempotency_key"],
        })
    if name == "wakelite.v1.alert.ack":
        incident_id = int(arguments["incident_id"])
        return _api_request("POST", f"/v1/incidents/{incident_id}/ack", {
            "idempotency_key": arguments["idempotency_key"],
        })

    raise ValueError(f"Unknown tool: {name}")


@dataclass
class MCPProtocol:
    def handle(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = req.get("method")
        req_id = req.get("id")

        # Notifications have no id.
        if req_id is None:
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                },
            }

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS},
            }

        if method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                payload = call_tool(name, arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": _tool_result(payload),
                }
            except ServiceUnavailable as e:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": _tool_result({"error": str(e), "code": "SERVICE_UNAVAILABLE"}, is_error=True),
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": _tool_result({"error": str(e)}, is_error=True),
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: {method}",
            },
        }


def _read_framed_message() -> Optional[Dict[str, Any]]:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode("utf-8").split(":", 1)
        headers[key.strip().lower()] = value.strip()

    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def _write_framed_message(payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def run_stdio() -> None:
    protocol = MCPProtocol()
    while True:
        req = _read_framed_message()
        if req is None:
            return
        resp = protocol.handle(req)
        if resp is not None:
            _write_framed_message(resp)


class MCPHttpHandler(BaseHTTPRequestHandler):
    protocol = MCPProtocol()

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/sse":
            # Placeholder endpoint so clients expecting SSE can detect capability quickly.
            self.send_response(501)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"SSE transport not implemented; use /mcp JSON-RPC or stdio"}')
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/mcp":
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            req = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON"})
            return

        try:
            resp = self.protocol.handle(req)
            if resp is None:
                self._send_json(204, {})
            else:
                self._send_json(200, resp)
        except Exception:
            self._send_json(500, {"error": traceback.format_exc()})


def run_http(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), MCPHttpHandler)
    print(f"WakeLite MCP HTTP listening on http://{host}:{port}/mcp", file=sys.stderr)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="WakeLite MCP server")
    parser.add_argument("--http", action="store_true", help="run MCP HTTP endpoint")
    parser.add_argument("--host", default=MCP_HTTP_HOST)
    parser.add_argument("--port", type=int, default=MCP_HTTP_PORT)
    args = parser.parse_args()

    if args.http:
        run_http(args.host, args.port)
        return

    run_stdio()


if __name__ == "__main__":
    main()
