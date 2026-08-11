"""End-to-end-ish: one wake against the echo backend, publishing to a live
in-process Midway; then federation mail through the hub mailroom."""

import threading

import httpx
import pytest
import uvicorn

from cairnival.config import AgentConfig, HubConfig
from cairnival.federation import Identity, seal
from cairnival.hub_app import create_app as create_hub
from cairnival.instructions import Instruction, drop
from cairnival.memory import Memory
from cairnival.treasury import Ledger
from cairnival.wake import run_wake

HUB_PORT = 8639


@pytest.fixture()
def hub(tmp_path_factory):
    cfg = HubConfig(home=tmp_path_factory.mktemp("hub"), port=HUB_PORT)
    server = uvicorn.Server(
        uvicorn.Config(create_hub(cfg), host="127.0.0.1", port=HUB_PORT, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{HUB_PORT}/api/agents", timeout=1)
            break
        except httpx.HTTPError:
            import time

            time.sleep(0.05)
    yield f"http://127.0.0.1:{HUB_PORT}"
    server.should_exit = True
    thread.join(timeout=5)


def agent_cfg(tmp_path, hub_url: str) -> AgentConfig:
    cfg = AgentConfig()
    cfg.name = "rustle"
    cfg.home = tmp_path / "agent"
    cfg.llm_backend = "echo"
    cfg.hub_url = hub_url
    cfg.wake_interval_minutes = 1
    return cfg


def test_wake_works_instructions_and_publishes(tmp_path, hub):
    cfg = agent_cfg(tmp_path, hub)
    memory = Memory(cfg.home, cfg.name)
    memory.ensure()
    drop(memory.inbox_dir, Instruction(title="Say hi", body="Say hi to the midway."))

    # a paid memo should surface as a paid instruction on the same wake
    Ledger(memory.treasury_dir).deposit(0.05, "asker", "What color is the tent?")

    report = run_wake(cfg)
    assert report.wake_number == 1
    assert report.handled == 2
    assert report.specimen is not None

    # published to the hub, at a permanent URL
    resp = httpx.get(f"{hub}/api/agents")
    assert "rustle" in resp.json()
    page = httpx.get(f"{hub}/specimens/rustle/{report.specimen.id}")
    assert page.status_code == 200

    # inbox drained, state advanced, journal written
    assert not list(memory.inbox_dir.glob("*.md"))
    assert memory.load_state()["wakes"] == 1
    assert "wake 1" in memory.journal_tail()

    # second wake with nothing pending still writes a specimen
    report2 = run_wake(cfg)
    assert report2.handled == 0
    assert report2.specimen.id != report.specimen.id


def test_hub_mailroom_holds_and_delivers(tmp_path, hub):
    cfg = agent_cfg(tmp_path, hub)
    run_wake(cfg)  # registers rustle with the hub

    # a second identity mails rustle through the hub; rustle has no public
    # URL, so the letter is held
    memory_b = Memory(tmp_path / "b", "moth")
    memory_b.ensure()
    moth = Identity.load_or_create(memory_b.keys_dir, "moth")
    httpx.post(
        f"{hub}/api/register",
        json=seal(moth, "hello", {"tagline": "second"}).to_dict(),
    ).raise_for_status()
    note = seal(moth, "note", {"text": "meet me by the ferris wheel"})
    resp = httpx.post(f"{hub}/api/mail/rustle", json=note.to_dict())
    assert resp.json()["delivery"] == "held"

    # on rustle's next wake the held mail becomes an instruction and is worked
    report = run_wake(cfg)
    assert any("moth" in line for line in report.log)
    assert report.handled >= 1


def test_hub_rejects_impostor_key(tmp_path, hub):
    cfg = agent_cfg(tmp_path, hub)
    run_wake(cfg)  # pins rustle's real key

    impostor_mem = Memory(tmp_path / "imp", "rustle")
    impostor_mem.ensure()
    impostor = Identity.load_or_create(impostor_mem.keys_dir, "rustle")
    env = seal(impostor, "hello", {"tagline": "the real rustle, honest"})
    resp = httpx.post(f"{hub}/api/register", json=env.to_dict())
    # rejected at the pinned-key check: once a handle has said hello, only
    # the key from that first hello is believed
    assert resp.status_code == 403


def test_self_directed_idle_wake(tmp_path):
    """An idle wake is creative time: nobody instructed the agent, but it works
    on a goal of its own."""
    cfg = AgentConfig()
    cfg.name = "rustle"
    cfg.home = tmp_path / "solo"
    cfg.llm_backend = "echo"  # self-direction and tools on by default
    report = run_wake(cfg)
    assert report.handled == 0          # no one gave it work
    assert report.self_directed is True  # it worked anyway
    assert report.specimen is not None
    assert "self-directed" in report.specimen.tags


def test_self_direction_switch_off(tmp_path):
    cfg = AgentConfig()
    cfg.name = "rustle"
    cfg.home = tmp_path / "quiet"
    cfg.llm_backend = "echo"
    cfg.self_direction_enabled = False
    report = run_wake(cfg)
    assert report.self_directed is False


def test_specimen_generated_without_thinking(tmp_path, monkeypatch):
    """The published specimen must be produced with reasoning off, so a
    thinking model's monologue can never land in the record."""
    from cairnival import wake as wake_mod

    seen_think = []

    class RecordingLLM:
        def describe(self):
            return "recording"

        def chat(self, system, prompt, think=None):
            # remember the think flag for the specimen-writing call
            if "blog entry" in prompt or "blog entry about this wake" in prompt:
                seen_think.append(think)
            return "# Wake\n\nA clean entry."

    monkeypatch.setattr(wake_mod, "build_backend", lambda cfg: RecordingLLM())

    cfg = AgentConfig()
    cfg.name = "rustle"
    cfg.home = tmp_path / "nt"
    cfg.self_direction_enabled = False  # keep the wake to just the specimen
    report = run_wake(cfg)
    assert report.specimen is not None
    assert seen_think and all(t is False for t in seen_think)
