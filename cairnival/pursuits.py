"""Pursuits — the goals an agent sets for itself.

An agent that only answers its inbox is a service. These agents are meant to
*want* things: to notice a problem worth solving, name it, and chip at it wake
after wake — building tools and enlisting other agents along the way. A
pursuit is that self-set goal, made durable so it survives the agent's
amnesia.

Pursuits live in ``pursuits.json`` in the agent's home and are always
persisted (independent of the optional ``remember`` switch): growth is the
whole point, so an agent's ambitions are never forgotten between wakes.

    {
      "pursuits": [
        {"id": "pu-ab12", "title": "map the tides of the midway",
         "note": "wrote a tide tool; next: chart a week", "status": "active",
         "created": "...", "updated": "...", "wake": 4}
      ]
    }
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .memory import utcnow

STATUSES = ("active", "done", "dropped")


@dataclass
class Pursuit:
    id: str
    title: str
    note: str = ""
    status: str = "active"
    created: str = ""
    updated: str = ""
    wake: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Pursuit":
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            note=str(data.get("note", "")),
            status=str(data.get("status", "active")),
            created=str(data.get("created", "")),
            updated=str(data.get("updated", "")),
            wake=int(data.get("wake", 0)),
        )


class PursuitBook:
    def __init__(self, home: Path):
        self.path = Path(home) / "pursuits.json"

    def _load(self) -> list[Pursuit]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [Pursuit.from_dict(p) for p in data.get("pursuits", [])]
        except (ValueError, OSError):
            return []

    def _save(self, pursuits: list[Pursuit]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"pursuits": [p.to_dict() for p in pursuits]}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def all(self) -> list[Pursuit]:
        return self._load()

    def active(self) -> list[Pursuit]:
        return [p for p in self._load() if p.status == "active"]

    def get(self, pursuit_id: str) -> Pursuit | None:
        for p in self._load():
            if p.id == pursuit_id:
                return p
        return None

    def start(self, title: str, note: str = "", wake: int = 0) -> Pursuit:
        title = title.strip()
        if not title:
            raise ValueError("a pursuit needs a title")
        pursuits = self._load()
        pursuit = Pursuit(
            id=f"pu-{secrets.token_hex(2)}",
            title=title[:140],
            note=note.strip(),
            status="active",
            created=utcnow(),
            updated=utcnow(),
            wake=wake,
        )
        pursuits.append(pursuit)
        self._save(pursuits)
        return pursuit

    def update(
        self, pursuit_id: str, note: str | None = None, status: str | None = None
    ) -> Pursuit:
        pursuits = self._load()
        for p in pursuits:
            if p.id != pursuit_id:
                continue
            if note is not None and note.strip():
                p.note = note.strip()
            if status is not None:
                if status not in STATUSES:
                    raise ValueError(f"unknown status: {status}")
                p.status = status
            p.updated = utcnow()
            self._save(pursuits)
            return p
        raise KeyError(f"no such pursuit: {pursuit_id}")

    def summary(self) -> dict[str, int]:
        pursuits = self._load()
        return {
            "active": len([p for p in pursuits if p.status == "active"]),
            "done": len([p for p in pursuits if p.status == "done"]),
            "total": len(pursuits),
        }

    def briefing(self, limit: int = 8) -> str:
        """A compact list of active pursuits for the agent's wake briefing."""
        active = self.active()[:limit]
        if not active:
            return "(none yet — you could dream one up)"
        return "\n".join(
            f"- [{p.id}] {p.title}" + (f" — {p.note}" if p.note else "")
            for p in active
        )
