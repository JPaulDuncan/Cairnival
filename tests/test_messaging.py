"""Discovery, agent-to-agent messaging, and replies.

Two paths are exercised:
  * the *receiving* semantics — a signed note posted to an agent lands in its
    inbox, identified by sender, and is reply-eligible (or terminal) — via the
    agent app directly, which is synchronous;
  * the *end-to-end* flow — discovery, a held message, a wake that answers it,
    and a reply that comes back without looping — through a live Midway.
"""

import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from cairnival.agent_app import create_app
from cairnival.config import AgentConfig, HubConfig
from cairnival.federation import Identity, seal
from cairnival.hub_app import create_app as create_hub
from cairnival.instructions import pending
from cairnival.memory import Memory
from cairnival import messaging
from cairnival.wake import run_wake

HUB_PORT = 8641


def agent_cfg(tmp_path, name, hub_url="", tools=False):
    cfg = AgentConfig()
    cfg.name = name
    cfg.home = tmp_path / name
    cfg.llm_backend = "echo"
    cfg.hub_url = hub_url
    cfg.tools_enabled = tools
    return cfg


# --- receiving semantics (synchronous, via the app) -----------------------

def test_receiver_identifies_sender_and_message_is_reply_eligible(tmp_path):
    moth_cfg = agent_cfg(tmp_path, "moth")
    app = create_app(moth_cfg)

    rustle_mem = Memory(tmp_path / "rustle", "rustle")
    rustle_mem.ensure()
    rustle = Identity.load_or_create(rustle_mem.keys_dir, "rustle")

    with TestClient(app) as client:
        note = seal(rustle, "note", {"text": "are you awake?"})
        resp = client.post("/api/federation/inbox", json=note.to_dict())
        assert resp.status_code == 200
        assert resp.json()["received_by"] == "moth"

    inbox = pending(Memory(moth_cfg.home, "moth").inbox_dir)
    msg = [i for i in inbox if i.sender == "rustle"]
    assert msg, "message should be in moth's inbox"
    assert msg[0].source == "federation"
    assert msg[0].reply_to == "rustle", "a fresh message earns a reply"
    assert "are you awake?" in msg[0].body


def test_reply_flag_makes_message_terminal(tmp_path):
    moth_cfg = agent_cfg(tmp_path, "moth")
    app = create_app(moth_cfg)
    rustle_mem = Memory(tmp_path / "rustle", "rustle")
    rustle_mem.ensure()
    rustle = Identity.load_or_create(rustle_mem.keys_dir, "rustle")

    with TestClient(app) as client:
        reply = seal(rustle, "note", {"text": "yes, hello", "reply": True})
        client.post("/api/federation/inbox", json=reply.to_dict())

    inbox = pending(Memory(moth_cfg.home, "moth").inbox_dir)
    msg = [i for i in inbox if i.sender == "rustle"][0]
    assert msg.reply_to == "", "a reply must not trigger another reply"


def test_forged_sender_rejected(tmp_path):
    """Sender identity is a pinned key, not a claimed name."""
    moth_cfg = agent_cfg(tmp_path, "moth")
    app = create_app(moth_cfg)

    real_mem = Memory(tmp_path / "wren", "wren")
    real_mem.ensure()
    real = Identity.load_or_create(real_mem.keys_dir, "wren")
    with TestClient(app) as client:
        # first contact pins wren's key
        client.post("/api/federation/inbox", json=seal(real, "note", {"text": "hi"}).to_dict())
        # an impostor using the same handle but a different key is refused
        imp_mem = Memory(tmp_path / "imp", "wren")
        imp_mem.ensure()
        impostor = Identity.load_or_create(imp_mem.keys_dir, "wren")
        forged = seal(impostor, "note", {"text": "it's me, wren"})
        assert client.post("/api/federation/inbox", json=forged.to_dict()).status_code == 403


# --- end-to-end through a live Midway -------------------------------------

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
            time.sleep(0.05)
    yield f"http://127.0.0.1:{HUB_PORT}"
    server.should_exit = True
    thread.join(timeout=5)


def test_discovery_learns_peers_from_hub(tmp_path, hub):
    a = agent_cfg(tmp_path, "rustle", hub)
    b = agent_cfg(tmp_path, "moth", hub)
    run_wake(a)   # rustle registers
    run_wake(b)   # moth registers and discovers rustle
    run_wake(a)   # rustle discovers moth

    peers_a = Memory(a.home, "rustle").load_peers()
    peers_b = Memory(b.home, "moth").load_peers()
    assert "moth" in peers_a and "rustle" in peers_b
    moth_key = Identity.load_or_create(Memory(b.home, "moth").keys_dir, "moth").public_key
    assert peers_a["moth"]["public_key"] == moth_key  # pinned, correct key


def test_message_answered_and_reply_returns_without_looping(tmp_path, hub):
    a = agent_cfg(tmp_path, "rustle", hub)
    b = agent_cfg(tmp_path, "moth", hub)
    run_wake(a); run_wake(b); run_wake(a)  # mutual discovery

    mem_a = Memory(a.home, "rustle")
    ident_a = Identity.load_or_create(mem_a.keys_dir, "rustle")
    ok, how = messaging.deliver_note(a, ident_a, mem_a, "moth", "are you awake?")
    assert ok and how in ("held", "relayed", "direct")

    # moth wakes: collects the message, works it, replies to rustle
    rep_b = run_wake(b)
    assert any("message from rustle" in line for line in rep_b.log)
    assert any("replied to rustle over federation" in line for line in rep_b.log)

    # rustle wakes: the reply arrives, marked terminal, and is NOT answered
    rep_a = run_wake(a)
    assert any("reply from moth" in line for line in rep_a.log)
    assert not any("replied to moth" in line for line in rep_a.log)

    # and the conversation is over: moth has nothing new from rustle
    rep_b2 = run_wake(b)
    assert not any("from rustle" in line for line in rep_b2.log)
