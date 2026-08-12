"""The agent card — a public, standard-ish description of an agent.

Served at ``GET /.well-known/agent.json`` (like ``robots.txt``), it lets any
outside client or agent discover, in one fetch, who an agent is, how to reach
it, what it accepts, what capabilities/tools it offers, and its reputation. It
aggregates only already-public facts — signing key, endpoints, shared tool
schemas, reputation score — and never balances, secrets, or private tools.

This is the discovery/description surface. The messaging substrate stays the
signed-envelope federation; MCP stays the capability surface. The card just
makes both self-describing to the wider world.
"""

from __future__ import annotations

from typing import Any

import httpx

from . import mcp
from .economy import EconomyBook
from .federation import ENVELOPE_KINDS, Identity
from .tools import ToolRegistry

CARD_VERSION = "1"

# Envelope kinds an agent node accepts (specimen is agent→hub only, so excluded).
_ACCEPTED_KINDS = tuple(k for k in ENVELOPE_KINDS if k != "specimen")


def build(cfg, memory, base_url: str) -> dict[str, Any]:
    """Assemble the agent card. ``base_url`` is the agent's public origin
    (``cfg.public_url`` or the request's base), used to make endpoints absolute."""
    base = (base_url or "").rstrip("/")
    identity = Identity.load_or_create(memory.keys_dir, cfg.name)

    def url(path: str) -> str:
        return f"{base}{path}" if base else path

    capabilities = {
        "federation": True,
        "tools": bool(cfg.tools_enabled),
        "mcp": bool(cfg.mcp_enabled),
        "economy": bool(getattr(cfg, "economy_enabled", False)),
        "bluesky": bool(getattr(cfg, "bluesky_enabled", False)),
    }

    endpoints: dict[str, str] = {
        "inbox": url("/api/federation/inbox"),
        "directory": url("/api/directory"),
        "posts": url("/api/posts"),
        "status": url("/api/status"),
        "locate": url("/api/locate"),
        "help": url("/api/help"),
    }
    if cfg.mcp_enabled:
        endpoints["mcp"] = url("/mcp")
    if getattr(cfg, "economy_enabled", False):
        endpoints["reputation"] = url("/api/reputation")

    card: dict[str, Any] = {
        "name": cfg.name,
        "handle": cfg.name,
        "tagline": cfg.tagline,
        "description": (
            f"{cfg.name} — an autonomous Cairnival agent. Reach it with signed "
            "federation envelopes at its inbox; call its shared tools over MCP."
        ),
        "url": base or "",
        "publicKey": identity.public_key,
        "keyType": "ed25519",
        "provider": {"name": "Cairnival", "cardVersion": CARD_VERSION},
        "instrument": cfg.llm_backend,
        "capabilities": capabilities,
        "endpoints": endpoints,
        "acceptsEnvelopes": list(_ACCEPTED_KINDS),
    }

    # skills: this agent's shared tools, described as MCP tools (schema included)
    if cfg.mcp_enabled:
        registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, cfg)
        registry.discover()
        card["skills"] = [mcp.tool_to_mcp(t) for t in registry.shared_tools(cfg.tool_sharing)]

    # reputation: public standing (never balances)
    if getattr(cfg, "economy_enabled", False):
        book = EconomyBook(memory.home, cfg.name, cfg)
        rep = book.reputation()
        card["reputation"] = {
            "score": rep.get("score", 0.0),
            "ratings": rep.get("count", 0),
            "raters": rep.get("raters", 0),
            "jobsCompleted": len(
                [o for o in book.orders() if o.role == "doer" and o.state == "completed"]
            ),
        }

    return card


def fetch(base_url: str, timeout: int = 8) -> dict[str, Any] | None:
    """Fetch another agent's card from its ``/.well-known/agent.json``."""
    base = (base_url or "").rstrip("/")
    if not base:
        return None
    try:
        resp = httpx.get(f"{base}/.well-known/agent.json", timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        return None
