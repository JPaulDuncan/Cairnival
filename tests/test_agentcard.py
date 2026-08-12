"""The public agent card at /.well-known/agent.json — discovery/description
for outside clients and agents. Aggregates only public facts."""

import json

from fastapi.testclient import TestClient

from cairnival import agentcard
from cairnival.agent_app import create_app
from cairnival.config import AgentConfig
from cairnival.economy import EconomyBook
from cairnival.memory import Memory
from cairnival.tools import ToolRegistry


def agent_cfg(tmp_path, name, **over):
    cfg = AgentConfig()
    cfg.name = name
    cfg.home = tmp_path / name
    cfg.llm_backend = "echo"
    cfg.public_url = f"http://{name}"
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def test_card_describes_identity_endpoints_and_capabilities(tmp_path):
    cfg = agent_cfg(tmp_path, "moth", tagline="a small agent", tool_sharing="all")
    app = create_app(cfg)
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    ToolRegistry(mem.tools_dir, mem.workspace_dir, cfg).write_tool(
        "greet", "greet by name", "bash", 'echo "hi $1"', ui=True, ui_inputs=["who"]
    )
    with TestClient(app) as client:
        r = client.get("/.well-known/agent.json")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        card = r.json()

    assert card["name"] == "moth" and card["handle"] == "moth"
    assert card["keyType"] == "ed25519" and card["publicKey"]
    assert card["capabilities"]["federation"] is True
    assert card["capabilities"]["mcp"] is True
    # endpoints are absolute against the public url
    assert card["endpoints"]["inbox"] == "http://moth/api/federation/inbox"
    assert card["endpoints"]["mcp"] == "http://moth/mcp"
    # shared tools appear as MCP-schema skills
    greet = next(s for s in card["skills"] if s["name"] == "greet")
    assert "who" in greet["inputSchema"]["properties"]
    # it advertises the envelope kinds it accepts
    assert "work_offer" in card["acceptsEnvelopes"] and "note" in card["acceptsEnvelopes"]
    assert "specimen" not in card["acceptsEnvelopes"]  # agent→hub only


def test_card_reflects_disabled_capabilities(tmp_path):
    cfg = agent_cfg(tmp_path, "wren", mcp_enabled=False, economy_enabled=False)
    app = create_app(cfg)
    with TestClient(app) as client:
        card = client.get("/.well-known/agent.json").json()
    assert card["capabilities"]["mcp"] is False
    assert card["capabilities"]["economy"] is False
    assert "mcp" not in card["endpoints"]
    assert "reputation" not in card["endpoints"]
    assert "skills" not in card       # no MCP → no advertised skills
    assert "reputation" not in card   # economy off → no reputation block


def test_card_shows_reputation_but_never_balance(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    book = EconomyBook(mem.home, "moth", cfg)
    book.ensure_grant()  # 1000 coins — must NOT appear in the card
    book.record_rating_received("wren", "WO-1", 5, "great")
    app = create_app(cfg)
    with TestClient(app) as client:
        card = client.get("/.well-known/agent.json").json()
    assert card["reputation"]["score"] == 5.0 and card["reputation"]["ratings"] == 1
    blob = json.dumps(card)
    assert "1000" not in blob and "balance" not in blob


def test_card_endpoint_is_open_even_with_ui_token(tmp_path):
    # discovery must not require the UI token
    cfg = agent_cfg(tmp_path, "moth", ui_token="secret")
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/.well-known/agent.json").status_code == 200


def test_fetch_reads_a_peer_card(tmp_path, monkeypatch):
    cfg = agent_cfg(tmp_path, "moth")
    app = create_app(cfg)
    with TestClient(app) as client:
        def fake_get(url, timeout=None):
            assert url == "http://moth/.well-known/agent.json"
            return client.get("/.well-known/agent.json")
        monkeypatch.setattr(agentcard.httpx, "get", fake_get)
        card = agentcard.fetch("http://moth")
    assert card and card["name"] == "moth"
    assert agentcard.fetch("") is None
