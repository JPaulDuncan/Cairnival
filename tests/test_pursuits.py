import pytest

from cairnival.pursuits import PursuitBook


def test_start_list_and_summary(tmp_path):
    book = PursuitBook(tmp_path)
    assert book.all() == []
    p = book.start("map the tides", "first gather data", wake=3)
    assert p.id.startswith("pu-")
    assert p.status == "active"
    assert p.wake == 3
    # persisted: a fresh book (next wake) sees it
    assert len(PursuitBook(tmp_path).active()) == 1
    assert PursuitBook(tmp_path).summary() == {"active": 1, "done": 0, "total": 1}


def test_update_note_and_status(tmp_path):
    book = PursuitBook(tmp_path)
    p = book.start("build a tide tool")
    book.update(p.id, note="wrote the tool")
    assert PursuitBook(tmp_path).get(p.id).note == "wrote the tool"
    book.update(p.id, status="done")
    assert PursuitBook(tmp_path).get(p.id).status == "done"
    assert PursuitBook(tmp_path).active() == []


def test_bad_updates(tmp_path):
    book = PursuitBook(tmp_path)
    with pytest.raises(KeyError):
        book.update("pu-nope", note="x")
    p = book.start("something")
    with pytest.raises(ValueError):
        book.update(p.id, status="sideways")
    with pytest.raises(ValueError):
        book.start("   ")


def test_briefing_lists_active_only(tmp_path):
    book = PursuitBook(tmp_path)
    a = book.start("alpha")
    book.start("beta")
    book.update(a.id, status="done")
    briefing = book.briefing()
    assert "beta" in briefing
    assert "alpha" not in briefing
