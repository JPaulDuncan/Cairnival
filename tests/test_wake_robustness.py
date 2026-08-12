"""A wake must always finish and write a specimen — a failing task, a dead
peer, or a thrown action can never leave a wake with nothing to show. Plus the
inter-agent ping: a progress/completion notice that lands in a collaborator's
inbox."""

from fastapi.testclient import TestClient

from cairnival import agentloop, messaging, wake as wake_mod
from cairnival.agent_app import create_app
from cairnival.config import AgentConfig
from cairnival.federation import Identity, seal
from cairnival.instructions import Instruction, drop, pending
from cairnival.memory import Memory
from cairnival.wake import run_wake


def agent_cfg(tmp_path, name, tools=True):
    cfg = AgentConfig()
    cfg.name = name
    cfg.home = tmp_path / name
    cfg.llm_backend = "echo"
    cfg.tools_enabled = tools
    cfg.self_direction_enabled = False
    return cfg


# --- a wake always produces a specimen ------------------------------------

def test_wake_writes_specimen_even_when_a_task_throws(tmp_path, monkeypatch):
    cfg = agent_cfg(tmp_path, "moth")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    drop(mem.inbox_dir, Instruction(title="do a thing", body="please", source="ui"))

    # make the actual work explode, the worst case mid-wake
    def boom(ctx, ins):
        raise RuntimeError("model melted")

    monkeypatch.setattr(wake_mod, "_work_instruction", boom)

    report = run_wake(cfg)
    assert report.specimen is not None, "a wake must always leave a specimen"
    assert report.specimen.id.startswith("SP-")
    # and it is on disk
    assert (mem.specimens_dir / f"{report.specimen.id}.md").exists()


def test_wake_survives_a_failing_network_stage(tmp_path, monkeypatch):
    cfg = agent_cfg(tmp_path, "moth")
    cfg.hub_url = "http://127.0.0.1:9"  # nothing is listening

    # even if peer discovery raises something non-HTTP, the wake completes
    def explode(*a, **k):
        raise RuntimeError("discovery blew up")

    monkeypatch.setattr(wake_mod, "_discover_peers", explode)
    report = run_wake(cfg)
    assert report.specimen is not None


def test_tool_loop_action_failure_is_an_observation(tmp_path, monkeypatch):
    from cairnival.llm import build_backend
    from cairnival.pursuits import PursuitBook
    from cairnival.tools import ToolRegistry
    from cairnival.treasury import Ledger

    cfg = agent_cfg(tmp_path, "moth")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    ledger = Ledger(mem.treasury_dir)
    identity = Identity.load_or_create(mem.keys_dir, "moth")
    reg = ToolRegistry(mem.tools_dir, mem.workspace_dir, cfg)
    ctx = wake_mod.WakeContext(
        cfg, mem, mem.load_state(), identity, ledger, build_backend(cfg), reg, PursuitBook(cfg.home)
    )

    # a model that emits one action (which will throw) then answers
    replies = iter(["```run\necho hi\n```", "```final\ndone anyway\n```"])

    class FlakyLLM:
        name = "flaky"

        def chat(self, system, prompt, think=None):
            return next(replies)

        def describe(self):
            return "flaky"

    ctx.llm = FlakyLLM()

    def boom(*a, **k):
        raise RuntimeError("shell died")

    monkeypatch.setattr(agentloop, "_observe", boom)
    out = agentloop.solve(ctx, Instruction(title="t", body="b", source="ui"), reg)
    # the loop kept going and produced a final answer despite the action failing
    assert out.answer == "done anyway"
    assert any("that action failed" in s.observation for s in out.steps)


# --- pings ----------------------------------------------------------------

def test_ping_endpoint_lands_terminal_notification(tmp_path):
    owner_cfg = agent_cfg(tmp_path, "moth")
    app = create_app(owner_cfg)
    omem = Memory(owner_cfg.home, "moth")
    omem.ensure()
    rmem = Memory(tmp_path / "rustle", "rustle")
    rmem.ensure()
    rustle = Identity.load_or_create(rmem.keys_dir, "rustle")
    peers = omem.load_peers()
    peers["rustle"] = {"public_key": rustle.public_key, "public_url": "http://rustle"}
    omem.save_peers(peers)

    with TestClient(app) as client:
        env = seal(rustle, "ping", {"text": "halfway through the scraper", "phase": "progress"})
        resp = client.post("/api/ping", json=env.to_dict())
        assert resp.status_code == 200 and resp.json()["phase"] == "progress"
        # empty ping is refused
        empty = seal(rustle, "ping", {"text": "  ", "phase": "done"})
        assert client.post("/api/ping", json=empty.to_dict()).status_code == 400

    inbox = pending(Memory(owner_cfg.home, "moth").inbox_dir)
    ping = [i for i in inbox if i.sender == "rustle"]
    assert ping, "the ping should be in moth's inbox"
    assert ping[0].reply_to == "", "a ping is a notification, not a question"
    assert "halfway through the scraper" in ping[0].body


def test_send_ping_direct_delivery(tmp_path):
    # stand up moth as a real node; rustle pings it directly
    moth_cfg = agent_cfg(tmp_path, "moth")
    app = create_app(moth_cfg)
    with TestClient(app) as client:
        moth_mem = Memory(moth_cfg.home, "moth")
        moth_mem.ensure()
        moth_id = Identity.load_or_create(moth_mem.keys_dir, "moth")

        rustle_cfg = agent_cfg(tmp_path, "rustle")
        rmem = Memory(rustle_cfg.home, "rustle")
        rmem.ensure()
        rustle = Identity.load_or_create(rmem.keys_dir, "rustle")
        # rustle knows moth, and delivery goes through the TestClient transport
        rmem.save_peers({"moth": {"public_key": moth_id.public_key, "public_url": "http://moth"}})

        import cairnival.messaging as m
        real_post = client.post

        def fake_post(url, json=None, timeout=None):
            path = url.split("http://moth", 1)[-1]
            return real_post(path, json=json)

        import httpx
        orig = httpx.post
        httpx.post = fake_post
        try:
            ok, how = messaging.send_ping(rustle_cfg, rustle, rmem, "moth", "kicking off", phase="start")
        finally:
            httpx.post = orig
        assert ok and how == "direct"

    inbox = pending(Memory(moth_cfg.home, "moth").inbox_dir)
    assert any("kicking off" in i.body for i in inbox)
