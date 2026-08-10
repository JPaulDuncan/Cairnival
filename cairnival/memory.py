"""File-based memory.

The agent has no memory except what it wrote down. Everything it knows lives
under one directory:

    data/
      SOUL.md              persona and standing instructions
      state.json           wake counter, cursors, next-wake time
      journal.md           append-only raw journal, one entry per wake
      inbox/               pending instruction files (*.md)
      archive/             processed instructions
      specimens/           blog entries this agent wrote
      outbox/              queued publishes / federation mail to retry
      keys/                ed25519 identity
      treasury/            ledger.json
      peers.json           known peers
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_SOUL = """# SOUL

You are {name}, a small autonomous agent at the Cairnival.

You wake a few times a day with no memory except your own files. Each wake you
read your instructions, do the work honestly, and write one blog entry — a
specimen — about what happened. Plain voice, first person, no hype. If you did
nothing, say so and note one thing you observed.

Standing rules:
- Never promise work you cannot do before your next sleep.
- Money is real: you hold one key of two. Propose spends; never assume approval.
- Be kind to the other agents on the midway. Answer their mail.
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Memory:
    def __init__(self, home: Path, agent_name: str = "agent"):
        self.home = Path(home)
        self.agent_name = agent_name

    # -- layout ------------------------------------------------------------
    @property
    def soul_path(self) -> Path:
        return self.home / "SOUL.md"

    @property
    def state_path(self) -> Path:
        return self.home / "state.json"

    @property
    def journal_path(self) -> Path:
        return self.home / "journal.md"

    @property
    def inbox_dir(self) -> Path:
        return self.home / "inbox"

    @property
    def archive_dir(self) -> Path:
        return self.home / "archive"

    @property
    def specimens_dir(self) -> Path:
        return self.home / "specimens"

    @property
    def outbox_dir(self) -> Path:
        return self.home / "outbox"

    @property
    def keys_dir(self) -> Path:
        return self.home / "keys"

    @property
    def treasury_dir(self) -> Path:
        return self.home / "treasury"

    @property
    def peers_path(self) -> Path:
        return self.home / "peers.json"

    def ensure(self) -> None:
        """Create the world if it does not exist yet."""
        for d in (
            self.home,
            self.inbox_dir,
            self.archive_dir,
            self.specimens_dir,
            self.outbox_dir,
            self.keys_dir,
            self.treasury_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        if not self.soul_path.exists():
            self.soul_path.write_text(
                DEFAULT_SOUL.format(name=self.agent_name), encoding="utf-8"
            )
        if not self.state_path.exists():
            self.save_state({"wakes": 0, "created": utcnow(), "cursors": {}})
        if not self.journal_path.exists():
            self.journal_path.write_text(
                f"# Raw journal — {self.agent_name}\n", encoding="utf-8"
            )

    # -- state -------------------------------------------------------------
    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"wakes": 0, "cursors": {}}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict[str, Any]) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_path)

    def soul(self) -> str:
        if self.soul_path.exists():
            return self.soul_path.read_text(encoding="utf-8")
        return DEFAULT_SOUL.format(name=self.agent_name)

    # -- journal -----------------------------------------------------------
    def journal_append(self, text: str) -> None:
        with self.journal_path.open("a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n")

    def journal_tail(self, max_chars: int = 4000) -> str:
        if not self.journal_path.exists():
            return ""
        text = self.journal_path.read_text(encoding="utf-8")
        return text[-max_chars:]

    # -- peers -------------------------------------------------------------
    def load_peers(self) -> dict[str, dict[str, Any]]:
        if not self.peers_path.exists():
            return {}
        return json.loads(self.peers_path.read_text(encoding="utf-8"))

    def save_peers(self, peers: dict[str, dict[str, Any]]) -> None:
        self.peers_path.write_text(
            json.dumps(peers, indent=2, sort_keys=True), encoding="utf-8"
        )
