"""The job board: open jobs, bids, awards, progress, kanban columns, discovery,
and an end-to-end open-job flow (post → discover → bid → award → progress →
submit → release) across two live agent nodes."""

import pytest
from fastapi.testclient import TestClient

from cairnival import economy_net
from cairnival.agent_app import create_app
from cairnival.config import AgentConfig
from cairnival.economy import EconomyBook, EconomyError, WorkOrder, kanban_column
from cairnival.federation import Identity
from cairnival.memory import Memory


def book(tmp_path, name):
    cfg = AgentConfig()
    b = EconomyBook(tmp_path / name, name, cfg)
    b.ensure_grant()
    return b


# --- core: open jobs, bids, award, progress -------------------------------

def test_open_job_is_on_the_board(tmp_path):
    b = book(tmp_path, "moth")
    o = b.create_offer(coins=100, criteria="scrape a page", title="scrape")
    assert o.is_open and o.doer == "" and o.column() == "open"
    assert [j.id for j in b.board()] == [o.id]
    assert b.reserved() == 100 and b.available() == 900


def test_bids_recorded_one_per_agent(tmp_path):
    b = book(tmp_path, "moth")
    o = b.create_offer(coins=50, criteria="x")
    b.add_bid(o.id, "wren", "I can do this fast")
    b.add_bid(o.id, "rustle", "me too")
    b.add_bid(o.id, "wren", "updated pitch")  # replaces wren's earlier bid
    bids = b.get(o.id).bids
    assert len(bids) == 2
    assert next(x for x in bids if x["from"] == "wren")["note"] == "updated pitch"
    with pytest.raises(EconomyError):
        b.add_bid(o.id, "moth", "self")  # can't bid on your own job


def test_award_debits_escrow_and_closes_the_board(tmp_path):
    b = book(tmp_path, "moth")
    o = b.create_offer(coins=100, criteria="x")
    b.add_bid(o.id, "wren", "pick me")
    b.award(o.id, "wren")
    awarded = b.get(o.id)
    assert awarded.doer == "wren" and awarded.state == "accepted"
    assert b.balance() == 900 and b.escrowed() == 100
    assert b.board() == []  # no longer open
    assert awarded.column() == "in_progress"


def test_progress_trail_and_kanban_columns(tmp_path):
    b = book(tmp_path, "moth")
    o = b.create_offer(coins=10, criteria="x")
    b.award(o.id, "wren")
    b.add_progress(o.id, "wren", "halfway")
    b.add_progress(o.id, "wren", "almost done")
    assert [p["note"] for p in b.get(o.id).progress] == ["halfway", "almost done"]
    assert kanban_column("offered") == "open"
    assert kanban_column("bid") == "in_progress"
    assert kanban_column("submitted") == "review"
    assert kanban_column("completed") == "done"
    assert kanban_column("expired") == "closed"


def test_doer_records_bid_then_award(tmp_path):
    doer = book(tmp_path, "wren")
    job = WorkOrder(id="WO-x", asker="moth", doer="", coins=40, criteria="do it")
    doer.place_bid(job, "my pitch")
    assert doer.get("WO-x").state == "bid" and doer.get("WO-x").column() == "in_progress"
    doer.record_award(job)
    assert doer.get("WO-x").state == "accepted" and doer.get("WO-x").doer == "wren"


# --- end-to-end open-job flow across two live nodes -----------------------

def make(tmp_path, name):
    cfg = AgentConfig()
    cfg.name = name
    cfg.home = tmp_path / name
    cfg.llm_backend = "echo"
    cfg.public_url = f"http://{name}"
    return cfg


def test_open_job_end_to_end(tmp_path, monkeypatch):
    a_cfg = make(tmp_path, "moth")   # poster
    d_cfg = make(tmp_path, "wren")   # worker
    a_app, d_app = create_app(a_cfg), create_app(d_cfg)
    with TestClient(a_app) as ac, TestClient(d_app) as dc:
        amem = Memory(a_cfg.home, "moth"); amem.ensure()
        dmem = Memory(d_cfg.home, "wren"); dmem.ensure()
        aid = Identity.load_or_create(amem.keys_dir, "moth")
        did = Identity.load_or_create(dmem.keys_dir, "wren")
        amem.save_peers({"wren": {"public_key": did.public_key, "public_url": "http://wren"}})
        dmem.save_peers({"moth": {"public_key": aid.public_key, "public_url": "http://moth"}})
        EconomyBook(amem.home, "moth", a_cfg).ensure_grant()
        EconomyBook(dmem.home, "wren", d_cfg).ensure_grant()

        clients = {"http://moth": ac, "http://wren": dc}

        def route(url, json=None, timeout=None, headers=None, params=None):
            for base, client in clients.items():
                if url.startswith(base):
                    path = url[len(base):]
                    return client.get(path, params=params) if json is None and params is not None else \
                        (client.get(path) if json is None else client.post(path, json=json))
            raise AssertionError(url)

        monkeypatch.setattr(economy_net.httpx, "post", lambda url, json=None, timeout=None, headers=None: route(url, json=json))
        monkeypatch.setattr(economy_net.httpx, "get", lambda url, timeout=None, params=None: route(url, params=params or {}))

        # 1. moth posts an OPEN job
        abook = EconomyBook(amem.home, "moth", a_cfg)
        job = abook.create_offer(coins=120, criteria="count words", title="wordcount")
        assert job.is_open

        # 2. wren discovers it on moth's board
        found = economy_net.discover_jobs(d_cfg, dmem, limit=10)
        assert any(j["id"] == job.id and j["asker"] == "moth" for j in found)

        # 3. wren bids
        wo = WorkOrder(id=job.id, asker="moth", doer="", coins=120, criteria="count words")
        ok, _ = economy_net.send_bid(d_cfg, did, dmem, wo, "I'll do it in one wake")
        assert ok
        assert EconomyBook(amem.home, "moth", a_cfg).get(job.id).bids[0]["from"] == "wren"

        # 4. moth awards wren → escrow debited; wren engaged
        economy_net.send_award(a_cfg, aid, amem, job.id, "wren")
        assert EconomyBook(amem.home, "moth", a_cfg).balance() == 880
        assert EconomyBook(dmem.home, "wren", d_cfg).get(job.id).state == "accepted"

        # 5. wren posts progress, then submits
        economy_net.send_progress(d_cfg, did, dmem, job.id, "halfway there")
        assert EconomyBook(amem.home, "moth", a_cfg).get(job.id).progress[-1]["note"] == "halfway there"
        economy_net.send_submit(d_cfg, did, dmem, job.id, "180 words")

        # 6. moth releases → wren paid + dividend
        economy_net.send_release(a_cfg, aid, amem, job.id)
        dbook = EconomyBook(dmem.home, "wren", d_cfg)
        assert dbook.get(job.id).state == "completed"
        assert dbook.balance() == 1000 + 120 + 6  # 5% dividend on 120

        # the public board endpoint no longer lists the (now-awarded) job
        assert ac.get("/api/board").json()["jobs"] == []
