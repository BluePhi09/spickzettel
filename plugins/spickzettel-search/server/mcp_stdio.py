"""Minimal stdlib-only MCP stdio server (JSON-RPC 2.0, newline-delimited).

THIS FILE IS SHARED: the canonical copy lives in shared/mcp_stdio.py and is copied
into each plugin by scripts/sync-shared.sh. Edit the shared copy only.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable, Dict, List

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


class Server:
    def __init__(self, name: str, version: str, instructions: str = ""):
        self.name, self.version, self.instructions = name, version, instructions
        self.tools: List[Dict[str, Any]] = []
        self.handlers: Dict[str, Callable[[Dict[str, Any]], str]] = {}

    def tool(self, schema: Dict[str, Any], handler: Callable[[Dict[str, Any]], str]) -> None:
        self.tools.append({"name": schema["name"], "description": schema["description"],
                           "inputSchema": schema["parameters"]})
        self.handlers[schema["name"]] = handler

    def _handle(self, msg: Dict[str, Any]):
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
            result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": self.name, "version": self.version}}
            if self.instructions:
                result["instructions"] = self.instructions
            return result
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools}
        if method == "tools/call":
            name = params.get("name")
            handler = self.handlers.get(name)
            if handler is None:
                raise KeyError(f"Unknown tool: {name}")
            try:
                text = handler(params.get("arguments") or {})
                is_error = False
                try:
                    parsed = json.loads(text)
                    is_error = isinstance(parsed, dict) and parsed.get("success") is False and not parsed.get("done")
                except ValueError:
                    pass
            except Exception as exc:  # never crash the server on a tool bug
                text = json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"})
                is_error = True
                traceback.print_exc(file=sys.stderr)
            return {"content": [{"type": "text", "text": text}], "isError": is_error}
        raise NotImplementedError(method)

    def run(self) -> None:
        try:
            self._loop()
        except (BrokenPipeError, KeyboardInterrupt):
            pass

    def _loop(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            if "id" not in msg:  # notification (e.g. notifications/initialized)
                continue
            try:
                result = self._handle(msg)
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            except NotImplementedError as exc:
                self._send({"jsonrpc": "2.0", "id": msg["id"],
                            "error": {"code": -32601, "message": f"Method not found: {exc}"}})
            except Exception as exc:
                self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(exc)}})

    @staticmethod
    def _send(obj: Dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
