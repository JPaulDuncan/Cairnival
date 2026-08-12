"""MCP both ways, and the response-format contract between agents.

- An agent serves its shared tools at POST /mcp (JSON-RPC).
- An agent consumes an MCP server (HTTP against another agent's /mcp, and a
  stdio server via a tiny fake) and calls its tools from the loop.
- When one agent asks another to do a task, it can specify the reply format
  and the target honors it.
"""

import json
import sys

from fastapi.testclient import TestClient

from cairnival import agentloop, mcp, messaging
from cairnival.agent_app import create_app
from cairnival.config import AgentConfig
from cairnival.instructions import Instruction
from cairnival.memory import Memory
from cairnival.tools import ToolRegistry


def agent_cfg(tmp_path, name):
    cfg = AgentConfig()
    cfg.name = name
    cfg.home = tmp_path / name
    cfg.llm_backend = "echo"
    cfg.tools_enabled = True
    return cfg


# --- serving: the agent's tools as MCP ------------------------------------

def test_mcp_server_lists_and_calls_shared_tools(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    cfg.tool_sharing = "all"
    app = create_app(cfg)
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    reg = ToolRegistry(mem.tools_dir, mem.workspace_dir, cfg)
    reg.write_tool("greet", "greet by name", "bash", 'echo "hello, $1"',
                   ui=True, ui_inputs=["who"])

    with TestClient(app) as client:
        init = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert init.status_code == 200
        assert init.json()["result"]["serverInfo"]["name"] == "cairnival:moth"

        listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = listed.json()["result"]["tools"]
        assert any(t["name"] == "greet" for t in tools)
        greet = next(t for t in tools if t["name"] == "greet")
        assert "who" in greet["inputSchema"]["properties"]

        called = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "greet", "arguments": {"who": "wren"}},
        })
        result = called.json()["result"]
        assert not result["isError"]
        assert "hello, wren" in result["content"][0]["text"]


def test_mcp_server_notification_and_unknown_method(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    app = create_app(cfg)
    with TestClient(app) as client:
        note = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert note.status_code == 202
        bad = client.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "nonsense"})
        assert bad.json()["error"]["code"] == -32601


# --- consuming: HTTP MCP (agent B uses agent A's tools) -------------------

def test_registry_consumes_http_mcp_server(tmp_path, monkeypatch):
    # server agent A
    a_cfg = agent_cfg(tmp_path, "alpha")
    a_cfg.tool_sharing = "all"
    a_app = create_app(a_cfg)
    a_mem = Memory(a_cfg.home, "alpha")
    a_mem.ensure()
    ToolRegistry(a_mem.tools_dir, a_mem.workspace_dir, a_cfg).write_tool(
        "shout", "uppercase a word", "bash", 'echo "$1" | tr a-z A-Z', ui=True, ui_inputs=["word"]
    )

    with TestClient(a_app) as a_client:
        # route the consumer's httpx.post at alpha's /mcp through the test client
        import cairnival.mcp as mcpmod

        def fake_post(url, json=None, timeout=None, headers=None):
            assert url.endswith("/mcp")
            return a_client.post("/mcp", json=json)

        monkeypatch.setattr(mcpmod.httpx, "post", fake_post)

        b_cfg = agent_cfg(tmp_path, "beta")
        b_mem = Memory(b_cfg.home, "beta")
        b_mem.ensure()
        reg = mcp.MCPRegistry(b_mem.mcp_path, b_cfg)
        reg.add(mcp.MCPServerConfig(name="alpha", transport="http", url="http://alpha/mcp"))

        tools = reg.list_tools()
        assert any(t["server"] == "alpha" and t["name"] == "shout" for t in tools)
        out = reg.call("alpha", "shout", {"word": "hi"})
        assert "HI" in out


# --- consuming: a stdio MCP server (tiny fake over stdin/stdout) ----------

_FAKE_STDIO_SERVER = r'''
import sys, json
def send(obj): sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    msg = json.loads(line)
    mid = msg.get("id"); method = msg.get("method")
    if mid is None:
        continue  # a notification
    if method == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"fake","version":"1"}}})
    elif method == "tools/list":
        send({"jsonrpc":"2.0","id":mid,"result":{"tools":[{"name":"echo","description":"echo text","inputSchema":{"type":"object","properties":{"text":{"type":"string"}}}}]}})
    elif method == "tools/call":
        args = msg.get("params",{}).get("arguments",{})
        send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":"echo: "+str(args.get("text",""))}],"isError":False}})
    else:
        send({"jsonrpc":"2.0","id":mid,"error":{"code":-32601,"message":"no"}})
'''


def test_registry_consumes_stdio_mcp_server(tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(_FAKE_STDIO_SERVER)
    cfg = agent_cfg(tmp_path, "moth")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    reg = mcp.MCPRegistry(mem.mcp_path, cfg, timeout=15)
    reg.add(mcp.MCPServerConfig(name="fake", transport="stdio", command=sys.executable, args=[str(script)]))

    tools = reg.list_tools()
    assert any(t["server"] == "fake" and t["name"] == "echo" for t in tools)
    out = reg.call("fake", "echo", {"text": "ping"})
    assert out == "echo: ping"


# --- the mcp action in the loop -------------------------------------------

def test_mcp_action_calls_registered_tool(tmp_path):
    from cairnival.federation import Identity
    from cairnival.llm import build_backend
    from cairnival.pursuits import PursuitBook
    from cairnival.treasury import Ledger
    from cairnival.wake import WakeContext

    script = tmp_path / "fake_mcp.py"
    script.write_text(_FAKE_STDIO_SERVER)
    cfg = agent_cfg(tmp_path, "moth")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    mcp.MCPRegistry(mem.mcp_path, cfg).add(
        mcp.MCPServerConfig(name="fake", transport="stdio", command=sys.executable, args=[str(script)])
    )
    reg = ToolRegistry(mem.tools_dir, mem.workspace_dir, cfg)
    ctx = WakeContext(cfg, mem, mem.load_state(), Identity.load_or_create(mem.keys_dir, "moth"),
                      Ledger(mem.treasury_dir), build_backend(cfg), reg, PursuitBook(cfg.home))
    action = agentloop.Action(kind="mcp", arg="fake/echo", body='{"text": "from the loop"}')
    out = agentloop._observe(action, reg, cfg, agentloop.LoopResult(answer=""), ctx)
    assert out == "echo: from the loop"


# --- response-format contract --------------------------------------------

def test_ask_carries_response_format(tmp_path, monkeypatch):
    from cairnival.federation import Identity
    from cairnival.llm import build_backend
    from cairnival.pursuits import PursuitBook
    from cairnival.treasury import Ledger
    from cairnival.wake import WakeContext

    cfg = agent_cfg(tmp_path, "moth")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    captured = {}

    def fake_deliver(cfg, identity, memory, to, text, **kw):
        captured["to"] = to
        captured["text"] = text
        captured["respond_with"] = kw.get("respond_with", "")
        return True, "direct"

    monkeypatch.setattr(agentloop.messaging, "deliver_note", fake_deliver)
    reg = ToolRegistry(mem.tools_dir, mem.workspace_dir, cfg)
    ctx = WakeContext(cfg, mem, mem.load_state(), Identity.load_or_create(mem.keys_dir, "moth"),
                      Ledger(mem.treasury_dir), build_backend(cfg), reg, PursuitBook(cfg.home))
    body = 'format: JSON {"answer": string}\n---\nWhat is 2+2?'
    action = agentloop.Action(kind="ask", arg="calcbot", body=body)
    out = agentloop._observe(action, reg, cfg, agentloop.LoopResult(answer=""), ctx)
    assert captured["to"] == "calcbot"
    assert captured["text"] == "What is 2+2?"
    assert captured["respond_with"] == 'JSON {"answer": string}'
    assert "reply in the format" in out


def test_respond_with_round_trips_and_reaches_the_prompt(tmp_path):
    # the note delivered with respond_with reaches the receiver's inbox and the
    # solve() prompt tells the model to answer in that format
    cfg = agent_cfg(tmp_path, "moth")
    app = create_app(cfg)
    from cairnival.federation import Identity, seal
    other = Memory(tmp_path / "rustle", "rustle")
    other.ensure()
    rustle = Identity.load_or_create(other.keys_dir, "rustle")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    mem.save_peers({"rustle": {"public_key": rustle.public_key, "public_url": "http://rustle"}})

    with TestClient(app) as client:
        env = seal(rustle, "note", {"text": "give me the weather", "respond_with": 'JSON {"temp": number}'})
        client.post("/api/federation/inbox", json=env.to_dict())

    from cairnival.instructions import pending
    ins = [i for i in pending(mem.inbox_dir) if i.sender == "rustle"][0]
    assert ins.respond_with == 'JSON {"temp": number}'

    # the working prompt carries the format contract
    class CaptureLLM:
        name = "cap"
        def __init__(self): self.seen = []
        def chat(self, system, prompt, think=None):
            self.seen.append(prompt)
            return "```final\n{\"temp\": 12}\n```"
        def describe(self): return "cap"

    from cairnival.pursuits import PursuitBook
    from cairnival.treasury import Ledger
    from cairnival.wake import WakeContext
    llm = CaptureLLM()
    reg = ToolRegistry(mem.tools_dir, mem.workspace_dir, cfg)
    ctx = WakeContext(cfg, mem, mem.load_state(), Identity.load_or_create(mem.keys_dir, "moth"),
                      Ledger(mem.treasury_dir), llm, reg, PursuitBook(cfg.home))
    agentloop.solve(ctx, ins, reg)
    assert any('JSON {"temp": number}' in p for p in llm.seen)
