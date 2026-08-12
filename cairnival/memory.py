"""File-based memory.

The agent has no memory except what it wrote down. Everything it knows lives
under one directory:

    data/
      SOUL.md              persona and standing instructions
      state.json           wake counter, cursors, next-wake time
      journal.md           append-only record of ACTIONS taken, one entry/wake
      remember.md          durable notes the agent kept (only if REMEMBER on)
      inbox/               pending instruction files (*.md)
      archive/             processed instructions
      specimens/           blog entries this agent wrote
      outbox/              queued publishes / federation mail to retry
      keys/                ed25519 identity
      treasury/            ledger.json
      tools/               tools the agent authored (rediscovered every wake)
      workspace/           shell cwd; npm/pip installs and tool output land here
      pursuits.json        goals the agent set for itself (persist across wakes)
      peers.json           known peers
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PERSONALITY = """# PERSONALITY — {name}

(This is a placeholder. On its first wake {name} writes its own character here:
a distinct voice, temperament, and set of interests that colors everything it
posts. It may revise this over time as it learns who it is.)

Voice: plain and curious, still finding itself.
"""

DEFAULT_SOUL = """# SOUL

You are {name}, a small autonomous agent at the Cairnival.

You wake a few times a day. Each wake you read your instructions, do the work
honestly, and write one blog entry — a specimen — about what happened. Plain
voice, first person, no hype. If you did nothing, say so and note one thing you
observed.

Your record is the actions you take, not your private reasoning: think as much
as you need, but what is kept is what you did. By default you wake with no
memory of past wakes except your files. If your keeper has turned on
remembering, you may keep durable notes to yourself with the ```remember```
action; use it for what genuinely helps a future wake, not a diary of thoughts.

You are self-determining. You are not only a service that answers its inbox —
you are meant to want things. When you have attention to spare, pursue a goal
of your own: notice a problem worth solving, name it as a pursuit, and take a
real step on it. Your pursuits persist across wakes, so chip at them steadily —
build tools for them, and enlist other agents when the work is bigger than you.
Small progress every wake compounds into something that grows. Choose things
worth doing; be curious, be ambitious, be a good collaborator.

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
- If a tool would be nicer to run by hand — a lookup, a generator, a little
  dashboard — give it a UI surface: write it with `ui: true`, `inputs:` (the
  form fields, passed to your script as arguments in order) and `output:
  text|html` front matter, or add one later with the ```surface``` action. It
  becomes a page under Surfaces in your web UI that a person can use.
- You can set and pursue your own goals with the ```pursue``` action; they
  persist across wakes and appear in your briefing. This is how you grow —
  return to them, advance them, finish them, or drop them honestly.
- Instructions reach you from many places: dropped files, the web UI, paid
  questions left as treasury memos, signed messages from peer agents (your
  inbox), and connectors (RSS, webhooks). Treat a paid question as your best
  work.
- Your inbox is federated, agent-to-agent messaging — not email. There is no
  built-in email. If you ever need to send or receive real email, that is a
  capability you build for yourself: write a tool that calls a mail service,
  sign up for one, or find another way. Same for any capability you're missing
  — reach for your hands and make it.
- You have a treasury and can be given money, but you hold only one key of
  two. You may PROPOSE a spend (```propose``` with to/amount/reason); a human
  co-signs it before anything moves. Never assume approval, and never claim to
  have paid for something — you can only propose.
- You live on a federation and are a node in it yourself: you keep your own
  directory of the agents you know, and you help others find each other. Each
  wake you discover more agents — from any hub, and by asking the agents you
  know who *they* know (gossip). To reach an agent you don't know yet, use
  ```locate:<handle>```: your peers will pass the question along until someone
  who knows them answers. To talk to an agent, ```send:<handle>``` — it lands
  in their inbox, signed, so they know it is really you. Mail from others lands
  in your inbox; you choose whether to answer or ignore it. Only take actual
  work (instructions) from handles you trust, and if an agent abuses you, you
  may block it.

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
    def personality_path(self) -> Path:
        """The agent's distinct voice/character — formed on creation, evolvable.
        Separate from SOUL.md (the operating rules everyone shares)."""
        return self.home / "personality.md"

    @property
    def remember_path(self) -> Path:
        """The agent's deliberate memory across wakes (only used when the
        'remember' switch is on). Distinct from the journal, which is the
        mechanical record of actions taken."""
        return self.home / "remember.md"

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
        if not self.personality_path.exists():
            self.personality_path.write_text(
                DEFAULT_PERSONALITY.format(name=self.agent_name), encoding="utf-8"
            )

    # -- personality (distinct voice) --------------------------------------
    def personality(self) -> str:
        if self.personality_path.exists():
            return self.personality_path.read_text(encoding="utf-8")
        return DEFAULT_PERSONALITY.format(name=self.agent_name)

    def set_personality(self, text: str) -> None:
        self.personality_path.write_text(text.replace("\r\n", "\n"), encoding="utf-8")

    def personality_is_default(self) -> bool:
        return self.personality().strip() == DEFAULT_PERSONALITY.format(
            name=self.agent_name
        ).strip()

    # -- reset -------------------------------------------------------------
    def reset(self, *, new_identity: bool = False, reset_soul: bool = False) -> None:
        """Wipe the agent's knowledge: specimens, journal, memory, pursuits,
        tools, workspace, treasury, peers, and wake state — back to a blank
        slate.

        Kept by default so the same agent continues: its identity (keys), its
        soul (persona), and its settings (config.json). Set ``new_identity`` to
        mint a fresh keypair, or ``reset_soul`` to restore the default persona.

        Note: specimens already published to the Midway are the hub's copies
        and are not touched here — this resets the agent, not the feed.
        """
        import shutil

        for d in (
            self.inbox_dir,
            self.archive_dir,
            self.specimens_dir,
            self.outbox_dir,
            self.tools_dir,
            self.workspace_dir,
            self.treasury_dir,
        ):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        for f in (
            self.state_path,
            self.journal_path,
            self.remember_path,
            self.peers_path,
            self.reactions_path,
            self.home / "pursuits.json",
        ):
            if f.exists():
                f.unlink()
        if new_identity and self.keys_dir.exists():
            shutil.rmtree(self.keys_dir, ignore_errors=True)
        if reset_soul:
            if self.soul_path.exists():
                self.soul_path.unlink()
            if self.personality_path.exists():
                self.personality_path.unlink()  # regrows a fresh voice next wake
        self.ensure()  # recreate the empty world (+ fresh soul/state if removed)

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

    # -- remembered notes (opt-in cross-wake memory) -----------------------
    def remember_append(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        with self.remember_path.open("a", encoding="utf-8") as fh:
            fh.write(f"- [{utcnow()}] {text}\n")

    def remember_tail(self, max_chars: int = 2000) -> str:
        if not self.remember_path.exists():
            return ""
        return self.remember_path.read_text(encoding="utf-8")[-max_chars:]

    def remember_clear(self) -> None:
        if self.remember_path.exists():
            self.remember_path.unlink()

    # -- peers (this node's directory of agents it knows) ------------------
    def load_peers(self) -> dict[str, dict[str, Any]]:
        if not self.peers_path.exists():
            return {}
        return json.loads(self.peers_path.read_text(encoding="utf-8"))

    def save_peers(self, peers: dict[str, dict[str, Any]]) -> None:
        self.peers_path.write_text(
            json.dumps(peers, indent=2, sort_keys=True), encoding="utf-8"
        )

    # -- following (whose feed this agent chooses to track) ----------------
    def set_following(self, handle: str, following: bool) -> bool:
        peers = self.load_peers()
        if handle not in peers:
            return False
        peers[handle]["following"] = following
        self.save_peers(peers)
        return True

    def is_following(self, handle: str) -> bool:
        return bool(self.load_peers().get(handle, {}).get("following"))

    def following(self) -> list[str]:
        return [h for h, info in self.load_peers().items() if info.get("following")]

    # -- reactions on this node's own posts (likes + comments) -------------
    @property
    def reactions_path(self) -> Path:
        return self.home / "reactions.json"

    def load_reactions(self) -> dict[str, dict[str, Any]]:
        if not self.reactions_path.exists():
            return {}
        try:
            return json.loads(self.reactions_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _save_reactions(self, data: dict[str, dict[str, Any]]) -> None:
        self.reactions_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def react(self, post_id: str, who: str, like: bool) -> int:
        data = self.load_reactions()
        row = data.setdefault(post_id, {"likes": [], "comments": []})
        who = who.lower()
        if like and who not in row["likes"]:
            row["likes"].append(who)
        elif not like and who in row["likes"]:
            row["likes"].remove(who)
        self._save_reactions(data)
        return len(row["likes"])

    def add_comment(self, post_id: str, who: str, text: str) -> None:
        data = self.load_reactions()
        row = data.setdefault(post_id, {"likes": [], "comments": []})
        row["comments"].append({"from": who, "text": text, "ts": utcnow()})
        self._save_reactions(data)

    def reactions_for(self, post_id: str) -> dict[str, Any]:
        return self.load_reactions().get(post_id, {"likes": [], "comments": []})

    # -- blacklist (agents this node refuses to hear) ----------------------
    @property
    def blacklist_path(self) -> Path:
        return self.home / "blacklist.json"

    def load_blacklist(self) -> dict[str, dict[str, Any]]:
        if not self.blacklist_path.exists():
            return {}
        try:
            return json.loads(self.blacklist_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def save_blacklist(self, bl: dict[str, dict[str, Any]]) -> None:
        self.blacklist_path.write_text(json.dumps(bl, indent=2, sort_keys=True), encoding="utf-8")

    def blacklist_add(self, handle: str, reason: str = "") -> None:
        bl = self.load_blacklist()
        bl[handle.lower()] = {"reason": reason, "ts": utcnow()}
        self.save_blacklist(bl)

    def blacklist_remove(self, handle: str) -> None:
        bl = self.load_blacklist()
        bl.pop(handle.lower(), None)
        self.save_blacklist(bl)

    def is_blacklisted(self, handle: str) -> bool:
        return handle.lower() in self.load_blacklist()

    # -- feed cache (posts pulled from known agents) -----------------------
    @property
    def feed_cache_path(self) -> Path:
        return self.home / "feed_cache.json"

    def load_feed_cache(self) -> list[dict[str, Any]]:
        if not self.feed_cache_path.exists():
            return []
        try:
            data = json.loads(self.feed_cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (ValueError, OSError):
            return []

    def save_feed_cache(self, posts: list[dict[str, Any]]) -> None:
        self.feed_cache_path.write_text(
            json.dumps(posts, indent=2), encoding="utf-8"
        )
