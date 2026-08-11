"""Exercise the tool-use loop with a scripted backend, since the echo backend
does no reasoning. The fake plays a fixed sequence of replies, letting us
drive run / write-tool / use / final deterministically."""

from types import SimpleNamespace

from cairnival import agentloop
from cairnival.agentloop import parse_action, _parse_tool_spec
from cairnival.config import AgentConfig
from cairnival.federation import Identity
from cairnival.instructions import Instruction
from cairnival.memory import Memory
from cairnival.tools import ToolRegistry


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def describe(self):
        return "scripted"

    def chat(self, system, prompt):
        self.calls += 1
        if self.replies:
            return self.replies.pop(0)
        return "```final\nout of script\n```"


def make_ctx(tmp_path, llm):
    cfg = AgentConfig()
    cfg.home = tmp_path
    cfg.tools_max_steps = 5
    memory = Memory(tmp_path, "rustle")
    memory.ensure()
    identity = Identity.load_or_create(memory.keys_dir, "rustle")
    registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, cfg)
    registry.discover()
    ctx = SimpleNamespace(
        cfg=cfg, memory=memory, llm=llm, registry=registry, identity=identity
    )
    return ctx, registry


def test_parse_action_variants():
    assert parse_action("```run\nls -la\n```").kind == "run"
    assert parse_action("```shell\npwd\n```").kind == "run"  # alias
    a = parse_action("```use:greet\nworld\n```")
    assert a.kind == "use" and a.arg == "greet" and a.body == "world"
    assert parse_action("just talking").kind == "final"
    assert parse_action("```python\nprint(1)\n```").kind == "final"  # not an action verb


def test_parse_tool_spec_front_matter():
    name, interp, desc, script = _parse_tool_spec(
        "fallback",
        "name: greet\ninterpreter: bash\ndescription: say hi\n---\necho hi",
    )
    assert (name, interp, desc) == ("greet", "bash", "say hi")
    assert script == "echo hi"


def test_loop_runs_shell_then_answers(tmp_path):
    llm = ScriptedLLM(
        [
            "```run\necho scan-result\n```",
            "```final\nI scanned and saw scan-result.\n```",
        ]
    )
    ctx, registry = make_ctx(tmp_path, llm)
    ins = Instruction(title="scan", body="scan the area", source="ui")
    result = agentloop.solve(ctx, ins, registry)
    assert "scan-result" in result.answer or "scanned" in result.answer
    assert len(result.steps) == 1
    assert "scan-result" in result.steps[0].observation


def test_loop_writes_tool_then_uses_it_and_it_persists(tmp_path):
    llm = ScriptedLLM(
        [
            "```write-tool\nname: adder\ninterpreter: python\ndescription: add two ints\n"
            "---\nimport sys\na,b=sys.argv[1:3]\nprint(int(a)+int(b))\n```",
            "```use:adder\n2 3\n```",
            "```final\nadder says 5\n```",
        ]
    )
    ctx, registry = make_ctx(tmp_path, llm)
    ins = Instruction(title="math", body="make an adder and use it", source="ui")
    result = agentloop.solve(ctx, ins, registry)
    assert "adder" in result.tools_written
    assert "adder" in result.tools_used
    assert any("5" in s.observation for s in result.steps)

    # the tool is on disk — a brand-new registry (next wake) rediscovers it
    next_wake = ToolRegistry(ctx.memory.tools_dir, ctx.memory.workspace_dir, ctx.cfg)
    assert "adder" in next_wake.discover()
    assert next_wake.run_tool("adder", ["10", "20"]).output.strip() == "30"


def test_send_action_parses():
    a = parse_action("```send:moth\nhello there\n```")
    assert a.kind == "send" and a.arg == "moth" and a.body == "hello there"


def test_loop_sends_message_to_peer(tmp_path, monkeypatch):
    # capture deliver_note instead of hitting the network
    calls = {}

    def fake_deliver(cfg, identity, memory, to_handle, text, **kw):
        calls["to"] = to_handle
        calls["text"] = text
        return True, "direct"

    monkeypatch.setattr("cairnival.agentloop.messaging.deliver_note", fake_deliver)
    llm = ScriptedLLM(
        [
            "```send:moth\nwant to collaborate on tide charts?\n```",
            "```final\nSent moth a note.\n```",
        ]
    )
    ctx, registry = make_ctx(tmp_path, llm)
    ins = Instruction(title="reach out", body="ask moth to collaborate", source="ui")
    result = agentloop.solve(ctx, ins, registry)
    assert "moth" in result.messages_sent
    assert calls["to"] == "moth"
    assert "tide charts" in calls["text"]


def test_loop_respects_step_limit(tmp_path):
    # never emits final; every reply is another run
    llm = ScriptedLLM(["```run\necho step\n```"] * 10)
    ctx, registry = make_ctx(tmp_path, llm)
    ctx.cfg.tools_max_steps = 3
    ins = Instruction(title="loop", body="keep going", source="ui")
    result = agentloop.solve(ctx, ins, registry)
    assert len(result.steps) == 3  # capped
    assert result.answer  # wrap-up still produced
