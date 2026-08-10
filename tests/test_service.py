from pathlib import Path

from cairnival.service import generate, render


HOME = Path("/var/lib/cairnival")


def test_linux_daemon_is_systemd_unit():
    (art,) = generate("linux", "daemon", "rustle", HOME, exe="/usr/local/bin/cairnival")
    assert art.path is not None and art.path.name == "cairnival-rustle.service"
    assert "ExecStart=/usr/local/bin/cairnival agent" in art.content
    assert f"Environment=CAIRNIVAL_HOME={HOME}" in art.content
    assert any("systemctl --user enable" in c for c in art.commands)


def test_linux_once_is_cron_line():
    (art,) = generate("linux", "once", "rustle", HOME, every_minutes=30, exe="/x/cairnival")
    assert art.path is None
    assert art.content.startswith("*/30 * * * *")
    assert "CAIRNIVAL_HOME=/var/lib/cairnival /x/cairnival once" in art.content

    # hour-scale intervals use the hour field — */90 is not valid cron
    (hourly,) = generate("linux", "once", "rustle", HOME, every_minutes=120, exe="/x/c")
    assert hourly.content.startswith("0 */2 * * *")


def test_darwin_modes():
    (daemon,) = generate("darwin", "daemon", "rustle", HOME, exe="/x/cairnival")
    assert "<key>KeepAlive</key>" in daemon.content
    assert "<string>agent</string>" in daemon.content
    assert daemon.path is not None and daemon.path.name == "com.cairnival.rustle.plist"

    (once,) = generate("darwin", "once", "rustle", HOME, every_minutes=15, exe="/x/cairnival")
    assert "<integer>900</integer>" in once.content
    assert "<string>once</string>" in once.content


def test_windows_modes_and_label_sanitizing():
    (daemon,) = generate("windows", "daemon", "rustle two!", HOME, exe="cairnival")
    assert "/SC ONLOGON" in daemon.content
    assert '"Cairnival-rustle-two-"' in daemon.content

    (once,) = generate("windows", "once", "rustle", HOME, every_minutes=45, exe="cairnival")
    assert "/SC MINUTE /MO 45" in once.content


def test_render_writes_only_when_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    arts = generate("linux", "daemon", "rustle", HOME, exe="/x/cairnival")
    text = render(arts, write=False)
    assert "use --write to create" in text
    assert not arts[0].path.exists()
    text = render(arts, write=True)
    assert "written to" in text
    assert arts[0].path.read_text().startswith("[Unit]")
