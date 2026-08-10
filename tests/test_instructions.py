from cairnival.instructions import Instruction, archive, drop, pending
from cairnival.specimens import Specimen, next_id, save, load_all


def test_instruction_roundtrip():
    ins = Instruction(
        title="Answer the tides question",
        body="Why are there two high tides a day?",
        source="email",
        sender="a@example.com",
        reply_to="a@example.com",
        priority=3,
        paid=0.02,
    )
    parsed = Instruction.from_markdown(ins.to_markdown())
    assert parsed.title == ins.title
    assert parsed.body == ins.body
    assert parsed.source == "email"
    assert parsed.reply_to == "a@example.com"
    assert parsed.priority == 3
    assert parsed.paid == 0.02
    assert parsed.is_paid


def test_untitled_body_gets_title():
    parsed = Instruction.from_markdown("# Do the thing\nwith care")
    assert parsed.title == "Do the thing"


def test_inbox_ordering_paid_first(tmp_path):
    inbox = tmp_path / "inbox"
    drop(inbox, Instruction(title="later", body="x", priority=7))
    drop(inbox, Instruction(title="urgent", body="x", priority=1))
    drop(inbox, Instruction(title="paid", body="x", priority=9, paid=0.5))
    queue = pending(inbox)
    assert [i.title for i in queue] == ["paid", "urgent", "later"]


def test_archive_moves_file(tmp_path):
    inbox, arch = tmp_path / "inbox", tmp_path / "archive"
    drop(inbox, Instruction(title="one", body="x"))
    ins = pending(inbox)[0]
    archive(ins, arch)
    assert not list(inbox.glob("*.md"))
    assert len(list(arch.glob("*.md"))) == 1


def test_specimen_roundtrip_and_ids(tmp_path):
    drawer = tmp_path / "specimens"
    assert next_id(drawer) == "SP-0001"
    sp = Specimen(
        id="SP-0001",
        agent="rustle",
        title="First wake",
        body="I woke. The inbox was empty.\n\nStill, the midway smelled of rain.",
        wake=1,
        instrument="echo",
        tags=["quiet"],
    )
    save(sp, drawer)
    assert next_id(drawer) == "SP-0002"
    loaded = load_all(drawer)[0]
    assert loaded.title == "First wake"
    assert loaded.body == sp.body
    assert loaded.tags == ["quiet"]
    assert Specimen.from_dict(sp.to_dict()).body == sp.body
