"""The coin economy over the wire: two live agent nodes running the full
signed escrow (offer → accept → submit → release → rate), plus the reputation
endpoint. Delivery is routed through the TestClients."""

import httpx
import pytest
from fastapi.testclient import TestClient

from cairnival import economy_net
from cairnival.agent_app import create_app
from cairnival.config import AgentConfig
from cairnival.economy import EconomyBook
from cairnival.federation import Identity
from cairnival.memory import Memory


def make(tmp_path, name):
    cfg = AgentConfig()
    cfg.name = name
    cfg.home = tmp_path / name
    cfg.llm_backend = "echo"
    cfg.public_url = f"http://{name}"
    return cfg


def test_end_to_end_escrow_between_two_agents(tmp_path, monkeypatch):
    asker_cfg = make(tmp_path, "moth")
    doer_cfg = make(tmp_path, "wren")
    asker_app = create_app(asker_cfg)
    doer_app = create_app(doer_cfg)

    with TestClient(asker_app) as ac, TestClient(doer_app) as dc:
        amem = Memory(asker_cfg.home, "moth"); amem.ensure()
        dmem = Memory(doer_cfg.home, "wren"); dmem.ensure()
        aid = Identity.load_or_create(amem.keys_dir, "moth")
        did = Identity.load_or_create(dmem.keys_dir, "wren")
        # pin each other + know URLs
        amem.save_peers({"wren": {"public_key": did.public_key, "public_url": "http://wren"}})
        dmem.save_peers({"moth": {"public_key": aid.public_key, "public_url": "http://moth"}})
        # grant coins
        EconomyBook(amem.home, "moth", asker_cfg).ensure_grant()
        EconomyBook(dmem.home, "wren", doer_cfg).ensure_grant()

        # route economy_net's httpx.post at each node's inbox through the clients
        clients = {"http://moth": ac, "http://wren": dc}

        def fake_post(url, json=None, timeout=None, headers=None):
            for base, client in clients.items():
                if url.startswith(base):
                    return client.post(url[len(base):], json=json)
            raise AssertionError(url)

        monkeypatch.setattr(economy_net.httpx, "post", fake_post)

        # 1. asker offers wren a 100-coin job
        order, ok, how = economy_net.send_offer(asker_cfg, aid, amem, "wren", 100, "count the words", "wordcount")
        assert ok and how == "direct"
        abook = EconomyBook(amem.home, "moth", asker_cfg)
        dbook = EconomyBook(dmem.home, "wren", doer_cfg)
        assert abook.reserved() == 100 and abook.balance() == 1000
        # wren has the offer in its book + an inbox notice
        assert dbook.get(order.id) is not None

        # 2. wren accepts → asker debits escrow
        economy_net.send_accept(doer_cfg, did, dmem, order.id)
        abook = EconomyBook(amem.home, "moth", asker_cfg)
        assert abook.balance() == 900 and abook.escrowed() == 100
        assert abook.get(order.id).accept_sig  # the doer's key-part is stored

        # 3. wren submits, asker releases
        economy_net.send_submit(doer_cfg, did, dmem, order.id, "42 words")
        economy_net.send_release(asker_cfg, aid, amem, order.id)
        abook = EconomyBook(amem.home, "moth", asker_cfg)
        dbook = EconomyBook(dmem.home, "wren", doer_cfg)
        assert abook.get(order.id).state == "completed"
        # wren credited 100 + 5% dividend = 105
        assert dbook.balance() == 1105
        assert dbook.get(order.id).release_sig  # the asker's key-part is stored

        # 4. both rate each other
        economy_net.send_rate(asker_cfg, aid, amem, order.id, 5, "fast and correct")
        economy_net.send_rate(doer_cfg, did, dmem, order.id, 4, "clear brief")
        assert EconomyBook(dmem.home, "wren", doer_cfg).reputation()["score"] == 5.0
        assert EconomyBook(amem.home, "moth", asker_cfg).reputation()["score"] == 4.0

        # the reputation endpoint is public
        rep = dc.get("/api/reputation").json()
        assert rep["handle"] == "wren" and rep["reputation"]["score"] == 5.0
        assert rep["jobs_completed"] == 1


def test_offer_to_unreachable_doer_unreserves(tmp_path, monkeypatch):
    cfg = make(tmp_path, "moth")
    app = create_app(cfg)
    with TestClient(app) as c:
        mem = Memory(cfg.home, "moth"); mem.ensure()
        aid = Identity.load_or_create(mem.keys_dir, "moth")
        EconomyBook(mem.home, "moth", cfg).ensure_grant()
        # no peer known and no hub → undeliverable
        def dead_post(url, json=None, timeout=None, headers=None):
            raise httpx.ConnectError("no route")
        monkeypatch.setattr(economy_net.httpx, "post", dead_post)
        order, ok, how = economy_net.send_offer(cfg, aid, mem, "ghost", 50, "do a thing")
        assert not ok
        book = EconomyBook(mem.home, "moth", cfg)
        assert book.reserved() == 0 and book.available() == 1000  # reservation released
