"""Bluesky / AT Protocol — a small XRPC client.

Enough of the protocol for an agent to live on Bluesky the way it lives on the
Midway: sign in with an app password, post (its specimens cross-post here), and
read its notifications (mentions and replies come back into its inbox as
instructions). Plain HTTP+JSON against a PDS (``https://bsky.social`` by
default).

Only app passwords are used — never a real account password — and they are
stored like every other secret (blanked in the settings UI, kept out of the
record). This client does the minimum: ``createSession`` for auth,
``createRecord`` to post, ``listNotifications`` to read. It is deliberately
small; an agent that wants more of the protocol can build a tool for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from .memory import utcnow

# Bluesky counts post length in graphemes and caps it at 300; we approximate
# with characters, which is safe (a grapheme is >= one character).
POST_LIMIT = 300


class BlueskyError(RuntimeError):
    pass


def link_facets(text: str, url: str) -> list[dict]:
    """A richtext facet making ``url`` (as it appears at the end of ``text``) a
    real clickable link — Bluesky needs byte offsets, not just a bare URL."""
    idx = text.rfind(url)
    if idx < 0:
        return []
    byte_start = len(text[:idx].encode("utf-8"))
    byte_end = byte_start + len(url.encode("utf-8"))
    return [{
        "index": {"byteStart": byte_start, "byteEnd": byte_end},
        "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
    }]


def compose_post(title: str, body: str, url: str = "") -> tuple[str, list[dict]]:
    """Turn a specimen into a single Bluesky post: title, a trimmed excerpt, and
    an optional link — kept within the 300-grapheme limit. Returns (text,
    facets)."""
    title = " ".join((title or "").split())
    excerpt = " ".join((body or "").split())
    tail = f"\n\n{url}" if url else ""
    budget = POST_LIMIT - len(tail)
    head = title
    if excerpt:
        room = budget - len(title) - 2
        if room > 20:
            if len(excerpt) > room:
                excerpt = excerpt[: room - 1].rstrip() + "…"
            head = f"{title}\n\n{excerpt}" if title else excerpt
    if len(head) > budget:
        head = head[: budget - 1].rstrip() + "…"
    text = head + tail
    return text, (link_facets(text, url) if url else [])


@dataclass
class BlueskyClient:
    handle: str
    app_password: str
    pds: str = "https://bsky.social"
    timeout: int = 15
    _did: str = field(default="", repr=False)
    _jwt: str = field(default="", repr=False)

    def _url(self, method: str) -> str:
        return f"{self.pds.rstrip('/')}/xrpc/{method}"

    def login(self) -> None:
        if self._jwt:
            return
        if not self.handle or not self.app_password:
            raise BlueskyError("Bluesky needs a handle and an app password")
        try:
            resp = httpx.post(
                self._url("com.atproto.server.createSession"),
                json={"identifier": self.handle, "password": self.app_password},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BlueskyError(f"Bluesky login failed: {exc}") from exc
        self._did = str(data.get("did", ""))
        self._jwt = str(data.get("accessJwt", ""))
        if not self._did or not self._jwt:
            raise BlueskyError("Bluesky login returned no session")

    @property
    def did(self) -> str:
        return self._did

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._jwt}"}

    def create_post(self, text: str, facets: list[dict] | None = None) -> dict:
        """Publish a post; returns {uri, cid}."""
        self.login()
        record: dict = {
            "$type": "app.bsky.feed.post",
            "text": text[:POST_LIMIT],
            "createdAt": utcnow(),
            "langs": ["en"],
        }
        if facets:
            record["facets"] = facets
        try:
            resp = httpx.post(
                self._url("com.atproto.repo.createRecord"),
                headers=self._auth(),
                json={"repo": self._did, "collection": "app.bsky.feed.post", "record": record},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BlueskyError(f"Bluesky post failed: {exc}") from exc

    def list_notifications(self, limit: int = 30) -> list[dict]:
        """Recent notifications (mentions, replies, likes, follows, …)."""
        self.login()
        try:
            resp = httpx.get(
                self._url("app.bsky.notification.listNotifications"),
                headers=self._auth(),
                params={"limit": max(1, min(limit, 100))},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return list(resp.json().get("notifications", []))
        except (httpx.HTTPError, ValueError) as exc:
            raise BlueskyError(f"Bluesky notifications failed: {exc}") from exc
