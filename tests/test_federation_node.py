"""Each agent is a federation node: it serves a directory, answers locate
queries by forwarding them, gossips to learn peers-of-peers, gathers a feed,
and can blacklist. These tests stand up several agent apps in-process and wire
them into a chain to exercise recursive discovery."""

import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from cairnival.agent_app import create_app
from cairnival.config import AgentConfig
from cairnival.federation import Identity, seal
from cairnival.llm import CliAgentBackend, build_backend
from cairnival.memory import Memory
from cairnival import messaging


# ---- CLI backends --------------------------------------------------------

def test_cli_backend_arg_and_stdin():
    # arg substitution: printf %s <full prompt>
    arg_backend = CliAgentBackend("x", ["printf", "%s", "{prompt}"], use_stdin=False)
    assert "hello" in arg_backend.chat("SYS", "hello")
    # stdin: cat echoes the prompt piped in
    stdin_backend = CliAgentBackend("y", ["cat"], use_stdin=True)
    assert "hello" in stdin_backend.chat("SYS", "hello")


def test_build_backend_selects_cli(tmp_path):
    for name in ("claude-cli", "codex-cli"):
        cfg = AgentConfig()
        cfg.llm_backend = name
        assert isinstance(build_backend(cfg), CliAgentBackend)
        assert build_backend(cfg).describe() == name


def test_cli_backend_strips_thinking():
    b = CliAgentBackend("x", ["printf", "%s", "{prompt}"], use_stdin=False)
    # the "prompt" here is what printf echoes back; simulate leaked reasoning
    out = b.chat("", "<think>secret</think>answer")
    assert out == "answer"


# ---- an in-process federation of agent nodes -----------------------------

class Node:
    def __init__(self, tmp_path, name, port):
        cfg = AgentConfig()
        cfg.name = name
        cfg.home = tmp_path / name
        cfg.llm_backend = "echo"
        cfg.public_url = f"http://127.0.0.1:{port}"
        cfg.ui_port = port
        cfg.gossip_enabled = True
        cfg.locate_ttl = 4
        self.cfg = cfg
        self.port = port
        self.app = create_app(cfg)
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="error")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(100):
            try:
                httpx.get(f"{self.cfg.public_url}/api/status", timeout=1)
                return
            except httpx.HTTPError:
                time.sleep(0.05)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @property
    def memory(self):
        m = Memory(self.cfg.home, self.cfg.name)
        m.ensure()
        return m

    @property
    def identity(self):
        return Identity.load_or_create(self.memory.keys_dir, self.cfg.name)


def introduce(a: Node, b: Node):
    """a learns b directly (a hello, so keys pin and urls are known)."""
    env = seal(a.identity, "hello", {"public_url": a.cfg.public_url, "tagline": a.cfg.name})
    httpx.post(f"{b.cfg.public_url}/api/federation/inbox", json=env.to_dict()).raise_for_status()
    env2 = seal(b.identity, "hello", {"public_url": b.cfg.public_url, "tagline": b.cfg.name})
    httpx.post(f"{a.cfg.public_url}/api/federation/inbox", json=env2.to_dict()).raise_for_status()


@pytest.fixture()
def chain(tmp_path):
    # X — R — W — Y  (each only knows its neighbour)
    nodes = {
        "X": Node(tmp_path, "x", 8731),
        "R": Node(tmp_path, "r", 8732),
        "W": Node(tmp_path, "w", 8733),
        "Y": Node(tmp_path, "y", 8734),
    }
    for n in nodes.values():
        n.start()
    introduce(nodes["X"], nodes["R"])
    introduce(nodes["R"], nodes["W"])
    introduce(nodes["W"], nodes["Y"])
    yield nodes
    for n in nodes.values():
        n.stop()


def test_directory_endpoint_lists_known_agents(chain):
    r = httpx.get(f"{chain['R'].cfg.public_url}/api/directory").json()["agents"]
    assert "r" in r  # itself
    assert "x" in r and "w" in r  # its neighbours


def test_recursive_locate_finds_distant_agent(chain):
    # X does not know Y; only reachable via R -> W. The query must forward.
    x = chain["X"]
    assert "y" not in x.memory.load_peers()
    loc = messaging.locate(x.cfg, x.identity, x.memory, "y")
    assert loc is not None
    assert loc["handle"] == "y"
    assert loc["public_url"] == chain["Y"].cfg.public_url
    # and X now knows Y, so it can message it directly
    assert "y" in x.memory.load_peers()


def test_gossip_learns_peers_of_peers(chain):
    x = chain["X"]
    assert "w" not in x.memory.load_peers()  # X only knew R
    messaging.gossip_peers(x.cfg, x.identity, x.memory)
    assert "w" in x.memory.load_peers()  # learned R's neighbour W


def test_blacklist_blocks_inbound(chain):
    r, x = chain["R"], chain["X"]
    r.memory.blacklist_add("x", "testing")
    env = seal(x.identity, "note", {"text": "let me in"})
    resp = httpx.post(f"{r.cfg.public_url}/api/federation/inbox", json=env.to_dict())
    assert resp.status_code == 403
    # and R won't forward a locate for a blacklisted sender either
    loc_env = seal(x.identity, "locate", {"target": "y", "ttl": 3, "visited": []})
    assert httpx.post(f"{r.cfg.public_url}/api/locate", json=loc_env.to_dict()).status_code == 403


def test_posts_endpoint_and_feed_gather(chain):
    from cairnival.specimens import Specimen, save as save_sp

    w, x = chain["W"], chain["X"]
    save_sp(Specimen(id="SP-0001", agent="w", title="hi from W", body="a post"), w.memory.specimens_dir)
    # W is reachable from X only after gossip; introduce the post source directly
    introduce(x, w)
    n = messaging.gather_feed(x.cfg, x.identity, x.memory, own_posts=[])
    assert n >= 1
    cached = x.memory.load_feed_cache()
    assert any(p["agent"] == "w" and p["title"] == "hi from W" for p in cached)
