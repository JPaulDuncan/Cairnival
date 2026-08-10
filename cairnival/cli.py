"""Command line entry points.

    cairnival agent    run an agent: wake scheduler + attach UI (headless-friendly)
    cairnival hub      run the Midway hub
    cairnival once     perform exactly one wake and exit (cron-style operation)
    cairnival status   print the agent's status from its files
    cairnival service  generate cron/systemd/launchd/schtasks pieces to run
                       the agent outside Docker on any OS

Configuration: environment variables first, then the UI-editable
``config.json`` in CAIRNIVAL_HOME — so `once` fired from cron behaves exactly
like the daemon, and settings changed in the browser follow the data
directory wherever it goes.
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import AgentConfig, HubConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cairnival", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("agent", help="run the agent (scheduler + web UI)")
    sub.add_parser("hub", help="run the Midway hub")
    sub.add_parser("once", help="one wake, then exit (for cron)")
    sub.add_parser("status", help="print agent status from its files")

    svc = sub.add_parser(
        "service", help="generate cron/systemd/launchd/schtasks to run without Docker"
    )
    svc.add_argument(
        "--mode",
        choices=("daemon", "once"),
        default="daemon",
        help="daemon = long-running agent; once = one wake per firing (default: daemon)",
    )
    svc.add_argument(
        "--platform",
        choices=("linux", "darwin", "windows"),
        default=None,
        help="target platform (default: this machine)",
    )
    svc.add_argument(
        "--every",
        type=int,
        default=60,
        metavar="MINUTES",
        help="firing interval for --mode once (default: 60)",
    )
    svc.add_argument(
        "--write",
        action="store_true",
        help="also write the unit/plist file into place (never enables/starts it)",
    )

    args = parser.parse_args(argv)

    if args.command == "agent":
        import uvicorn

        from .agent_app import create_app
        from .settings import load_agent_config

        cfg = load_agent_config()
        uvicorn.run(create_app(cfg), host=cfg.ui_host, port=cfg.ui_port)
        return 0

    if args.command == "hub":
        import uvicorn

        from .hub_app import create_app

        cfg = HubConfig.from_env()
        uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)
        return 0

    if args.command == "once":
        from .settings import load_agent_config
        from .wake import run_wake

        report = run_wake(load_agent_config())
        for line in report.log:
            print(f"  {line}")
        if report.specimen:
            print(f"specimen: {report.specimen.id} — {report.specimen.title}")
        return 0

    if args.command == "status":
        from .memory import Memory
        from .settings import load_agent_config
        from .treasury import Ledger

        cfg = load_agent_config()
        memory = Memory(cfg.home, cfg.name)
        state = memory.load_state()
        print(
            json.dumps(
                {
                    "agent": cfg.name,
                    "wakes": state.get("wakes", 0),
                    "last_wake": state.get("last_wake", ""),
                    "inbox": len(list(memory.inbox_dir.glob("*.md")))
                    if memory.inbox_dir.exists()
                    else 0,
                    "specimens": len(list(memory.specimens_dir.glob("SP-*.md")))
                    if memory.specimens_dir.exists()
                    else 0,
                    "treasury": Ledger(memory.treasury_dir).summary(),
                },
                indent=2,
            )
        )
        return 0

    if args.command == "service":
        from .service import generate, render
        from .settings import load_agent_config

        cfg = load_agent_config()
        platform = args.platform
        if platform is None:
            platform = {
                "linux": "linux",
                "darwin": "darwin",
                "win32": "windows",
            }.get(sys.platform, "linux")
        artifacts = generate(
            platform=platform,
            mode=args.mode,
            agent_name=cfg.name,
            home=cfg.home,
            every_minutes=max(1, args.every),
        )
        print(render(artifacts, write=args.write))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
