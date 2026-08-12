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
from cairnival.pursuits import PursuitBook
from cairnival.tools import ToolRegistry
from cairnival.treasury import Ledger


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def describe(self):
        return "scripted"

    def chat(self, system, prompt, think=None):
        self.calls += 1
        self.last_think = think
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
    ledger = Ledger(memory.treasury_dir)
    registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, cfg)
    registry.discover()
    ctx = SimpleNamespace(
        cfg=cfg,
        memory=memory,
        llm=llm,
        registry=registry,
        identity=identity,
        ledger=ledger,
        pursuits=PursuitBook(tmp_path),
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
    name, interp, desc, script, ui = _parse_tool_spec(
        "fallback",
        "name: greet\ninterpreter: bash\ndescription: say hi\n---\necho hi",
    )
    assert (name, interp, desc) == ("greet", "bash", "say hi")
    assert script == "echo hi"
    assert ui == {}  # no UI declared


def test_parse_tool_spec_ui_front_matter():
    name, interp, desc, script, ui = _parse_tool_spec(
        "fallback",
        "name: weather\ninterpreter: python\nui: true\ntitle: Weather\n"
        "inputs: city, days\noutput: html\n---\nprint('hi')",
    )
    assert name == "weather"
    assert ui["enabled"] is True
    assert ui["title"] == "Weather"
    assert ui["inputs"] == ["city", "days"]
    assert ui["output"] == "html"


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


def test_propose_action_creates_pending_spend(tmp_path):
    llm = ScriptedLLM(
        [
            "```propose\nto: registrar\namount: 0.2\nreason: renew the domain\n```",
            "```final\nProposed the renewal; waiting on a co-signer.\n```",
        ]
    )
    ctx, registry = make_ctx(tmp_path, llm)
    ctx.ledger.deposit(1.0)  # has funds, but a spend still needs a co-sign
    ins = Instruction(title="renew", body="renew the domain", source="ui")
    result = agentloop.solve(ctx, ins, registry)
    assert result.proposals, "a proposal id should be recorded"
    pending_props = ctx.ledger.proposals("pending")
    assert len(pending_props) == 1
    assert pending_props[0]["to"] == "registrar"
    assert pending_props[0]["amount"] == 0.2
    # nothing moved: the agent cannot spend alone
    assert ctx.ledger.balance() == 1.0


def test_system_prompt_briefs_all_capabilities(tmp_path):
    from cairnival.agentloop import _system_prompt

    ctx, registry = make_ctx(tmp_path, ScriptedLLM([]))
    ctx.ledger.deposit(0.5)
    prompt = _system_prompt(ctx.memory.soul(), registry, ctx.cfg, ctx)
    for token in ("```run", "```use", "```write-tool", "```send", "```propose", "```final"):
        assert token in prompt, f"{token} missing from the briefing"
    assert "Treasury:" in prompt  # situation block present
    assert "rustle" in prompt  # identity named


def test_records_actions_not_reasoning(tmp_path):
    # the model rambles before acting; only the action should be recorded
    llm = ScriptedLLM(
        [
            "Let me think about this carefully. I believe I should list files.\n"
            "```run\necho hi\n```",
            "```final\ndone\n```",
        ]
    )
    ctx, registry = make_ctx(tmp_path, llm)
    result = agentloop.solve(ctx, Instruction(title="t", body="b", source="ui"), registry)
    assert result.actions == ["ran shell: echo hi"]
    assert not any("think" in a.lower() or "believe" in a.lower() for a in result.actions)


def test_remember_off_keeps_nothing(tmp_path):
    llm = ScriptedLLM(["```remember\nremember this fact\n```", "```final\nok\n```"])
    ctx, registry = make_ctx(tmp_path, llm)  # remember disabled by default
    result = agentloop.solve(ctx, Instruction(title="t", body="b", source="ui"), registry)
    assert result.remembered == 0
    assert not ctx.memory.remember_path.exists()
    assert any("off" in s.observation for s in result.steps)


def test_remember_on_persists_and_briefs_next_wake(tmp_path):
    from cairnival.agentloop import _system_prompt

    llm = ScriptedLLM(
        ["```remember\ntide tables live in the workspace\n```", "```final\nok\n```"]
    )
    ctx, registry = make_ctx(tmp_path, llm)
    ctx.cfg.remember_enabled = True
    result = agentloop.solve(ctx, Instruction(title="t", body="b", source="ui"), registry)
    assert result.remembered == 1
    assert "tide tables" in ctx.memory.remember_tail()

    # the next wake's briefing surfaces the memory and offers the action
    prompt = _system_prompt(ctx.memory.soul(), registry, ctx.cfg, ctx)
    assert "What you remember" in prompt
    assert "tide tables" in prompt
    assert "```remember" in prompt


def test_briefing_states_amnesia_when_remember_off(tmp_path):
    from cairnival.agentloop import _system_prompt

    ctx, registry = make_ctx(tmp_path, ScriptedLLM([]))
    prompt = _system_prompt(ctx.memory.soul(), registry, ctx.cfg, ctx)
    assert "no memory of past wakes" in prompt
    # the remember action is not offered in the action list when the switch is off
    assert "keep a durable note to yourself" not in prompt


def test_pursue_action_starts_and_advances_a_goal(tmp_path):
    llm = ScriptedLLM(
        [
            "```pursue\ntitle: map the tides of the midway\nnote: first, gather data\n```",
            "```final\nStarted the tide project.\n```",
        ]
    )
    ctx, registry = make_ctx(tmp_path, llm)
    result = agentloop.solve(ctx, Instruction(title="self", body="pursue goals", source="self"), registry)
    assert result.pursuits_started == ["map the tides of the midway"]
    active = ctx.pursuits.active()
    assert len(active) == 1
    pid = active[0].id

    # a later wake advances it by id
    llm2 = ScriptedLLM(
        [f"```pursue\nid: {pid}\nnote: charted a week\nstatus: done\n```", "```final\nok\n```"]
    )
    ctx2, reg2 = make_ctx(tmp_path, llm2)  # same tmp_path → same pursuit book
    result2 = agentloop.solve(ctx2, Instruction(title="self", body="pursue", source="self"), reg2)
    assert pid in result2.pursuits_advanced
    assert ctx2.pursuits.get(pid).status == "done"
    assert ctx2.pursuits.active() == []


def test_pursuits_appear_in_briefing_when_enabled(tmp_path):
    from cairnival.agentloop import _system_prompt

    ctx, registry = make_ctx(tmp_path, ScriptedLLM([]))
    ctx.pursuits.start("map the tides of the midway", "gathering data")
    prompt = _system_prompt(ctx.memory.soul(), registry, ctx.cfg, ctx)
    assert "Your pursuits" in prompt
    assert "map the tides" in prompt
    assert "```pursue" in prompt

    ctx.cfg.self_direction_enabled = False
    off = _system_prompt(ctx.memory.soul(), registry, ctx.cfg, ctx)
    assert "Your pursuits (goals you set" not in off


def test_loop_respects_step_limit(tmp_path):
    # never emits final; every reply is another run
    llm = ScriptedLLM(["```run\necho step\n```"] * 10)
    ctx, registry = make_ctx(tmp_path, llm)
    ctx.cfg.tools_max_steps = 3
    ins = Instruction(title="loop", body="keep going", source="ui")
    result = agentloop.solve(ctx, ins, registry)
    assert len(result.steps) == 3  # capped
    assert result.answer  # wrap-up still produced
