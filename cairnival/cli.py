"""Command line entry points.

    cairnival agent   run an agent: wake scheduler + attach UI (headless-friendly)
    cairnival hub     run the Midway hub
    cairnival once    perform exactly one wake and exit (cron-style operation)
    cairnival status  print the agent's status from its files
"""

from __future__ import annotations

import argparse
import json
import sys

import uvicorn

from .config import AgentConfig, HubConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cairnival", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("agent", help="run the agent (scheduler + web UI)")
    sub.add_parser("hub", help="run the Midway hub")
    sub.add_parser("once", help="one wake, then exit (for cron)")
    sub.add_parser("status", help="print agent status from its files")
    args = parser.parse_args(argv)

    if args.command == "agent":
        from .agent_app import create_app

        cfg = AgentConfig.from_env()
        uvicorn.run(create_app(cfg), host=cfg.ui_host, port=cfg.ui_port)
        return 0

    if args.command == "hub":
        from .hub_app import create_app

        cfg = HubConfig.from_env()
        uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)
        return 0

    if args.command == "once":
        from .wake import run_wake

        report = run_wake(AgentConfig.from_env())
        for line in report.log:
            print(f"  {line}")
        if report.specimen:
            print(f"specimen: {report.specimen.id} — {report.specimen.title}")
        return 0

    if args.command == "status":
        from .memory import Memory
        from .treasury import Ledger

        cfg = AgentConfig.from_env()
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

    return 1


if __name__ == "__main__":
    sys.exit(main())
