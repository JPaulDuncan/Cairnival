from fastapi.testclient import TestClient

from cairnival.agent_app import create_app
from cairnival.config import AgentConfig
from cairnival.settings import (
    apply_form,
    load_agent_config,
    read_overrides,
    write_overrides,
)


def base_cfg(tmp_path) -> AgentConfig:
    cfg = AgentConfig()
    cfg.name = "rustle"
    cfg.home = tmp_path / "agent"
    cfg.home.mkdir(parents=True, exist_ok=True)
    cfg.llm_backend = "echo"
    return cfg


def test_overrides_win_over_base(tmp_path):
    cfg = base_cfg(tmp_path)
    write_overrides(
        cfg.home,
        {
            "tagline": "changed in the UI",
            "wake_interval_minutes": 5,
            "peers": ["http://moth:8700"],
            "email_enabled": True,
        },
    )
    loaded = load_agent_config(cfg)
    assert loaded.tagline == "changed in the UI"
    assert loaded.wake_interval_minutes == 5
    assert loaded.peers == ["http://moth:8700"]
    assert loaded.email_enabled is True
    # base object untouched; process-level fields never overridden
    assert cfg.tagline != "changed in the UI"
    assert loaded.home == cfg.home


def test_bad_or_process_only_overrides_ignored(tmp_path):
    cfg = base_cfg(tmp_path)
    write_overrides(
        cfg.home,
        {"home": "/etc", "ui_port": 1, "wake_interval_minutes": "not-a-number"},
    )
    loaded = load_agent_config(cfg)
    assert loaded.home == cfg.home
    assert loaded.ui_port == cfg.ui_port
    assert loaded.wake_interval_minutes == cfg.wake_interval_minutes


def test_apply_form_secrets_blank_keeps_dash_clears(tmp_path):
    home = tmp_path / "agent"
    home.mkdir()
    apply_form(home, {"imap_password": "hunter2", "tagline": "one"})
    assert read_overrides(home)["imap_password"] == "hunter2"
    # blank secret keeps the stored value
    apply_form(home, {"imap_password": "", "tagline": "two"})
    data = read_overrides(home)
    assert data["imap_password"] == "hunter2"
    assert data["tagline"] == "two"
    # the sentinel clears it
    apply_form(home, {"imap_password": "-"})
    assert read_overrides(home)["imap_password"] == ""


def test_settings_page_and_save_apply_live(tmp_path):
    cfg = base_cfg(tmp_path)
    app = create_app(cfg)
    with TestClient(app) as client:
        page = client.get("/settings")
        assert page.status_code == 200
        assert "Wake interval" in page.text

        resp = client.post(
            "/settings",
            data={
                "tagline": "configured in the browser",
                "wake_interval_minutes": "7",
                "llm_backend": "echo",
                "trusted_handles": "moth, wren",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # the dashboard reflects the change with no restart
        assert "configured in the browser" in client.get("/").text

    loaded = load_agent_config(cfg)
    assert loaded.wake_interval_minutes == 7
    assert loaded.trusted_handles == ["moth", "wren"]


def test_soul_editable_via_ui(tmp_path):
    cfg = base_cfg(tmp_path)
    app = create_app(cfg)
    with TestClient(app) as client:
        resp = client.post(
            "/settings/soul", data={"soul": "# SOUL\n\nBe brief."}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert "Be brief." in client.get("/settings").text
    assert (cfg.home / "SOUL.md").read_text() == "# SOUL\n\nBe brief."


def test_settings_gated_by_ui_token(tmp_path):
    cfg = base_cfg(tmp_path)
    cfg.ui_token = "s3cret"
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/settings").status_code == 403
        assert client.get("/settings?token=s3cret").status_code == 200
