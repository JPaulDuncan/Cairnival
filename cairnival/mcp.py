"""MCP — the Model Context Protocol, both ends.

Two capabilities live here:

* **Consuming** other MCP servers: an agent registers servers (an HTTP endpoint
  or a stdio command), lists their tools, and calls them from its tool loop —
  so an agent can reach any MCP tool in the world, not just the ones it wrote.
* **Serving** its own tools as MCP is done in ``agent_app`` at ``POST /mcp``,
  using ``tool_input_schema`` / ``call_shared_tool`` from this module — so any
  MCP client (another Cairnival agent, Claude Desktop, Codex) can use an
  agent's tools.

The wire format is JSON-RPC 2.0. HTTP servers get one request/response POST;
stdio servers get newline-delimited JSON over a short-lived child process that
is torn down (whole process group) after each exchange, so nothing lingers.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .llm import _terminate_tree

PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "cairnival", "version": "1"}


class MCPError(RuntimeError):
    pass


# --- server configuration --------------------------------------------------

@dataclass
class MCPServerConfig:
    name: str
    transport: str = "http"  # http | stdio
    url: str = ""            # http transport
    command: str = ""        # stdio transport
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "url": self.url,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MCPServerConfig":
        transport = str(d.get("transport") or ("stdio" if d.get("command") else "http"))
        return cls(
            name=str(d.get("name", "")).strip(),
            transport=transport,
            url=str(d.get("url", "")).rstrip("/"),
            command=str(d.get("command", "")),
            args=[str(a) for a in d.get("args", [])],
            env={str(k): str(v) for k, v in (d.get("env") or {}).items()},
            enabled=bool(d.get("enabled", True)),
        )


# --- transports ------------------------------------------------------------

def _http_session(cfg: MCPServerConfig, ops: list[tuple[str, dict]], timeout: int = 30) -> list[dict]:
    """initialize, then run each (method, params) against an HTTP MCP server."""
    def rpc(method: str, params: dict, rid: int) -> dict:
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        try:
            r = httpx.post(cfg.url, json=payload, timeout=timeout,
                           headers={"accept": "application/json"})
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MCPError(f"{cfg.name}: {exc}") from exc
        if data.get("error"):
            raise MCPError(f"{cfg.name}: {data['error'].get('message', 'error')}")
        return data.get("result", {})

    rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": _CLIENT_INFO}, 1)
    return [rpc(method, params, i) for i, (method, params) in enumerate(ops, start=2)]


def _stdio_converse(proc: subprocess.Popen, ops: list[tuple[str, dict]]) -> list[dict]:
    def send(obj: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv_result(rid: int) -> dict:
        assert proc.stdout is not None
        # skip notifications / log lines until the response with our id arrives
        while True:
            line = proc.stdout.readline()
            if not line:
                raise MCPError("stdio MCP server closed the connection")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") != rid:
                continue
            if msg.get("error"):
                raise MCPError(str(msg["error"].get("message", "error")))
            return msg.get("result", {})

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                     "clientInfo": _CLIENT_INFO}})
    recv_result(1)
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    results = []
    for i, (method, params) in enumerate(ops, start=2):
        send({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}})
        results.append(recv_result(i))
    return results


def _stdio_session(cfg: MCPServerConfig, ops: list[tuple[str, dict]], timeout: int = 30) -> list[dict]:
    if not cfg.command:
        raise MCPError(f"{cfg.name}: stdio server has no command")
    argv = [cfg.command, *cfg.args]
    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            env=dict(os.environ, **cfg.env), **popen_kwargs,
        )
    except OSError as exc:
        raise MCPError(f"{cfg.name}: cannot start '{cfg.command}': {exc}") from exc

    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["result"] = _stdio_converse(proc, ops)
        except Exception as exc:  # noqa: BLE001 — reported to the caller
            box["error"] = exc

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join(timeout)
    timed_out = th.is_alive()
    _terminate_tree(proc)  # unblocks a stuck reader and reaps the whole group
    if timed_out:
        th.join(2)
        raise MCPError(f"{cfg.name}: stdio server timed out after {timeout}s")
    if "error" in box:
        raise box["error"]
    return box.get("result", [])


def _session(cfg: MCPServerConfig, ops: list[tuple[str, dict]], timeout: int = 30) -> list[dict]:
    if cfg.transport == "stdio":
        return _stdio_session(cfg, ops, timeout)
    if not cfg.url:
        raise MCPError(f"{cfg.name}: http server has no url")
    return _http_session(cfg, ops, timeout)


def _content_text(result: dict) -> str:
    """Flatten an MCP tools/call result into plain text."""
    parts: list[str] = []
    for item in result.get("content", []):
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(json.dumps(item))
    text = "\n".join(p for p in parts if p)
    if result.get("isError"):
        return f"[tool error] {text}" if text else "[tool error]"
    return text


# --- the registry an agent uses to consume MCP servers ---------------------

class MCPRegistry:
    def __init__(self, path: Path, cfg, timeout: int = 30):
        self.path = Path(path)
        self.cfg = cfg
        self.timeout = timeout
        self.servers: list[MCPServerConfig] = []

    def load(self) -> list[MCPServerConfig]:
        servers: dict[str, MCPServerConfig] = {}
        # 1. env shorthand: MCP_SERVERS="name=http://host/mcp, other=..."
        for item in getattr(self.cfg, "mcp_servers", []) or []:
            if "=" in item:
                name, url = item.split("=", 1)
                name = name.strip()
                if name:
                    servers[name] = MCPServerConfig(name=name, transport="http", url=url.strip().rstrip("/"))
        # 2. the agent's own mcp.json (full configs; wins over env shorthand)
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for d in data:
                    s = MCPServerConfig.from_dict(d)
                    if s.name:
                        servers[s.name] = s
            except (ValueError, OSError):
                pass
        self.servers = list(servers.values())
        return self.servers

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([s.to_dict() for s in self.servers], indent=2), encoding="utf-8"
        )

    def get(self, name: str) -> MCPServerConfig | None:
        return next((s for s in self.servers if s.name == name), None)

    def add(self, server: MCPServerConfig) -> None:
        self.load()
        # a config-file entry only; drop any existing with the same name
        self.servers = [s for s in self.servers if s.name != server.name] + [server]
        self.save()

    def remove(self, name: str) -> bool:
        self.load()
        before = len(self.servers)
        self.servers = [s for s in self.servers if s.name != name]
        if len(self.servers) != before:
            self.save()
            return True
        return False

    def list_tools(self) -> list[dict]:
        """Every tool across all enabled servers, name-spaced ``server/tool``.
        A server that can't be reached contributes an ``error`` entry rather
        than breaking the listing."""
        if not self.servers:
            self.load()
        out: list[dict] = []
        for s in self.servers:
            if not s.enabled:
                continue
            try:
                (result,) = _session(s, [("tools/list", {})], self.timeout)
                for t in result.get("tools", []):
                    out.append({
                        "server": s.name,
                        "name": str(t.get("name", "")),
                        "description": str(t.get("description", "")),
                        "inputSchema": t.get("inputSchema", {}),
                    })
            except MCPError as exc:
                out.append({"server": s.name, "name": "", "error": str(exc)})
        return out

    def call(self, server_name: str, tool_name: str, arguments: dict) -> str:
        server = self.get(server_name) or next(
            (s for s in (self.servers or self.load()) if s.name == server_name), None
        )
        if server is None:
            raise MCPError(f"no MCP server named '{server_name}'")
        (result,) = _session(server, [("tools/call", {"name": tool_name, "arguments": arguments})], self.timeout)
        return _content_text(result)

    def catalog(self) -> str:
        """A model/human-readable list of reachable MCP tools."""
        tools = self.list_tools()
        if not tools:
            return ""
        lines: list[str] = []
        for t in tools:
            if t.get("error"):
                lines.append(f"- {t['server']}: (unreachable — {t['error']})")
            else:
                lines.append(f"- {t['server']}/{t['name']}: {t.get('description', '')}".rstrip())
        return "\n".join(lines)


# --- helpers for SERVING the agent's own tools as MCP ----------------------

def tool_input_schema(tool) -> dict:
    """A JSON Schema for a registered tool: its declared UI inputs as string
    properties, or a single free-text ``args`` field."""
    if getattr(tool, "ui_inputs", None):
        return {
            "type": "object",
            "properties": {name: {"type": "string"} for name in tool.ui_inputs},
            "required": list(tool.ui_inputs),
        }
    return {
        "type": "object",
        "properties": {"args": {"type": "string", "description": "command-line arguments"}},
    }


def tool_to_mcp(tool) -> dict:
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool_input_schema(tool),
    }


def call_shared_tool(registry, tool, arguments: dict) -> tuple[bool, str]:
    """Run one of the agent's own tools from an MCP ``tools/call``, mapping the
    arguments to the script's positional args (declared UI inputs in order, or
    a single ``args`` string)."""
    from .tools import parse_args

    if getattr(tool, "ui_inputs", None):
        args = [str(arguments.get(name, "")) for name in tool.ui_inputs]
    else:
        args = parse_args(str(arguments.get("args", "")))
    result = registry.run_tool(tool.name, args, by="mcp")
    return result.ok, result.render(registry.cfg.tools_output_limit)
