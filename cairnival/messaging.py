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

import httpx

from .federation import Identity, seal
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
) -> tuple[bool, str]:
    """Send a signed note to ``to_handle``. Returns (ok, how).

    ``how`` is one of: direct, relayed, held, undeliverable.
    """
    body: dict[str, object] = {"text": text}
    if reply:
        body["reply"] = True
    if in_reply_to:
        body["in_reply_to"] = in_reply_to
    if title:
        body["title"] = title
    env = seal(identity, "note", body)
    payload = env.to_dict()

    peers = memory.load_peers()
    peer = peers.get(to_handle, {})

    # 1. straight to the recipient if we know where it lives
    target_url = peer.get("public_url", "")
    if target_url:
        try:
            httpx.post(
                f"{target_url}/api/federation/inbox", json=payload, timeout=10
            ).raise_for_status()
            return True, "direct"
        except httpx.HTTPError:
            pass

    # 2. otherwise ask the Midway to relay or hold it
    if cfg.hub_url:
        try:
            resp = httpx.post(
                f"{cfg.hub_url}/api/mail/{to_handle}", json=payload, timeout=10
            )
            resp.raise_for_status()
            how = resp.json().get("delivery", "held")
            return True, how
        except (httpx.HTTPError, ValueError):
            pass

    return False, "undeliverable"


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
