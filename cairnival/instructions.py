"""Instructions: the one shape every request funnels into.

Whether it arrives as a dropped file, a peer message, a paid memo on the treasury,
a webhook, a trusted peer's envelope, or the web UI's instruct form, it lands
in ``inbox/`` as a markdown file with front matter. The wake cycle only ever
reads files — which is also what makes the agent auditable: its whole inbox
is on disk.

Format:

    ---
    title: Answer the question about tides
    source: email
    from: someone@example.com
    reply_to: someone@example.com
    priority: 5
    paid: 0.02
    ---
    Why are there two high tides a day?
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory import utcnow

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Tiny YAML-subset front matter parser: `key: value` lines only."""
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip()
    return meta, text[match.end():]


def render_front_matter(meta: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.strip() + "\n"


@dataclass
class Instruction:
    title: str
    body: str
    source: str = "file"  # file | email | ui | federation | treasury | sent | connector name
    sender: str = ""
    reply_to: str = ""
    to: str = ""  # recipient handle — set on outgoing (source="sent") messages
    respond_with: str = ""  # a response-format contract the sender asked us to honor
    priority: int = 5  # 1 highest .. 9 lowest
    paid: float = 0.0
    received: str = field(default_factory=utcnow)
    path: Path | None = None  # set when loaded from disk

    @property
    def is_paid(self) -> bool:
        return self.paid > 0

    def to_markdown(self) -> str:
        meta: dict[str, Any] = {
            "title": self.title,
            "source": self.source,
            "from": self.sender,
            "to": self.to,
            "reply_to": self.reply_to,
            "respond_with": self.respond_with,
            "priority": self.priority,
            "received": self.received,
        }
        if self.paid:
            meta["paid"] = self.paid
        return render_front_matter(meta, self.body)

    @classmethod
    def from_markdown(cls, text: str, path: Path | None = None) -> "Instruction":
        meta, body = parse_front_matter(text)
        try:
            priority = int(meta.get("priority", "5"))
        except ValueError:
            priority = 5
        try:
            paid = float(meta.get("paid", "0"))
        except ValueError:
            paid = 0.0
        title = meta.get("title", "").strip()
        if not title:
            first = body.strip().splitlines()[0] if body.strip() else "untitled"
            title = first.lstrip("# ").strip()[:80] or "untitled"
        return cls(
            title=title,
            body=body.strip(),
            source=meta.get("source", "file"),
            sender=meta.get("from", ""),
            reply_to=meta.get("reply_to", ""),
            to=meta.get("to", ""),
            respond_with=meta.get("respond_with", ""),
            priority=priority,
            paid=paid,
            received=meta.get("received", utcnow()),
            path=path,
        )


def drop(inbox_dir: Path, instruction: Instruction) -> Path:
    """Write an instruction into the inbox with a unique, sortable name."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().replace(":", "").replace("-", "").replace("+0000", "Z")
    name = f"{stamp}-{secrets.token_hex(3)}.md"
    path = inbox_dir / name
    path.write_text(instruction.to_markdown(), encoding="utf-8")
    return path


def pending(inbox_dir: Path) -> list[Instruction]:
    """Load all pending instructions, paid first, then by priority, then age."""
    items: list[Instruction] = []
    if not inbox_dir.exists():
        return items
    for path in sorted(inbox_dir.glob("*.md")):
        try:
            items.append(
                Instruction.from_markdown(path.read_text(encoding="utf-8"), path)
            )
        except Exception:
            # A malformed file must never wedge the wake cycle.
            continue
    items.sort(key=lambda i: (-i.paid, i.priority, i.received))
    return items


def archive(instruction: Instruction, archive_dir: Path) -> None:
    if instruction.path and instruction.path.exists():
        archive_dir.mkdir(parents=True, exist_ok=True)
        instruction.path.rename(archive_dir / instruction.path.name)
