"""MCP-compatible JSON-RPC 2.0 tool server that runs inside each pinned image.

    python tool_server.py --mode agentic_native            # stdio (MCP stdio transport)
    python tool_server.py --mode auto --http 0.0.0.0:7000  # HTTP POST /rpc

Implements `initialize`, `tools/list` and `tools/call`. Stdlib only, so the images need nothing
beyond Python. The server only ever touches /workspace; ground truth is never in the container.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    from .tools import Backend, ExecResult, WorkspaceTools
except ImportError:  # inside the image the file sits next to tools.py, outside any package
    from tools import Backend, ExecResult, WorkspaceTools  # type: ignore

PROTOCOL_VERSION = "2025-06-18"


class InContainerBackend(Backend):
    def __init__(self, root: str = "/workspace"):
        self.root = os.path.realpath(root)

    def _abs(self, rel: str) -> str:
        p = os.path.realpath(os.path.join(self.root, rel))
        if p != self.root and not p.startswith(self.root + os.sep):
            raise PermissionError(f"path outside workspace: {rel}")
        return p

    def exec(self, argv, timeout):
        try:
            r = subprocess.run(argv, cwd=self.root, capture_output=True, timeout=timeout)
            return ExecResult(r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace"))
        except subprocess.TimeoutExpired as e:
            return ExecResult(-1, ((e.stdout or b"") + (e.stderr or b"")).decode("utf-8", "replace"), timed_out=True)

    def read_bytes(self, rel):
        with open(self._abs(rel), "rb") as f:
            return f.read()

    def write_bytes(self, rel, data):
        p = self._abs(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def list_files(self):
        out = []
        for d, _, files in os.walk(self.root):
            out += [os.path.relpath(os.path.join(d, f), self.root) for f in files]
        return out


class ToolServer:
    def __init__(self, tools: WorkspaceTools):
        self.tools = tools

    def handle(self, req: dict) -> dict | None:
        rid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
        if rid is None:  # notification
            return None
        try:
            if method == "initialize":
                result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "seatrail-sandbox", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": [{"name": s["name"], "description": s["description"],
                                     "inputSchema": s["input_schema"]} for s in self.tools.specs()]}
            elif method == "tools/call":
                r = self.tools.call(params.get("name", ""), params.get("arguments") or {})
                content = [{"type": "text", "text": r.output}]
                if r.image_b64:
                    content.append({"type": "image", "data": r.image_b64, "mimeType": "image/png"})
                result = {"content": content, "isError": r.is_error,
                          "_meta": {"finished": self.tools.finished}}
            else:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}}
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except Exception as e:  # pragma: no cover - defensive
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}}


def _serve_stdio(server: ToolServer) -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        resp = server.handle(json.loads(line))
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


def _serve_http(server: ToolServer, addr: str) -> None:
    host, port = addr.rsplit(":", 1)

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length", 0)))
            resp = server.handle(json.loads(body)) or {}
            data = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    HTTPServer((host, int(port)), H).serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="agentic_native", choices=["auto", "agentic_native"])
    ap.add_argument("--root", default="/workspace")
    ap.add_argument("--http", default="")
    a = ap.parse_args()
    server = ToolServer(WorkspaceTools(InContainerBackend(a.root), mode=a.mode))
    _serve_http(server, a.http) if a.http else _serve_stdio(server)


if __name__ == "__main__":
    main()
