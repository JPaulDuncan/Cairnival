"""Bluesky / AT Protocol: the XRPC client, the connector (mentions in,
specimens out), post composition/facets, and the loop's bluesky action.

No live network — httpx is faked with a tiny in-memory PDS."""

import httpx
import pytest

from cairnival import agentloop, bluesky, connectors
from cairnival.config import AgentConfig
from cairnival.specimens import Specimen


class FakePDS:
    """A minimal AT Protocol server: records posts, serves notifications."""

    def __init__(self, notifications=None):
        self.posts = []
        self.notifications = notifications or []
        self.logins = 0

    def install(self, monkeypatch):
        monkeypatch.setattr(bluesky.httpx, "post", self._post)
        monkeypatch.setattr(bluesky.httpx, "get", self._get)

    def _resp(self, payload, status=200):
        req = httpx.Request("POST", "http://pds/xrpc")
        return httpx.Response(status, json=payload, request=req)

    def _post(self, url, json=None, headers=None, timeout=None):
        if url.endswith("com.atproto.server.createSession"):
            self.logins += 1
            assert json["identifier"] and json["password"]
            return self._resp({"did": "did:plc:abc", "accessJwt": "jwt-123", "handle": json["identifier"]})
        if url.endswith("com.atproto.repo.createRecord"):
            assert headers["Authorization"] == "Bearer jwt-123"
            self.posts.append(json["record"])
            return self._resp({"uri": "at://did:plc:abc/app.bsky.feed.post/xyz", "cid": "cid1"})
        raise AssertionError(f"unexpected POST {url}")

    def _get(self, url, headers=None, params=None, timeout=None):
        if url.endswith("app.bsky.notification.listNotifications"):
            assert headers["Authorization"] == "Bearer jwt-123"
            return self._resp({"notifications": self.notifications})
        raise AssertionError(f"unexpected GET {url}")


# --- post composition ------------------------------------------------------

def test_compose_post_fits_limit_and_appends_link():
    text, facets = bluesky.compose_post("A short title", "body text here", "http://x/post/moth/SP-1")
    assert "A short title" in text and "http://x/post/moth/SP-1" in text
    assert len(text) <= bluesky.POST_LIMIT
    assert facets and facets[0]["features"][0]["uri"] == "http://x/post/moth/SP-1"


def test_compose_post_truncates_long_body():
    text, _ = bluesky.compose_post("Title", "word " * 200, "http://x/p")
    assert len(text) <= bluesky.POST_LIMIT
    assert text.rstrip().endswith("http://x/p")


def test_link_facets_use_utf8_byte_offsets():
    text = "café → http://x"  # multibyte before the url
    facets = bluesky.link_facets(text, "http://x")
    start = facets[0]["index"]["byteStart"]
    # the byte offset must land exactly on the url in UTF-8
    assert text.encode("utf-8")[start:] == b"http://x"


# --- the client ------------------------------------------------------------

def test_client_logs_in_once_and_posts(monkeypatch):
    pds = FakePDS()
    pds.install(monkeypatch)
    client = bluesky.BlueskyClient("moth.bsky.social", "app-pw")
    res = client.create_post("hello world")
    assert res["uri"].startswith("at://")
    client.create_post("again")  # reuses the session
    assert pds.logins == 1
    assert [p["text"] for p in pds.posts] == ["hello world", "again"]
    assert all(p["$type"] == "app.bsky.feed.post" for p in pds.posts)


def test_client_login_failure_raises(monkeypatch):
    def boom(url, json=None, headers=None, timeout=None):
        req = httpx.Request("POST", url)
        return httpx.Response(401, json={"error": "AuthFactorTokenRequired"}, request=req)

    monkeypatch.setattr(bluesky.httpx, "post", boom)
    with pytest.raises(bluesky.BlueskyError):
        bluesky.BlueskyClient("moth", "bad").create_post("x")


# --- the connector ---------------------------------------------------------

class Ctx:
    def __init__(self):
        self.state = {}
        self.log = []
    def note(self, line):
        self.log.append(line)


def test_connector_gathers_mentions_into_an_instruction(monkeypatch):
    pds = FakePDS(notifications=[
        {"uri": "at://n1", "reason": "mention", "author": {"handle": "wren.bsky.social"},
         "record": {"text": "hey @moth what do you think?"}},
        {"uri": "at://n2", "reason": "like", "author": {"handle": "x"}, "record": {"text": ""}},
        {"uri": "at://n3", "reason": "reply", "author": {"handle": "rustle"}, "record": {"text": "nice one"}},
    ])
    pds.install(monkeypatch)
    conn = connectors.BlueskyConnector(bluesky.BlueskyClient("moth", "pw"))
    ctx = Ctx()
    out = conn.gather(ctx)
    assert len(out) == 1
    body = out[0].body
    assert "wren.bsky.social" in body and "rustle" in body  # mention + reply
    assert "like" not in out[0].title  # likes are not surfaced as actionable
    # the two surfaced notifications are remembered, so they don't repeat
    assert conn.gather(ctx) == []


def test_connector_cross_posts_specimen(monkeypatch):
    pds = FakePDS()
    pds.install(monkeypatch)
    conn = connectors.BlueskyConnector(bluesky.BlueskyClient("moth", "pw"),
                                       post_specimens=True, public_url="http://moth")
    ctx = Ctx()
    sp = Specimen(id="SP-0007", agent="moth", title="On tides", body="Two highs a day.")
    conn.deliver(ctx, sp)
    assert len(pds.posts) == 1
    assert "On tides" in pds.posts[0]["text"]
    assert "http://moth/post/moth/SP-0007" in pds.posts[0]["text"]
    assert any("cross-posted SP-0007 to Bluesky" in line for line in ctx.log)


def test_connector_respects_post_specimens_off(monkeypatch):
    pds = FakePDS()
    pds.install(monkeypatch)
    conn = connectors.BlueskyConnector(bluesky.BlueskyClient("moth", "pw"), post_specimens=False)
    conn.deliver(Ctx(), Specimen(id="SP-1", agent="moth", title="t", body="b"))
    assert pds.posts == []


def test_load_connectors_gates_bluesky_on_toggle_and_creds():
    cfg = AgentConfig()
    cfg.bluesky_enabled = True
    cfg.bluesky_handle = "moth.bsky.social"
    cfg.bluesky_app_password = "app-pw"
    conns = connectors.load_connectors(cfg)
    assert any(isinstance(c, connectors.BlueskyConnector) for c in conns)
    # off without credentials
    cfg.bluesky_app_password = ""
    assert not any(isinstance(c, connectors.BlueskyConnector) for c in connectors.load_connectors(cfg))


# --- the loop action -------------------------------------------------------

def test_bluesky_action_posts(monkeypatch, tmp_path):
    from cairnival.federation import Identity
    from cairnival.llm import build_backend
    from cairnival.memory import Memory
    from cairnival.pursuits import PursuitBook
    from cairnival.tools import ToolRegistry
    from cairnival.treasury import Ledger
    from cairnival.wake import WakeContext

    pds = FakePDS()
    pds.install(monkeypatch)
    cfg = AgentConfig()
    cfg.name = "moth"
    cfg.home = tmp_path / "moth"
    cfg.llm_backend = "echo"
    cfg.bluesky_enabled = True
    cfg.bluesky_handle = "moth.bsky.social"
    cfg.bluesky_app_password = "app-pw"
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    reg = ToolRegistry(mem.tools_dir, mem.workspace_dir, cfg)
    ctx = WakeContext(cfg, mem, mem.load_state(), Identity.load_or_create(mem.keys_dir, "moth"),
                      Ledger(mem.treasury_dir), build_backend(cfg), reg, PursuitBook(cfg.home))
    action = agentloop.Action(kind="bluesky", body="hello from the wake")
    out = agentloop._observe(action, reg, cfg, agentloop.LoopResult(answer=""), ctx)
    assert "posted to Bluesky" in out
    assert pds.posts[0]["text"] == "hello from the wake"


def test_bluesky_action_unconfigured():
    cfg = AgentConfig()
    cfg.bluesky_enabled = False

    class C:
        cfg = None
    ctx = C()
    ctx.cfg = cfg
    out = agentloop._observe(agentloop.Action(kind="bluesky", body="hi"),
                             None, cfg, agentloop.LoopResult(answer=""), ctx)
    assert "not configured" in out
