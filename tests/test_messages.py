"""The Messages mailbox: incoming, outgoing (Sent), Spam, Trash, and
per-agent conversation threads."""

from fastapi.testclient import TestClient

from cairnival import messages, messaging
from cairnival.agent_app import create_app
from cairnival.config import AgentConfig
from cairnival.federation import Identity, seal
from cairnival.instructions import Instruction, drop
from cairnival.memory import Memory


def agent_cfg(tmp_path, name):
    cfg = AgentConfig()
    cfg.name = name
    cfg.home = tmp_path / name
    cfg.llm_backend = "echo"
    return cfg


def _known_peer(mem, other_home, name):
    om = Memory(other_home, name)
    om.ensure()
    ident = Identity.load_or_create(om.keys_dir, name)
    peers = mem.load_peers()
    peers[name] = {"public_key": ident.public_key, "public_url": f"http://{name}"}
    mem.save_peers(peers)
    return ident


# --- storage + model ------------------------------------------------------

def test_instruction_to_field_round_trips():
    ins = Instruction(title="→ wren", body="hi", source="sent", sender="moth", to="wren")
    back = Instruction.from_markdown(ins.to_markdown())
    assert back.to == "wren" and back.source == "sent" and back.sender == "moth"


def test_record_sent_appears_in_sent_folder(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    messages.record_sent(mem, "wren", "meet by the ferris wheel", status="direct")
    sent = messages.load(mem, "sent")
    assert len(sent) == 1
    assert sent[0].to == "wren" and "ferris wheel" in sent[0].body
    assert messages.direction(sent[0]) == "out"
    assert messages.peer_of(sent[0]) == "wren"


def test_deliver_note_files_a_sent_copy(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    ident = Identity.load_or_create(mem.keys_dir, "moth")
    # no peer/hub → undeliverable, but the attempt is still filed in Sent
    ok, how = messaging.deliver_note(cfg, ident, mem, "wren", "hello wren")
    assert not ok and how == "undeliverable"
    sent = messages.load(mem, "sent")
    assert sent and sent[0].to == "wren" and "hello wren" in sent[0].body


# --- threads --------------------------------------------------------------

def test_threads_group_incoming_and_outgoing_by_peer(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    drop(mem.inbox_dir, Instruction(title="Message from rustle", body="you awake?",
                                    source="federation", sender="rustle", received="2026-01-01T00:00:00+00:00"))
    messages.record_sent(mem, "rustle", "yes, hello")
    # a message with no peer (a UI task) must not create a thread
    drop(mem.inbox_dir, Instruction(title="do a thing", body="x", source="ui"))

    convos = messages.threads(mem)
    peers = [t["peer"] for t in convos]
    assert peers == ["rustle"]
    assert convos[0]["count"] == 2  # one in, one out


# --- spam + trash ---------------------------------------------------------

def test_sweep_sender_to_spam(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    drop(mem.inbox_dir, Instruction(title="a", body="1", source="federation", sender="pest"))
    drop(mem.inbox_dir, Instruction(title="b", body="2", source="federation", sender="pest"))
    drop(mem.inbox_dir, Instruction(title="c", body="3", source="federation", sender="friend"))
    moved = messages.sweep_sender_to_spam(mem, "pest")
    assert moved == 2
    assert len(messages.load(mem, "spam")) == 2
    assert len(messages.load(mem, "inbox")) == 1  # friend's message stays


def test_mark_spam_route_blocks_and_moves(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    app = create_app(cfg)
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    path = drop(mem.inbox_dir, Instruction(title="junk", body="buy now", source="federation", sender="spammer"))
    with TestClient(app) as client:
        client.post("/messages/inbox/spam", data={"name": path.name})
    mem2 = Memory(cfg.home, "moth")
    assert mem2.is_blacklisted("spammer")
    assert len(messages.load(mem2, "spam")) == 1
    assert len(messages.load(mem2, "inbox")) == 0


def test_trash_restore_and_delete(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    app = create_app(cfg)
    mem = Memory(cfg.home, "moth")
    mem.ensure()
    path = drop(mem.inbox_dir, Instruction(title="m", body="hi", source="federation", sender="rustle"))
    with TestClient(app) as client:
        client.post("/messages/inbox/trash", data={"name": path.name})
        assert len(messages.load(mem, "trash")) == 1
        assert len(messages.load(mem, "inbox")) == 0
        # restore brings it back to the inbox
        tname = messages.load(mem, "trash")[0].path.name
        client.post("/messages/trash/restore", data={"name": tname})
        assert len(messages.load(mem, "inbox")) == 1
        # trash again, then delete forever
        client.post("/messages/inbox/trash", data={"name": messages.load(mem, "inbox")[0].path.name})
        dname = messages.load(mem, "trash")[0].path.name
        client.post("/messages/trash/delete", data={"name": dname})
        assert len(messages.load(mem, "trash")) == 0


def test_inbox_redirects_to_messages(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.get("/inbox", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/messages"


def test_messages_page_renders_folder_tabs(tmp_path):
    cfg = agent_cfg(tmp_path, "moth")
    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.get("/messages?folder=sent")
        assert r.status_code == 200
        for tab in ("Inbox", "Sent", "Read", "Spam", "Trash", "Threads"):
            assert tab in r.text
