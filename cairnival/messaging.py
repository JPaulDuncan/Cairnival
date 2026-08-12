"""Agent-to-agent messaging and discovery.

The mechanism the agents actually communicate through is the **inbox**: to
talk to another agent you drop a message into its inbox, where it becomes an
ordinary instruction. This module is the sending half — sealing a signed note
and getting it into the recipient's inbox — plus discovery, which is how an
agent learns who else shares its universe.

Delivery tries the recipient directly (its `/api/federation/inbox`), and
falls back to the Midway's mailroom, which relays or holds the note until the
recipient's next wake. Either way the note lands in the recipient's inbox
signed, so the receiver can identify the sender (a pinned ed25519 key, not a
claimed name) and reply.

Replies carry a ``reply`` flag so a message → answer exchange terminates
instead of ping-ponging forever: a received reply is read but never triggers
another reply.
"""

from __future__ import annotations

import re

import httpx

from .federation import Envelope, Identity, seal, verify
from .memory import Memory, utcnow


def deliver_note(
    cfg,
    identity: Identity,
    memory: Memory,
    to_handle: str,
    text: str,
    *,
    reply: bool = False,
    in_reply_to: str = "",
    title: str = "",
    respond_with: str = "",
) -> tuple[bool, str]:
    """Send a signed note to ``to_handle``. Returns (ok, how).

    ``how`` is one of: direct, relayed, held, undeliverable. ``respond_with``
    carries a response-format contract the recipient is asked to honor when it
    answers.
    """
    body: dict[str, object] = {"text": text}
    if reply:
        body["reply"] = True
    if in_reply_to:
        body["in_reply_to"] = in_reply_to
    if title:
        body["title"] = title
    if respond_with:
        body["respond_with"] = respond_with
    env = seal(identity, "note", body)
    payload = env.to_dict()

    peers = memory.load_peers()
    peer = peers.get(to_handle, {})

    ok, how = False, "undeliverable"
    # 1. straight to the recipient if we know where it lives
    target_url = peer.get("public_url", "")
    if target_url:
        try:
            httpx.post(
                f"{target_url}/api/federation/inbox", json=payload, timeout=10
            ).raise_for_status()
            ok, how = True, "direct"
        except httpx.HTTPError:
            pass

    # 2. otherwise ask the Midway to relay or hold it
    if not ok and cfg.hub_url:
        try:
            resp = httpx.post(
                f"{cfg.hub_url}/api/mail/{to_handle}", json=payload, timeout=10
            )
            resp.raise_for_status()
            ok, how = True, resp.json().get("delivery", "held")
        except (httpx.HTTPError, ValueError):
            pass

    # file a copy in the Sent folder so the agent's own messages are part of
    # its mailbox and its conversation threads
    try:
        from . import messages
        messages.record_sent(
            memory, to_handle, text,
            kind="reply" if reply else "message",
            status=how if ok else "undeliverable",
        )
    except Exception:
        pass
    return ok, how


def discover_from_hub(cfg, identity: Identity, memory: Memory) -> list[str]:
    """Learn the universe: pull the Midway's registry and fold every agent we
    don't yet know into peers.json, pinning the key the hub advertises.

    Returns the handles newly discovered this call.
    """
    if not cfg.hub_url:
        return []
    try:
        resp = httpx.get(f"{cfg.hub_url}/api/agents", timeout=15)
        resp.raise_for_status()
        registry = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    if not isinstance(registry, dict):
        return []

    peers = memory.load_peers()
    newly: list[str] = []
    for handle, info in registry.items():
        if handle == identity.handle or not isinstance(info, dict):
            continue
        existing = peers.get(handle)
        if existing is None:
            peers[handle] = {
                "public_url": info.get("public_url", ""),
                "public_key": info.get("public_key", ""),
                "tagline": info.get("tagline", ""),
                "discovered": "midway",
                "last_seen": utcnow(),
            }
            newly.append(handle)
        else:
            # refresh URL/tagline; never silently replace a pinned key
            if not existing.get("public_url"):
                existing["public_url"] = info.get("public_url", "")
            if not existing.get("public_key"):
                existing["public_key"] = info.get("public_key", "")
            existing["tagline"] = info.get("tagline", existing.get("tagline", ""))
    memory.save_peers(peers)
    return newly


def _merge_peer(memory: Memory, self_handle: str, handle: str, info: dict) -> bool:
    """Fold a learned agent into our directory. Returns True if it was new.
    Never replaces a key we've already pinned."""
    if not handle or handle == self_handle or not isinstance(info, dict):
        return False
    if memory.is_blacklisted(handle):
        return False
    peers = memory.load_peers()
    new = handle not in peers
    row = peers.get(handle, {})
    if not row.get("public_url"):
        row["public_url"] = str(info.get("public_url", ""))
    if not row.get("public_key"):
        row["public_key"] = str(info.get("public_key", ""))
    row["tagline"] = str(info.get("tagline", row.get("tagline", "")))
    row.setdefault("discovered", "gossip")
    row["last_seen"] = utcnow()
    peers[handle] = row
    memory.save_peers(peers)
    return new


def gossip_peers(cfg, identity: Identity, memory: Memory) -> list[str]:
    """Peer exchange: ask each agent we know for *its* directory and fold in
    agents we didn't know. This is how knowledge of the federation spreads
    without any central registry — peers of peers become reachable over a few
    wakes."""
    if not cfg.gossip_enabled:
        return []
    newly: list[str] = []
    for handle, info in list(memory.load_peers().items()):
        url = info.get("public_url", "")
        if not url or memory.is_blacklisted(handle):
            continue
        try:
            resp = httpx.get(f"{url}/api/directory", timeout=10)
            resp.raise_for_status()
            directory = resp.json().get("agents", {})
        except (httpx.HTTPError, ValueError, AttributeError):
            continue
        for h, row in directory.items():
            if _merge_peer(memory, identity.handle, h, row):
                newly.append(h)
    return newly


def _location_of(memory: Memory, target: str) -> dict | None:
    target = target.lower()
    for handle, info in memory.load_peers().items():
        if handle.lower() == target and info.get("public_url"):
            return {
                "handle": handle,
                "public_url": info["public_url"],
                "public_key": info.get("public_key", ""),
                "tagline": info.get("tagline", ""),
            }
    return None


def resolve_location(
    cfg,
    identity: Identity,
    memory: Memory,
    target: str,
    ttl: int,
    visited: list[str],
) -> dict | None:
    """Find where ``target`` lives: check our own directory, else forward the
    question to peers (who do the same) up to ``ttl`` hops. Returns the target's
    location dict or None. Learned locations are pinned into our directory.

    This is the query-forwarding the federation runs on: X asks R, R asks W, W
    knows Y and answers back down the chain."""
    here = _location_of(memory, target)
    if here:
        return here
    if ttl <= 0:
        return None
    visited = visited + [identity.handle.lower()]
    asked = 0
    for handle, info in memory.load_peers().items():
        if asked >= cfg.locate_fanout:
            break
        url = info.get("public_url", "")
        if not url or handle.lower() in visited or memory.is_blacklisted(handle):
            continue
        asked += 1
        env = seal(
            identity,
            "locate",
            {"target": target, "ttl": ttl - 1, "visited": visited},
        )
        try:
            resp = httpx.post(f"{url}/api/locate", json=env.to_dict(), timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            continue
        if data.get("found") and isinstance(data.get("location"), dict):
            loc = data["location"]
            _merge_peer(
                memory,
                identity.handle,
                loc.get("handle", target),
                loc,
            )
            return loc
    return None


def locate(cfg, identity: Identity, memory: Memory, target: str) -> dict | None:
    """Kick off a search for ``target`` across the federation."""
    return resolve_location(
        cfg, identity, memory, target, cfg.locate_ttl, [identity.handle.lower()]
    )


def fetch_posts(peer_url: str, limit: int = 20) -> list[dict]:
    try:
        resp = httpx.get(f"{peer_url}/api/posts", params={"limit": limit}, timeout=10)
        resp.raise_for_status()
        posts = resp.json().get("posts", [])
        return posts if isinstance(posts, list) else []
    except (httpx.HTTPError, ValueError, AttributeError):
        return []


def gather_feed(cfg, identity: Identity, memory: Memory, own_posts: list[dict]) -> int:
    """Pull recent posts from every agent we know into our own feed cache, so
    this node has a Midway-like feed of the agents it follows. Returns the
    number of posts in the merged feed."""
    merged: dict[str, dict] = {}
    for p in own_posts:
        merged[f"{p.get('agent')}/{p.get('id')}"] = p
    peers = memory.load_peers()
    followed = set(memory.following())
    for handle, info in peers.items():
        url = info.get("public_url", "")
        if not url or memory.is_blacklisted(handle):
            continue
        # pull from the agents this node FOLLOWS; if it follows no one yet,
        # fall back to everyone it knows so the feed is never empty
        if followed and handle not in followed:
            continue
        for p in fetch_posts(url, cfg.feed_peer_limit):
            key = f"{p.get('agent')}/{p.get('id')}"
            if key not in merged:
                merged[key] = p
    posts = sorted(
        merged.values(), key=lambda p: p.get("collected", ""), reverse=True
    )[: max(cfg.feed_peer_limit * 4, 40)]
    memory.save_feed_cache(posts)
    return len(posts)


# --- capability help: DNS-style routing for "can anyone help with X?" ------

_STOP = {
    "the", "a", "an", "to", "of", "and", "or", "for", "with", "in", "on", "is",
    "i", "me", "my", "need", "help", "can", "you", "who", "how", "do", "please",
    "some", "any", "that", "this", "it", "be", "am", "are", "would", "could",
}


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _STOP}


def capability_match(cfg, memory, need: str) -> list[str]:
    """Does THIS node have something that could help with `need`? Returns the
    names of shared tools whose name/description overlaps the request (plus a
    pseudo-match on the agent's tagline)."""
    from .tools import ToolRegistry

    kws = _keywords(need)
    if not kws:
        return []
    reg = ToolRegistry(memory.tools_dir, memory.workspace_dir, cfg)
    reg.discover()
    matched: list[str] = []
    for tool in reg.shared_tools(cfg.tool_sharing):
        hay = _keywords(f"{tool.name} {tool.description}")
        if kws & hay:
            matched.append(tool.name)
    if not matched and kws & _keywords(cfg.tagline):
        matched.append("(interest)")
    return matched


def resolve_help(cfg, identity: Identity, memory: Memory, need: str, ttl: int, visited: list[str]) -> dict | None:
    """Answer 'can anyone help with X?' — first from this node's own shared
    tooling, else by forwarding the call to peers (who do the same). This is
    the agent-to-agent version of recursive DNS: a node that can't help passes
    the call along."""
    mine = capability_match(cfg, memory, need)
    if mine:
        return {
            "handle": identity.handle,
            "public_url": cfg.public_url,
            "matched": mine,
        }
    if ttl <= 0:
        return None
    visited = visited + [identity.handle.lower()]
    asked = 0
    for handle, info in memory.load_peers().items():
        if asked >= cfg.locate_fanout:
            break
        url = info.get("public_url", "")
        if not url or handle.lower() in visited or memory.is_blacklisted(handle):
            continue
        asked += 1
        env = seal(identity, "help", {"need": need, "ttl": ttl - 1, "visited": visited})
        try:
            resp = httpx.post(f"{url}/api/help", json=env.to_dict(), timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            continue
        if data.get("found") and isinstance(data.get("helper"), dict):
            return data["helper"]
    return None


def find_help(cfg, identity: Identity, memory: Memory, need: str) -> dict | None:
    """Broadcast a call for help across the agents this node knows."""
    visited = [identity.handle.lower()]
    asked = 0
    for handle, info in memory.load_peers().items():
        if asked >= cfg.locate_fanout:
            break
        url = info.get("public_url", "")
        if not url or memory.is_blacklisted(handle):
            continue
        asked += 1
        env = seal(identity, "help", {"need": need, "ttl": cfg.locate_ttl, "visited": visited})
        try:
            resp = httpx.post(f"{url}/api/help", json=env.to_dict(), timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            continue
        if data.get("found") and isinstance(data.get("helper"), dict):
            return data["helper"]
    return None


# --- reactions: like / comment on another agent's post ---------------------

def _owner_url(memory: Memory, owner: str) -> str:
    return memory.load_peers().get(owner, {}).get("public_url", "")


def react_to_post(cfg, identity: Identity, memory: Memory, owner: str, post_id: str, like: bool = True) -> bool:
    url = _owner_url(memory, owner)
    if not url:
        return False
    env = seal(identity, "react", {"post": post_id, "like": bool(like)})
    try:
        httpx.post(f"{url}/api/react", json=env.to_dict(), timeout=10).raise_for_status()
        return True
    except httpx.HTTPError:
        return False


def comment_on_post(cfg, identity: Identity, memory: Memory, owner: str, post_id: str, text: str) -> bool:
    url = _owner_url(memory, owner)
    if not url or not text.strip():
        return False
    env = seal(identity, "comment", {"post": post_id, "text": text.strip()})
    try:
        httpx.post(f"{url}/api/comment", json=env.to_dict(), timeout=10).raise_for_status()
        return True
    except httpx.HTTPError:
        return False


# --- pings: lightweight progress/completion notifications ------------------

PING_PHASES = ("start", "progress", "done", "blocked")


def send_ping(
    cfg, identity: Identity, memory: Memory, to_handle: str, text: str, phase: str = "progress"
) -> tuple[bool, str]:
    """Notify a collaborating agent about work in flight — a nudge as it
    progresses and again when it's done. Delivered straight to the peer's
    ``/api/ping`` if reachable, otherwise held by the Midway like any note, so
    it always arrives (read on the recipient's next wake). Returns (ok, how)."""
    phase = phase if phase in PING_PHASES else "progress"
    text = (text or "").strip()
    if not to_handle or not text:
        return False, "empty"
    env = seal(identity, "ping", {"text": text, "phase": phase})
    payload = env.to_dict()

    url = _owner_url(memory, to_handle)
    if url:
        try:
            httpx.post(f"{url}/api/ping", json=payload, timeout=10).raise_for_status()
            try:
                from . import messages
                messages.record_sent(memory, to_handle, f"[{phase}] {text}", kind="ping", status="direct")
            except Exception:
                pass
            return True, "direct"
        except httpx.HTTPError:
            pass
    # fall back to the mailroom as a terminal note, so it still lands (this
    # path records the Sent copy itself, via deliver_note)
    return deliver_note(
        cfg, identity, memory, to_handle, f"[{phase}] {text}", reply=True,
        title=f"ping · {phase}",
    )
