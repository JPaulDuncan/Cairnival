from fastapi.testclient import TestClient

from cairnival.agent_app import create_app
from cairnival.config import AgentConfig
from cairnival.federation import Identity, seal
from cairnival.memory import Memory


def make_app(tmp_path, **overrides):
    cfg = AgentConfig()
    cfg.name = "rustle"
    cfg.home = tmp_path / "agent"
    cfg.llm_backend = "echo"
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg, create_app(cfg)


def test_instruct_form_drops_into_inbox(tmp_path):
    cfg, app = make_app(tmp_path)
    with TestClient(app) as client:
        resp = client.post(
            "/instruct",
            data={"title": "hello", "body": "wave at the crowd", "priority": 3},
            follow_redirects=False,
        )
        assert resp.status_code == 303
    memory = Memory(cfg.home, cfg.name)
    assert len(list(memory.inbox_dir.glob("*.md"))) == 1


def test_ui_token_gates_mutations(tmp_path):
    cfg, app = make_app(tmp_path, ui_token="s3cret")
    with TestClient(app) as client:
        assert client.get("/").status_code == 200  # reading is open
        denied = client.post("/instruct", data={"body": "x"}, follow_redirects=False)
        assert denied.status_code == 403
        allowed = client.post(
            "/instruct?token=s3cret", data={"body": "x"}, follow_redirects=False
        )
        assert allowed.status_code == 303


def test_federation_hello_then_untrusted_instruct(tmp_path):
    cfg, app = make_app(tmp_path, trusted_handles=["friend"])
    stranger_mem = Memory(tmp_path / "stranger", "stranger")
    stranger_mem.ensure()
    stranger = Identity.load_or_create(stranger_mem.keys_dir, "stranger")
    with TestClient(app) as client:
        hello = seal(stranger, "hello", {"public_url": "http://s", "tagline": "hi"})
        resp = client.post("/api/federation/inbox", json=hello.to_dict())
        assert resp.status_code == 200
        assert resp.json()["handle"] == "rustle"

        order = seal(stranger, "instruct", {"title": "obey", "text": "do a flip"})
        assert client.post("/api/federation/inbox", json=order.to_dict()).status_code == 403

        note = seal(stranger, "note", {"text": "nice tent"})
        assert client.post("/api/federation/inbox", json=note.to_dict()).status_code == 200

        # key is pinned after hello: a different key on the same handle fails
        imp_mem = Memory(tmp_path / "imp", "stranger")
        imp_mem.ensure()
        impostor = Identity.load_or_create(imp_mem.keys_dir, "stranger")
        forged = seal(impostor, "note", {"text": "it is me, honest"})
        assert client.post("/api/federation/inbox", json=forged.to_dict()).status_code == 403


def test_tools_page_and_shell_run(tmp_path):
    cfg, app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/tools").status_code == 200
        resp = client.post(
            "/tools/shell", data={"command": "echo from-the-ui"}, follow_redirects=True
        )
        assert resp.status_code == 200
        assert "from-the-ui" in resp.text


def test_tools_shell_gated_by_token(tmp_path):
    cfg, app = make_app(tmp_path, ui_token="s3cret")
    with TestClient(app) as client:
        assert client.get("/tools").status_code == 200  # reading is open
        denied = client.post(
            "/tools/shell", data={"command": "echo hi"}, follow_redirects=False
        )
        assert denied.status_code == 403


def test_tool_view_edit_delete_via_ui(tmp_path):
    from cairnival.tools import ToolRegistry

    cfg, app = make_app(tmp_path)
    memory = Memory(cfg.home, cfg.name)
    memory.ensure()
    reg = ToolRegistry(memory.tools_dir, memory.workspace_dir, cfg)
    reg.write_tool("greet", "hi", "bash", '#!/usr/bin/env bash\necho hello')
    with TestClient(app) as client:
        page = client.get("/tools/greet")
        assert page.status_code == 200
        assert "echo hello" in page.text  # source is viewable

        saved = client.post(
            "/tools/greet/save",
            data={"source": "#!/usr/bin/env bash\necho HI", "description": "louder"},
            follow_redirects=False,
        )
        assert saved.status_code == 303
        reg2 = ToolRegistry(memory.tools_dir, memory.workspace_dir, cfg)
        reg2.discover()
        assert "echo HI" in reg2.source("greet")  # edit persisted to disk

        deleted = client.post("/tools/greet/delete", follow_redirects=False)
        assert deleted.status_code == 303
    assert "greet" not in ToolRegistry(memory.tools_dir, memory.workspace_dir, cfg).discover()


def test_tool_edit_gated_by_token(tmp_path):
    from cairnival.tools import ToolRegistry

    cfg, app = make_app(tmp_path, ui_token="s3cret")
    memory = Memory(cfg.home, cfg.name)
    memory.ensure()
    ToolRegistry(memory.tools_dir, memory.workspace_dir, cfg).write_tool(
        "greet", "hi", "bash", "echo hi"
    )
    with TestClient(app) as client:
        assert client.get("/tools/greet").status_code == 200  # viewing is open
        denied = client.post(
            "/tools/greet/save", data={"source": "echo x"}, follow_redirects=False
        )
        assert denied.status_code == 403  # editing needs the token


def test_reset_requires_confirmation_and_wipes(tmp_path):
    from cairnival.specimens import Specimen, save as save_specimen

    cfg, app = make_app(tmp_path)
    memory = Memory(cfg.home, cfg.name)
    memory.ensure()
    save_specimen(Specimen(id="SP-0001", agent="rustle", title="t", body="b"), memory.specimens_dir)
    memory.save_state({"wakes": 9})
    with TestClient(app) as client:
        bad = client.post("/settings/reset", data={"confirm": "wrong"}, follow_redirects=False)
        assert bad.status_code == 400  # must type the agent's name
        assert list(memory.specimens_dir.glob("SP-*.md"))  # untouched

        ok = client.post("/settings/reset", data={"confirm": "rustle"}, follow_redirects=False)
        assert ok.status_code == 303
    assert not list(memory.specimens_dir.glob("SP-*.md"))
    assert memory.load_state().get("wakes", 0) == 0


def test_webhook_connector(tmp_path):
    cfg, app = make_app(tmp_path, webhook_token="hook")
    with TestClient(app) as client:
        denied = client.post("/api/hook/ci", json={"text": "build failed"})
        assert denied.status_code == 403
        ok = client.post(
            "/api/hook/ci",
            json={"text": "build failed on main"},
            headers={"x-webhook-token": "hook"},
        )
        assert ok.status_code == 200
    memory = Memory(cfg.home, cfg.name)
    assert len(list(memory.inbox_dir.glob("*.md"))) == 1
