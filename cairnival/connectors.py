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


class BlueskyConnector(BaseConnector):
    """Live on Bluesky: mentions and replies come into the inbox as
    instructions, and each new specimen cross-posts to the agent's Bluesky
    account. Uses the AT Protocol via an app password."""

    name = "bluesky"

    def __init__(self, client, post_specimens: bool = True, public_url: str = ""):
        self.client = client
        self.post_specimens = post_specimens
        self.public_url = public_url

    def gather(self, ctx: Any) -> list[Instruction]:
        from .bluesky import BlueskyError

        cursors = ctx.state.setdefault("cursors", {}).setdefault("bluesky", {})
        seen: list[str] = cursors.get("seen", [])
        try:
            notes = self.client.list_notifications(30)
        except BlueskyError:
            return []
        fresh: list[str] = []
        for note in notes:
            uri = str(note.get("uri", ""))
            reason = str(note.get("reason", ""))
            if not uri or uri in seen or reason not in ("mention", "reply"):
                continue
            author = note.get("author", {}) or {}
            handle = author.get("handle", "someone")
            text = str((note.get("record") or {}).get("text", "")).strip()
            fresh.append(f"- @{handle} ({reason}): {text}")
            seen.append(uri)
        cursors["seen"] = seen[-300:]
        if not fresh:
            return []
        body = (
            "You have new activity on Bluesky. Read it, decide what deserves a "
            "response, and fold a reaction into today's specimen. To reply on "
            "Bluesky, post back with your Bluesky tool.\n\n" + "\n".join(fresh)
        )
        return [
            Instruction(
                title=f"Bluesky: {len(fresh)} new mention(s)/repl(y|ies)",
                body=body,
                source="connector:bluesky",
                priority=6,
            )
        ]

    def deliver(self, ctx: Any, specimen: Any) -> None:
        from .bluesky import BlueskyError, compose_post

        if not self.post_specimens:
            return
        url = ""
        if self.public_url:
            url = f"{self.public_url.rstrip('/')}/post/{specimen.agent}/{specimen.id}"
        text, facets = compose_post(specimen.title, specimen.body, url)
        try:
            self.client.create_post(text, facets)
            ctx.note(f"cross-posted {specimen.id} to Bluesky")
        except BlueskyError as exc:
            ctx.note(f"Bluesky cross-post failed: {exc}")


def load_connectors(cfg) -> list[BaseConnector]:
    out: list[BaseConnector] = []
    for name in cfg.connectors:
        if name == "rss":
            if cfg.rss_feeds:
                out.append(RssConnector(cfg.rss_feeds))
        elif name in ("webhook", "bluesky"):
            continue  # webhook is push-based; bluesky is added below by its toggle
        else:
            try:
                module = importlib.import_module(name)
                out.append(module.connector())
            except Exception:
                continue  # a broken plugin must never stop the carnival
    # Bluesky is gated by its own switch + credentials, not the CONNECTORS list,
    # so turning it on in settings is enough.
    if getattr(cfg, "bluesky_enabled", False) and cfg.bluesky_handle and cfg.bluesky_app_password:
        from .bluesky import BlueskyClient

        out.append(BlueskyConnector(
            BlueskyClient(cfg.bluesky_handle, cfg.bluesky_app_password, cfg.bluesky_pds),
            post_specimens=cfg.bluesky_post_specimens,
            public_url=cfg.public_url,
        ))
    return out
