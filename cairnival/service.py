"""Run an agent outside Docker: cron lines and native service definitions.

Two modes, on any of the three major platforms:

    daemon  the long-running process (`cairnival agent`): scheduler + attach UI
    once    a single wake per invocation (`cairnival once`), fired on a timer

Because every human-tunable setting lives in ``config.json`` inside the data
directory, a service or cron entry needs only two things: the ``cairnival``
executable and ``CAIRNIVAL_HOME``. This module generates the platform pieces:

    linux    systemd user unit (daemon)         · crontab line (once)
    darwin   launchd LaunchAgent plist (daemon or once, via StartInterval)
    windows  schtasks command (daemon at logon  · once every N minutes)

``cairnival service`` prints everything; ``--write`` also writes the unit or
plist file into place. Enabling/starting is always left to the human — the
exact commands are printed, never run.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Artifact:
    """One generated piece: an optional file plus the commands to use it."""

    description: str
    path: Path | None  # None = nothing to write, just commands
    content: str
    commands: list[str]


def find_executable() -> str:
    found = shutil.which("cairnival")
    if found:
        return found
    candidate = Path(sys.executable).parent / "cairnival"
    if candidate.exists():
        return str(candidate)
    return f"{sys.executable} -m cairnival"


def _label(agent_name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in agent_name)


def systemd_unit(agent_name: str, exe: str, home: Path) -> Artifact:
    label = _label(agent_name)
    unit = f"""[Unit]
Description=Cairnival agent: {agent_name}
After=network-online.target

[Service]
Environment=CAIRNIVAL_HOME={home}
ExecStart={exe} agent
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""
    path = Path.home() / ".config" / "systemd" / "user" / f"cairnival-{label}.service"
    return Artifact(
        description="systemd user unit (daemon)",
        path=path,
        content=unit,
        commands=[
            "systemctl --user daemon-reload",
            f"systemctl --user enable --now cairnival-{label}.service",
            f"journalctl --user -u cairnival-{label}.service -f",
        ],
    )


def cron_line(exe: str, home: Path, every_minutes: int) -> Artifact:
    # cron minute steps only span 0-59; hour-scale intervals need the hour field
    if every_minutes < 60:
        schedule = f"*/{every_minutes} * * * *"
        described = every_minutes
    else:
        hours = max(1, round(every_minutes / 60))
        schedule = f"0 */{hours} * * *"
        described = hours * 60
    line = (
        f"{schedule} CAIRNIVAL_HOME={home} {exe} once "
        f">> {home}/cron.log 2>&1"
    )
    return Artifact(
        description=f"crontab line (one wake every {described} min)",
        path=None,
        content=line,
        commands=["crontab -e   # then paste the line above"],
    )


def launchd_plist(
    agent_name: str, exe: str, home: Path, mode: str, every_minutes: int
) -> Artifact:
    label = f"com.cairnival.{_label(agent_name)}"
    exe_parts = exe.split()
    args = "\n".join(
        f"        <string>{part}</string>" for part in exe_parts + [
            "agent" if mode == "daemon" else "once"
        ]
    )
    schedule = (
        "    <key>KeepAlive</key>\n    <true/>"
        if mode == "daemon"
        else f"    <key>StartInterval</key>\n    <integer>{every_minutes * 60}</integer>"
    )
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CAIRNIVAL_HOME</key>
        <string>{home}</string>
    </dict>
{schedule}
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{home}/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>{home}/launchd.log</string>
</dict>
</plist>
"""
    path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    return Artifact(
        description=f"launchd LaunchAgent ({'daemon' if mode == 'daemon' else f'one wake every {every_minutes} min'})",
        path=path,
        content=plist,
        commands=[
            f"launchctl load {path}",
            f"launchctl start {label}",
        ],
    )


def schtasks_command(
    agent_name: str, exe: str, home: Path, mode: str, every_minutes: int
) -> Artifact:
    label = f"Cairnival-{_label(agent_name)}"
    run = f'cmd /c "set CAIRNIVAL_HOME={home}&& {exe} {"agent" if mode == "daemon" else "once"}"'
    if mode == "daemon":
        create = (
            f'schtasks /Create /TN "{label}" /SC ONLOGON /TR \'{run}\' /F'
        )
        desc = "Windows scheduled task (daemon at logon)"
    else:
        create = (
            f'schtasks /Create /TN "{label}" /SC MINUTE /MO {every_minutes} '
            f"/TR '{run}' /F"
        )
        desc = f"Windows scheduled task (one wake every {every_minutes} min)"
    return Artifact(
        description=desc,
        path=None,
        content=create,
        commands=[
            create,
            f'schtasks /Run /TN "{label}"      # start immediately',
            f'schtasks /Query /TN "{label}"',
        ],
    )


def generate(
    platform: str,
    mode: str,
    agent_name: str,
    home: Path,
    every_minutes: int = 60,
    exe: str | None = None,
) -> list[Artifact]:
    exe = exe or find_executable()
    home = Path(home).resolve()
    if platform == "linux":
        if mode == "daemon":
            return [systemd_unit(agent_name, exe, home)]
        return [cron_line(exe, home, every_minutes)]
    if platform == "darwin":
        return [launchd_plist(agent_name, exe, home, mode, every_minutes)]
    if platform == "windows":
        return [schtasks_command(agent_name, exe, home, mode, every_minutes)]
    raise ValueError(f"unknown platform: {platform}")


def render(artifacts: list[Artifact], write: bool) -> str:
    out: list[str] = []
    for art in artifacts:
        out.append(f"# {art.description}")
        if art.path:
            if write:
                art.path.parent.mkdir(parents=True, exist_ok=True)
                art.path.write_text(art.content, encoding="utf-8")
                out.append(f"# written to {art.path}")
            else:
                out.append(f"# target file (use --write to create): {art.path}")
        out.append("")
        out.append(art.content.rstrip())
        out.append("")
        out.append("# then:")
        out.extend(f"#   {cmd}" for cmd in art.commands)
        out.append("")
    return "\n".join(out)
