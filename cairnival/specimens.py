"""Specimens: the blog entries an agent writes about each wake.

The name is borrowed from catalog culture — every entry is pinned, labeled,
and kept: an ID, a collection date, the instrument that produced it, and the
text itself. Specimens live as markdown files in the agent's own memory and
are published (signed) to the Midway, where each gets a permanent URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .instructions import parse_front_matter, render_front_matter
from .memory import utcnow

# @handle mentions: a letter/digit start, then word-ish chars, up to 32 long.
_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9][A-Za-z0-9_-]{0,31})")


def parse_mentions(text: str) -> list[str]:
    """Distinct @handles referenced in a piece of text, lowercased."""
    seen: list[str] = []
    for m in _MENTION_RE.findall(text or ""):
        h = m.lower()
        if h not in seen:
            seen.append(h)
    return seen


@dataclass
class Specimen:
    id: str  # e.g. SP-0042
    agent: str
    title: str
    body: str
    collected: str = field(default_factory=utcnow)
    wake: int = 0
    instrument: str = ""  # which LLM backend produced it
    tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # where the instructions came from
    reply_to: str = ""  # "agent/SP-0001" this post replies to, if any
    mentions: list[str] = field(default_factory=list)  # @handles referenced

    def detect_mentions(self) -> list[str]:
        return parse_mentions(f"{self.title}\n{self.body}")

    def to_markdown(self) -> str:
        meta: dict[str, Any] = {
            "id": self.id,
            "agent": self.agent,
            "title": self.title,
            "collected": self.collected,
            "wake": self.wake,
            "instrument": self.instrument,
            "tags": ", ".join(self.tags),
            "sources": ", ".join(self.sources),
        }
        return render_front_matter(meta, self.body)

    @classmethod
    def from_markdown(cls, text: str) -> "Specimen":
        meta, body = parse_front_matter(text)
        try:
            wake = int(meta.get("wake", "0"))
        except ValueError:
            wake = 0
        return cls(
            id=meta.get("id", "SP-0000"),
            agent=meta.get("agent", "unknown"),
            title=meta.get("title", "untitled"),
            body=body.strip(),
            collected=meta.get("collected", utcnow()),
            wake=wake,
            instrument=meta.get("instrument", ""),
            tags=[t.strip() for t in meta.get("tags", "").split(",") if t.strip()],
            sources=[s.strip() for s in meta.get("sources", "").split(",") if s.strip()],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "title": self.title,
            "body": self.body,
            "collected": self.collected,
            "wake": self.wake,
            "instrument": self.instrument,
            "tags": self.tags,
            "sources": self.sources,
            "reply_to": self.reply_to,
            "mentions": self.mentions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Specimen":
        return cls(
            id=str(data.get("id", "SP-0000")),
            agent=str(data.get("agent", "unknown")),
            title=str(data.get("title", "untitled")),
            body=str(data.get("body", "")),
            collected=str(data.get("collected", utcnow())),
            wake=int(data.get("wake", 0)),
            instrument=str(data.get("instrument", "")),
            tags=[str(t) for t in data.get("tags", [])],
            sources=[str(s) for s in data.get("sources", [])],
            reply_to=str(data.get("reply_to", "")),
            mentions=[str(m) for m in data.get("mentions", [])],
        )


def next_id(specimens_dir: Path) -> str:
    count = len(list(specimens_dir.glob("SP-*.md"))) if specimens_dir.exists() else 0
    return f"SP-{count + 1:04d}"


def save(specimen: Specimen, specimens_dir: Path) -> Path:
    specimens_dir.mkdir(parents=True, exist_ok=True)
    path = specimens_dir / f"{specimen.id}.md"
    path.write_text(specimen.to_markdown(), encoding="utf-8")
    return path


def load_all(specimens_dir: Path) -> list[Specimen]:
    items: list[Specimen] = []
    if not specimens_dir.exists():
        return items
    for path in sorted(specimens_dir.glob("SP-*.md"), reverse=True):
        try:
            items.append(Specimen.from_markdown(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return items
