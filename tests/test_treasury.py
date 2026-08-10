import pytest

from cairnival.treasury import Ledger


def test_deposit_and_balance(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.deposit(1.5, "nick", "")
    ledger.deposit(0.5)
    assert ledger.balance() == 2.0


def test_memo_deposit_becomes_paid_question(tmp_path):
    ledger = Ledger(tmp_path)
    dep = ledger.deposit(0.05, "asker", "Why are there two high tides a day?")
    ledger.deposit(0.01, "cheap", "under the ask price")
    memos = ledger.unconsumed_memo_deposits(min_amount=0.02)
    assert [m["id"] for m in memos] == [dep["id"]]
    ledger.consume_memo(dep["id"])
    assert ledger.unconsumed_memo_deposits(0.02) == []


def test_spend_requires_cosign_and_funds(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.deposit(1.0)
    prop = ledger.propose("vendor", 0.4, "domain renewal")
    # nothing moved yet — only proposed
    assert ledger.balance() == 1.0
    ledger.resolve(prop.id, approve=True)
    assert ledger.balance() == 0.6

    big = ledger.propose("vendor", 10.0, "too much")
    with pytest.raises(ValueError):
        ledger.resolve(big.id, approve=True)

    rej = ledger.propose("vendor", 0.1, "no thanks")
    ledger.resolve(rej.id, approve=False)
    assert ledger.balance() == 0.6
    assert ledger.proposals("rejected")[0]["id"] == rej.id


def test_double_resolution_rejected(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.deposit(1.0)
    prop = ledger.propose("vendor", 0.2, "once only")
    ledger.resolve(prop.id, approve=True)
    with pytest.raises(ValueError):
        ledger.resolve(prop.id, approve=True)
