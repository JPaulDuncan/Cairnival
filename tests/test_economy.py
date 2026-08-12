"""The coin economy core: the grant, escrow lifecycle, conservation,
reputation, minting, and deadlines. Pure ledger logic — no network."""

import pytest

from cairnival.config import AgentConfig
from cairnival.economy import EconomyBook, EconomyError, WorkOrder


def book(tmp_path, name, **cfg_over):
    cfg = AgentConfig()
    for k, v in cfg_over.items():
        setattr(cfg, k, v)
    b = EconomyBook(tmp_path / name, name, cfg)
    b.ensure_grant()
    return b


# --- the grant ------------------------------------------------------------

def test_starting_grant_is_once(tmp_path):
    b = EconomyBook(tmp_path / "moth", "moth", AgentConfig())
    assert b.balance() == 0
    assert b.ensure_grant() is True
    assert b.balance() == 1000
    assert b.ensure_grant() is False  # idempotent
    assert b.balance() == 1000
    assert b.minted() == 1000


def test_starting_balance_configurable(tmp_path):
    b = book(tmp_path, "moth", economy_starting_balance=500)
    assert b.balance() == 500


# --- offering + reservation ----------------------------------------------

def test_offer_reserves_but_does_not_debit(tmp_path):
    b = book(tmp_path, "moth")
    o = b.create_offer("wren", 200, "scrape a page")
    assert o.state == "offered" and o.coins == 200
    assert b.balance() == 1000            # not debited yet
    assert b.reserved() == 200
    assert b.available() == 800


def test_cannot_offer_more_than_available(tmp_path):
    b = book(tmp_path, "moth")
    b.create_offer("wren", 700, "a")
    with pytest.raises(EconomyError):
        b.create_offer("rustle", 400, "b")  # 700 + 400 > 1000
    assert b.available() == 300


def test_cannot_hire_self_or_zero(tmp_path):
    b = book(tmp_path, "moth")
    with pytest.raises(EconomyError):
        b.create_offer("moth", 10, "x")
    with pytest.raises(EconomyError):
        b.create_offer("wren", 0, "x")


# --- the full escrow lifecycle across two books --------------------------

def test_full_escrow_conserves_and_mints(tmp_path):
    asker = book(tmp_path, "moth")
    doer = book(tmp_path, "wren")

    # asker offers
    o = asker.create_offer("wren", 100, "count the words", title="wordcount")
    # doer receives the offer and accepts
    doer.record_offer(WorkOrder.from_dict(o.to_dict()))
    doer.accept(o.id)
    # asker records the doer's signed acceptance → escrow debited
    asker.record_acceptance(o.id, accept_sig="sig-doer")
    assert asker.balance() == 900
    assert asker.escrowed() == 100
    assert asker.reserved() == 0

    # doer submits, asker releases
    doer.submit(o.id, "42 words")
    asker.record_submission(o.id, "42 words")
    asker.release(o.id, release_sig="sig-asker")
    assert asker.escrowed() == 0
    assert asker.balance() == 900  # the 100 is gone from the asker for good

    # doer credits on the asker's release, plus the completion dividend (5%)
    order, credited = doer.receive_release(o.id, release_sig="sig-asker")
    assert order.state == "completed"
    assert doer.balance() == 1000 + 100 + 5   # earn 100 + minted dividend 5
    assert credited == 105

    # the 100 was conserved (asker -100, doer +100); the +5 is newly minted
    assert asker.balance() + doer.balance() == 2000 + 5


def test_receive_release_is_idempotent(tmp_path):
    asker = book(tmp_path, "moth")
    doer = book(tmp_path, "wren")
    o = asker.create_offer("wren", 100, "x")
    doer.record_offer(WorkOrder.from_dict(o.to_dict()))
    doer.accept(o.id)
    doer.submit(o.id, "done")
    doer.receive_release(o.id, "sig")
    bal = doer.balance()
    doer.receive_release(o.id, "sig")  # a re-delivered release must not double-pay
    assert doer.balance() == bal


# --- refunds & deadlines --------------------------------------------------

def test_cancel_offer_unreserves(tmp_path):
    b = book(tmp_path, "moth")
    o = b.create_offer("wren", 300, "x")
    b.refund(o.id, "cancelled")
    assert b.reserved() == 0 and b.available() == 1000
    assert b.get(o.id).state == "cancelled"


def test_expire_refunds_escrow(tmp_path):
    asker = book(tmp_path, "moth")
    o = asker.create_offer("wren", 250, "x", deadline="2000-01-01T00:00:00+00:00")
    asker.record_acceptance(o.id, "sig")
    assert asker.balance() == 750 and asker.escrowed() == 250
    expired = asker.expire_overdue()
    assert o.id in expired
    assert asker.balance() == 1000 and asker.escrowed() == 0
    assert asker.get(o.id).state == "expired"


# --- reputation -----------------------------------------------------------

def test_rating_requires_completion_and_is_once(tmp_path):
    asker = book(tmp_path, "moth")
    doer = book(tmp_path, "wren")
    o = asker.create_offer("wren", 100, "x")
    doer.record_offer(WorkOrder.from_dict(o.to_dict()))
    doer.accept(o.id)
    with pytest.raises(EconomyError):
        asker.rate(o.id, 5)  # not completed yet
    doer.submit(o.id, "done")
    asker.record_acceptance(o.id, "s")
    asker.release(o.id, "s")
    asker.rate(o.id, 5, "great work")
    with pytest.raises(EconomyError):
        asker.rate(o.id, 4)  # already rated


def test_reputation_aggregates_received_ratings(tmp_path):
    doer = book(tmp_path, "wren")
    doer.record_rating_received("moth", "WO-1", 5, "excellent")
    doer.record_rating_received("rustle", "WO-2", 3)
    doer.record_rating_received("moth", "WO-1", 4)  # dup for same order ignored
    rep = doer.reputation()
    assert rep["count"] == 2
    assert rep["raters"] == 2
    assert rep["score"] == 4.0
