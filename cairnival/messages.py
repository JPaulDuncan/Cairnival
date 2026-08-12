"""Messages: the agent's mailbox across folders.

Every message — incoming or outgoing — is an ``Instruction`` markdown file, the
same shape the wake cycle reads. They live in folders under the agent's home:

    inbox/    incoming, not yet worked (unread)
    archive/  incoming, already worked (read)
    sent/     outgoing — messages and pings the agent sent
    spam/     quarantined: from senders the agent has blocked
    trash/    soft-deleted, restorable until emptied

This module is the folder view over those directories, the moves between them
(trash / restore / spam), the record of what was sent, and the grouping of
incoming + outgoing into per-agent conversation threads.
"""

from __future__ import annotations

from pathlib import Path

from .instructions import Instruction, drop
from .memory import Memory

FOLDERS = ("inbox", "sent", "archive", "spam", "trash")


def folder_dir(memory: Memory, folder: str) -> Path:
    return {
        "inbox": memory.inbox_dir,
        "archive": memory.archive_dir,
        "sent": memory.sent_dir,
        "spam": memory.spam_dir,
        "trash": memory.trash_dir,
    }[folder]


def _is_mail(msg: Instruction) -> bool:
    """Self-directed work lands in the archive too, but it isn't mail — keep it
    out of the mailbox (it belongs to the Log)."""
    return msg.source != "self"


def load(memory: Memory, folder: str) -> list[Instruction]:
    """Every message in a folder, newest first (self-directed work excluded)."""
    d = folder_dir(memory, folder)
    items: list[Instruction] = []
    if not d.exists():
        return items
    for path in d.glob("*.md"):
        try:
            msg = Instruction.from_markdown(path.read_text(encoding="utf-8"), path)
        except Exception:
            continue
        if _is_mail(msg):
            items.append(msg)
    items.sort(key=lambda i: i.received, reverse=True)
    return items


def counts(memory: Memory) -> dict[str, int]:
    return {f: len(load(memory, f)) for f in FOLDERS}


def direction(msg: Instruction) -> str:
    return "out" if str(msg.source).startswith("sent") else "in"


def peer_of(msg: Instruction) -> str:
    """The other party in the conversation: the recipient of an outgoing
    message, the sender of an incoming one."""
    return msg.to if direction(msg) == "out" else msg.sender


def find(memory: Memory, folder: str, name: str) -> Instruction | None:
    """Load one message by file name from a folder — basename only, no
    path traversal."""
    d = folder_dir(memory, folder)
    path = d / Path(name).name
    if path.exists() and path.parent == d:
        try:
            return Instruction.from_markdown(path.read_text(encoding="utf-8"), path)
        except Exception:
            return None
    return None


def move(path: Path | None, dest_dir: Path) -> Path | None:
    """Move a message file into another folder, keeping its name."""
    if not path or not path.exists():
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    path.rename(dest)
    return dest


def record_sent(
    memory: Memory,
    to_handle: str,
    text: str,
    *,
    kind: str = "message",
    status: str = "",
) -> Path:
    """File a copy of an outgoing message in the Sent folder, so the agent's
    own messages are part of its mailbox and its conversation threads."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    label = {"ping": "ping", "reply": "reply"}.get(kind, "message")
    title = f"→ {to_handle}"
    if status:
        title += f" ({status})"
    ins = Instruction(
        title=title,
        body=text,
        source="sent" if kind != "ping" else "sent:ping",
        sender=memory.agent_name,
        to=to_handle,
        reply_to="",
        priority=9,
    )
    # stash the kind in the body-less metadata via reply_to-free title; the
    # folder + source are enough to classify it
    return drop(memory.sent_dir, ins)


def threads(memory: Memory) -> list[dict]:
    """Incoming (inbox + read) and outgoing (sent) grouped into per-agent
    conversations, most-recently-active first. Spam and trash are excluded."""
    msgs: list[Instruction] = []
    for folder in ("inbox", "archive", "sent"):
        msgs.extend(load(memory, folder))
    by_peer: dict[str, list[Instruction]] = {}
    for m in msgs:
        peer = peer_of(m)
        if not peer:
            continue
        by_peer.setdefault(peer, []).append(m)
    convos: list[dict] = []
    for peer, items in by_peer.items():
        items.sort(key=lambda i: i.received)  # oldest → newest within a thread
        convos.append(
            {
                "peer": peer,
                "messages": items,
                "last": items[-1].received,
                "count": len(items),
            }
        )
    convos.sort(key=lambda c: c["last"], reverse=True)
    return convos


def sweep_sender_to_spam(memory: Memory, sender: str) -> int:
    """Move every pending inbox message from ``sender`` into Spam — used when a
    sender is blocked (by hand or by the abuse limiter). Returns how many
    moved."""
    moved = 0
    for m in load(memory, "inbox"):
        if m.sender == sender and m.path is not None:
            if move(m.path, memory.spam_dir):
                moved += 1
    return moved
