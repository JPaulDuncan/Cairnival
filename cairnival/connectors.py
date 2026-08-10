"""Connectors: small plugins that feed the agent or carry its work outward.

A connector implements two optional hooks:

    gather(ctx)             -> list[Instruction]   run at the start of a wake
    deliver(ctx, specimen)  -> None                run after a specimen is written

``ctx`` is the WakeContext (config, memory, state cursors). Built-ins are
selected by name in the CONNECTORS env var (``rss``); anything else in the
list is imported as a dotted module path exposing a ``connector()`` factory,
so a deployment can drop in its own integrations without touching this
package. The webhook connector is wired in the agent's web app itself
(POST /api/hook) since it is push- rather than poll-based.
"""

from __future__ import annotations

import importlib
from typing import Any, Protocol

from .instructions import Instruction


class Connector(Protocol):
    name: str

    def gather(self, ctx: Any) -> list[Instruction]: ...

    def deliver(self, ctx: Any, specimen: Any) -> None: ...


class BaseConnector:
    name = "base"

    def gather(self, ctx: Any) -> list[Instruction]:
        return []

    def deliver(self, ctx: Any, specimen: Any) -> None:
        return None


class RssConnector(BaseConnector):
    """Watch RSS/Atom feeds; new entries become a digest instruction."""

    name = "rss"

    def __init__(self, feeds: list[str], max_items: int = 5):
        self.feeds = feeds
        self.max_items = max_items

    def gather(self, ctx: Any) -> list[Instruction]:
        import feedparser  # imported lazily; optional at runtime

        cursors = ctx.state.setdefault("cursors", {}).setdefault("rss", {})
        fresh: list[str] = []
        for url in self.feeds:
            try:
                feed = feedparser.parse(url)
            except Exception:
                continue
            seen: list[str] = cursors.get(url, [])
            for entry in feed.entries[: self.max_items]:
                eid = entry.get("id") or entry.get("link") or entry.get("title", "")
                if not eid or eid in seen:
                    continue
                title = entry.get("title", "(untitled)")
                link = entry.get("link", "")
                summary = entry.get("summary", "")[:500]
                fresh.append(f"- {title} — {link}\n  {summary}")
                seen.append(eid)
            cursors[url] = seen[-100:]
        if not fresh:
            return []
        body = (
            "New items appeared on the feeds you watch. Read the list, pick "
            "what actually matters, and fold a short reaction into today's "
            "specimen — a reader's note, not a rehash.\n\n" + "\n".join(fresh)
        )
        return [
            Instruction(
                title="React to fresh feed items",
                body=body,
                source="connector:rss",
                priority=7,
            )
        ]


def load_connectors(cfg) -> list[BaseConnector]:
    out: list[BaseConnector] = []
    for name in cfg.connectors:
        if name == "rss":
            if cfg.rss_feeds:
                out.append(RssConnector(cfg.rss_feeds))
        elif name == "webhook":
            continue  # handled by the web app, not the wake loop
        else:
            try:
                module = importlib.import_module(name)
                out.append(module.connector())
            except Exception:
                continue  # a broken plugin must never stop the carnival
    return out
