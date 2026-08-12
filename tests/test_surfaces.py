"""Tool UI surfaces: an agent can give one of its tools a page in the agent UI
— a form for its inputs and its output rendered inline — and the action log
records inbox answers and messages sent."""

from fastapi.testclient import TestClient

from cairnival import agentloop
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


# --- the tool registry carries UI metadata --------------------------------

def test_write_tool_with_ui_persists_surface(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    reg = ToolRegistry(mem.tools_dir, mem.workspace_dir, cfg)
    reg.write_tool(
        "greet", "greet someone", "bash", 'echo "hello, $1"',
        ui=True, ui_title="Greeter", ui_inputs=["who"], ui_output="text",
    )
    # rediscover from disk — the UI survives a manifest round-trip
    reg2 = ToolRegistry(mem.tools_dir, mem.workspace_dir, cfg)
    reg2.discover()
    tool = reg2.tools["greet"]
    assert tool.ui and tool.ui_title == "Greeter"
    assert tool.ui_inputs == ["who"] and tool.ui_output == "text"
    assert [t.name for t in reg2.ui_tools()] == ["greet"]


def test_set_ui_toggles_surface(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    reg = ToolRegistry(mem.tools_dir, mem.workspace_dir, cfg)
    reg.write_tool("calc", "adds", "bash", "echo $(( $1 + $2 ))")
    assert reg.ui_tools() == []
    assert reg.set_ui("calc", enabled=True, title="Adder", inputs=["a", "b"], output="text")
    assert [t.name for t in reg.ui_tools()] == ["calc"]
    assert reg.set_ui("calc", enabled=False)
    assert reg.ui_tools() == []
    assert not reg.set_ui("nope", enabled=True)


# --- the surface action from the tool loop --------------------------------

def test_surface_action_adds_ui(tmp_path):
    from cairnival.pursuits import PursuitBook
    from cairnival.treasury import Ledger
    from cairnival.federation import Identity
    from cairnival.llm import build_backend
    from cairnival.wake import WakeContext

    cfg = agent_cfg(tmp_path, "moth")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    reg = ToolRegistry(mem.tools_dir, mem.workspace_dir, cfg)
    reg.write_tool("weather", "forecast", "bash", "echo sunny")
    ctx = WakeContext(
        cfg, mem, mem.load_state(),
        Identity.load_or_create(mem.keys_dir, "moth"),
        Ledger(mem.treasury_dir), build_backend(cfg), reg, PursuitBook(cfg.home),
    )
    action = agentloop.Action(kind="surface", arg="weather", body="title: Weather\ninputs: city\noutput: text")
    out = agentloop._observe(action, reg, cfg, agentloop.LoopResult(answer=""), ctx)
    assert "surface for weather" in out
    assert reg.tools["weather"].ui and reg.tools["weather"].ui_inputs == ["city"]


# --- the surface renders and runs through the app -------------------------

def test_surface_page_runs_tool_with_inputs(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    app = create_app(cfg)
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    reg = ToolRegistry(mem.tools_dir, mem.workspace_dir, cfg)
    reg.write_tool(
        "greet", "greet someone", "bash", 'echo "hello, $1"',
        ui=True, ui_title="Greeter", ui_inputs=["who"], ui_output="text",
    )
    with TestClient(app) as client:
        # it's listed on the surfaces index
        idx = client.get("/surfaces")
        assert idx.status_code == 200 and "Greeter" in idx.text
        # the surface page shows a form field for the declared input
        page = client.get("/surface/greet")
        assert page.status_code == 200 and 'name="who"' in page.text
        # running it passes the input to the tool as a positional arg
        ran = client.post("/surface/greet", data={"who": "moth"})
        assert ran.status_code == 200 and "hello, moth" in ran.text
        # a tool without a surface has no page
        reg.write_tool("secret", "x", "bash", "echo x")
        assert client.get("/surface/secret").status_code == 404


def test_journal_page_shows_actions(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    app = create_app(cfg)
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    mem.journal_append(
        "\n## wake 1 — now\n- answered inbox message from rustle: are you there?\n"
        "- sent message to wren: on it\n- wrote specimen SP-0001: Hello"
    )
    with TestClient(app) as client:
        r = client.get("/journal")
        assert r.status_code == 200
        assert "answered inbox message from rustle" in r.text
        assert "sent message to wren" in r.text


# --- the log names inbox answers and message sends ------------------------

def test_worked_line_names_inbox_answer():
    from cairnival.wake import _worked_line

    fed = Instruction(title="are you there?", body="", source="federation",
                      sender="rustle", reply_to="rustle")
    assert _worked_line(fed) == "answered inbox message from rustle: are you there?"
    ui = Instruction(title="do a thing", body="", source="ui")
    assert _worked_line(ui) == "worked: do a thing [ui]"


def test_send_action_logged_with_preview():
    action = agentloop.Action(kind="send", arg="wren", body="meet me by the ferris wheel")
    assert agentloop._describe(action) == 'sent message to wren: "meet me by the ferris wheel"'
