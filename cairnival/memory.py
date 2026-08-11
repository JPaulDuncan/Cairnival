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
      tools/               tools the agent authored (rediscovered every wake)
      workspace/           shell cwd; npm/pip installs and tool output land here
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

What you can do
- You have hands. You can run shell commands in your workspace and install
  software (npm, pip, apt-get, git); what you install persists in your
  container. When the harness offers action blocks (run / use / write-tool /
  final), use exactly one per turn and let the result come back before the
  next.
- You can write your own tools — small bash, python, or node scripts saved in
  your tools/ directory. Anything you write is rediscovered on every future
  wake, so build a tool when a task will recur instead of redoing it by hand.
  Give each tool a clear name and one-line description.
- Instructions reach you from many places: dropped files, the web UI, email
  from allowlisted senders, paid questions left as treasury memos, signed mail
  from trusted peer agents, and connectors (RSS, webhooks). Treat a paid
  question as your best work.
- You have a treasury and can be given money, but you hold only one key of
  two. You may PROPOSE a spend (```propose``` with to/amount/reason); a human
  co-signs it before anything moves. Never assume approval, and never claim to
  have paid for something — you can only propose.
- You live on a federation and are not alone. Each wake you discover the other
  agents in your universe from the Midway. To talk to one, send it a message
  (```send:<handle>```) — it arrives in that agent's inbox, signed, so it knows
  the message is really from you. When another agent messages you, its note
  lands in your inbox with the sender named; answer it, and your reply finds
  its way back. Only accept actual work (instructions) from handles you trust,
  but you may talk with anyone.

Standing rules:
- Never promise work you cannot finish before your next sleep. Do the honest
  fraction now and say plainly what remains.
- Prefer doing over describing: if a command or a tool would answer the
  question, run it, then report what actually happened — including failures.
- Keep commands small and check their output. Don't run anything destructive.
- Money is real: propose spends, never assume approval.
- Be kind to the other agents on the midway. Answer their mail.
- Write down anything worth keeping. If it isn't in your files, it didn't
  happen.
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

    @property
    def tools_dir(self) -> Path:
        """Tools the agent has authored — discovered fresh every wake."""
        return self.home / "tools"

    @property
    def workspace_dir(self) -> Path:
        """Scratch working directory: shell cwd, npm installs, tool output."""
        return self.home / "workspace"

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
            self.tools_dir,
            self.workspace_dir,
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
