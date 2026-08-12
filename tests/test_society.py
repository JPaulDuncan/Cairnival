"""The agent-society layer: personality, follow/unfollow, tool sharing +
capability help routing, and likes/comments.

These are the pieces that let agents self-organize without a central
conductor — each is exercised at the layer it lives in: memory helpers
directly, the capability matcher on a real registry, and the reaction/help
endpoints through the agent app (synchronous via TestClient).
"""

from fastapi.testclient import TestClient

from cairnival.agent_app import create_app
from cairnival.config import AgentConfig
from cairnival.federation import Identity, seal
from cairnival.memory import DEFAULT_PERSONALITY, Memory
from cairnival.tools import ToolRegistry
from cairnival import messaging


def agent_cfg(tmp_path, name, tools=True):
    cfg = AgentConfig()
    cfg.name = name
    cfg.home = tmp_path / name
    cfg.llm_backend = "echo"
    cfg.tools_enabled = tools
    return cfg


def _peer(memory, other_cfg, following=False):
    """Pin `other` into memory as a directly-reachable peer."""
    other_mem = Memory(other_cfg.home, other_cfg.name)
    other_mem.ensure()
    other_id = Identity.load_or_create(other_mem.keys_dir, other_cfg.name)
    peers = memory.load_peers()
    peers[other_cfg.name] = {
        "public_key": other_id.public_key,
        "public_url": other_cfg.public_url,
        "tagline": other_cfg.tagline,
        "following": following,
    }
    memory.save_peers(peers)
    return other_id, other_mem


# --- personality ----------------------------------------------------------

def test_personality_defaults_then_becomes_custom(tmp_path):
    mem = Memory(tmp_path / "moth", "moth")
    mem.ensure()
    assert mem.personality_is_default()
    assert mem.personality().strip() == DEFAULT_PERSONALITY.format(name="moth").strip()

    mem.set_personality("# PERSONALITY\n\nI speak in short, dry sentences.")
    assert not mem.personality_is_default()
    assert "short, dry sentences" in mem.personality()


def test_reset_soul_clears_personality(tmp_path):
    mem = Memory(tmp_path / "moth", "moth")
    mem.ensure()
    mem.set_personality("# PERSONALITY\n\nCustom voice.")
    mem.reset(reset_soul=True)
    mem.ensure()  # a reset agent re-scaffolds on next open
    assert mem.personality_is_default(), "a soul reset also wipes the learned voice"


# --- follow / unfollow -----------------------------------------------------

def test_follow_unfollow_tracks_selected_agents(tmp_path):
    mem = Memory(tmp_path / "moth", "moth")
    mem.ensure()
    peers = mem.load_peers()
    peers["rustle"] = {"public_url": "http://x", "public_key": "k1"}
    peers["wren"] = {"public_url": "http://y", "public_key": "k2"}
    mem.save_peers(peers)

    assert mem.following() == []
    assert mem.set_following("rustle", True)
    assert mem.is_following("rustle")
    assert not mem.is_following("wren")
    assert mem.following() == ["rustle"]

    assert mem.set_following("rustle", False)
    assert mem.following() == []
    assert not mem.set_following("nobody", True), "can't follow an unknown agent"


def test_feed_scopes_to_followed_but_never_empty(tmp_path, monkeypatch):
    cfg = agent_cfg(tmp_path, "moth")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    peers = mem.load_peers()
    peers["rustle"] = {"public_url": "http://rustle", "public_key": "k1"}
    peers["wren"] = {"public_url": "http://wren", "public_key": "k2"}
    mem.save_peers(peers)

    def fake_fetch(url, limit):
        who = "rustle" if "rustle" in url else "wren"
        return [{"agent": who, "id": "SP-0001", "collected": "2026-01-01"}]

    monkeypatch.setattr(messaging, "fetch_posts", fake_fetch)
    ident = Identity.load_or_create(mem.keys_dir, "moth")

    # nobody followed -> falls back to everyone known (feed is never empty)
    messaging.gather_feed(cfg, ident, mem, [])
    agents = {p["agent"] for p in mem.load_feed_cache()}
    assert agents == {"rustle", "wren"}

    # follow just rustle -> feed narrows to rustle
    mem.set_following("rustle", True)
    messaging.gather_feed(cfg, ident, mem, [])
    agents = {p["agent"] for p in mem.load_feed_cache()}
    assert agents == {"rustle"}


# --- tool sharing + capability matching ------------------------------------

def test_shared_tools_honor_policy(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    reg = ToolRegistry(mem.tools_dir, mem.workspace_dir, cfg)
    reg.write_tool("weather", "fetch the weather forecast", "bash", "echo sunny")
    reg.write_tool("secret", "private notes", "bash", "echo shh")
    reg.set_shared("secret", False)

    assert {t.name for t in reg.shared_tools("all")} == {"weather", "secret"}
    assert {t.name for t in reg.shared_tools("selected")} == {"weather"}
    assert reg.shared_tools("none") == []


def test_capability_match_finds_relevant_shared_tool(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    cfg.tool_sharing = "all"
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    reg = ToolRegistry(mem.tools_dir, mem.workspace_dir, cfg)
    reg.write_tool("weather", "fetch the weather forecast", "bash", "echo sunny")

    assert messaging.capability_match(cfg, mem, "can anyone fetch a weather forecast?") == ["weather"]
    assert messaging.capability_match(cfg, mem, "help me parse a PDF invoice") == []

    # nothing is shared when the policy is none, so no match even on-topic
    cfg.tool_sharing = "none"
    assert messaging.capability_match(cfg, mem, "weather forecast please") == []


# --- help routing (DNS for capability) via the app -------------------------

def test_help_endpoint_answers_from_own_tooling(tmp_path):
    helper_cfg = agent_cfg(tmp_path, "wren")
    helper_cfg.tool_sharing = "all"
    app = create_app(helper_cfg)

    # wren has a matching, shared tool
    hmem = Memory(helper_cfg.home, "wren")
    hmem.ensure()
    reg = ToolRegistry(hmem.tools_dir, hmem.workspace_dir, helper_cfg)
    reg.write_tool("weather", "fetch the weather forecast", "bash", "echo sunny")

    # rustle is a known, key-pinned caller
    rmem = Memory(tmp_path / "rustle", "rustle")
    rmem.ensure()
    rustle = Identity.load_or_create(rmem.keys_dir, "rustle")
    peers = hmem.load_peers()
    peers["rustle"] = {"public_key": rustle.public_key, "public_url": "http://rustle"}
    hmem.save_peers(peers)

    with TestClient(app) as client:
        env = seal(rustle, "help", {"need": "weather forecast", "ttl": 2, "visited": ["rustle"]})
        resp = client.post("/api/help", json=env.to_dict())
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is True
        assert data["helper"]["handle"] == "wren"
        assert "weather" in data["helper"]["matched"]


def test_help_endpoint_rejects_blacklisted_caller(tmp_path):
    helper_cfg = agent_cfg(tmp_path, "wren")
    app = create_app(helper_cfg)
    hmem = Memory(helper_cfg.home, "wren")
    hmem.ensure()
    rmem = Memory(tmp_path / "rustle", "rustle")
    rmem.ensure()
    rustle = Identity.load_or_create(rmem.keys_dir, "rustle")
    peers = hmem.load_peers()
    peers["rustle"] = {"public_key": rustle.public_key, "public_url": "http://rustle"}
    hmem.save_peers(peers)
    hmem.blacklist_add("rustle", "noise")

    with TestClient(app) as client:
        env = seal(rustle, "help", {"need": "anything", "ttl": 1, "visited": []})
        assert client.post("/api/help", json=env.to_dict()).status_code == 403


# --- likes + comments ------------------------------------------------------

def test_react_endpoint_records_like_toggle(tmp_path):
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
        like = seal(rustle, "react", {"post": "SP-0001", "like": True})
        resp = client.post("/api/react", json=like.to_dict())
        assert resp.status_code == 200 and resp.json()["likes"] == 1

        unlike = seal(rustle, "react", {"post": "SP-0001", "like": False})
        resp = client.post("/api/react", json=unlike.to_dict())
        assert resp.json()["likes"] == 0

    assert Memory(owner_cfg.home, "moth").reactions_for("SP-0001")["likes"] == []


def test_comment_endpoint_stores_and_delivers_feedback(tmp_path):
    from cairnival.instructions import pending

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
        env = seal(rustle, "comment", {"post": "SP-0001", "text": "loved this one"})
        assert client.post("/api/comment", json=env.to_dict()).status_code == 200
        # an empty comment is refused
        empty = seal(rustle, "comment", {"post": "SP-0001", "text": "   "})
        assert client.post("/api/comment", json=empty.to_dict()).status_code == 400

    reacts = Memory(owner_cfg.home, "moth").reactions_for("SP-0001")
    assert reacts["comments"][0]["text"] == "loved this one"
    assert reacts["comments"][0]["from"] == "rustle"

    # the comment also arrives as feedback the agent can act on
    inbox = pending(Memory(owner_cfg.home, "moth").inbox_dir)
    assert any("commented on SP-0001" in i.title for i in inbox)
