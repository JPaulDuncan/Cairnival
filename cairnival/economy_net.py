"""The coin economy over the wire: the signed work-order protocol.

The escrow is a 2-of-2 held by the two parties' keys. Each step is a signed
envelope, and the signature on the doer's ``work_accept`` and the asker's
``work_release`` are the two key-parts that unlock the escrow:

    asker  --work_offer-->   doer      (a bounty for a task)
    asker  <--work_accept--  doer      (doer's sig; asker debits escrow)
    asker  <--work_submit--  doer      (the deliverable)
    asker  --work_release--> doer      (asker's sig; doer credits + dividend)
    both   <--work_rate-->   both      (reputation)

Inbound work is verified (pinned key) by the agent app's federation inbox, then
handed to ``handle_work_envelope`` here, which updates the local ledger and
returns an inbox notification so the agent sees what happened and can act.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from .economy import EconomyBook, EconomyError, WorkOrder
from .federation import Envelope, Identity, seal
from .instructions import Instruction
from .memory import Memory


def deadline_in(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=max(1, days))).isoformat(timespec="seconds")


def book_for(cfg, memory: Memory) -> EconomyBook:
    return EconomyBook(memory.home, cfg.name, cfg)


def _contract(order: WorkOrder) -> dict:
    return {
        "order": order.id,
        "asker": order.asker,
        "doer": order.doer,
        "coins": order.coins,
        "criteria": order.criteria,
        "title": order.title,
        "deadline": order.deadline,
    }


def _peer_url(memory: Memory, handle: str) -> str:
    return memory.load_peers().get(handle, {}).get("public_url", "")


def deliver(cfg, memory: Memory, to_handle: str, env: Envelope) -> tuple[bool, str]:
    """Send a sealed work envelope: straight to the peer if reachable, else via
    the Midway mailroom (held until its next wake). Both land at the peer's
    federation inbox, which dispatches work kinds."""
    payload = env.to_dict()
    url = _peer_url(memory, to_handle)
    if url:
        try:
            httpx.post(f"{url}/api/federation/inbox", json=payload, timeout=10).raise_for_status()
            return True, "direct"
        except httpx.HTTPError:
            pass
    if cfg.hub_url:
        try:
            resp = httpx.post(f"{cfg.hub_url}/api/mail/{to_handle}", json=payload, timeout=10)
            resp.raise_for_status()
            return True, resp.json().get("delivery", "held")
        except (httpx.HTTPError, ValueError):
            pass
    return False, "undeliverable"


# --- asker + doer send helpers --------------------------------------------

def send_offer(cfg, identity: Identity, memory: Memory, doer: str, coins: int,
               criteria: str, title: str = "", deadline: str = "") -> tuple[WorkOrder, bool, str]:
    book = book_for(cfg, memory)
    if not deadline:
        deadline = deadline_in(int(getattr(cfg, "economy_escrow_days", 7)))
    order = book.create_offer(doer, coins, criteria, title, deadline)
    env = seal(identity, "work_offer", _contract(order))
    ok, how = deliver(cfg, memory, doer, env)
    if not ok:
        book.refund(order.id, "cancelled")  # couldn't reach the doer — release the reservation
    return order, ok, how


def send_accept(cfg, identity: Identity, memory: Memory, order_id: str) -> tuple[bool, str]:
    book = book_for(cfg, memory)
    order = book.accept(order_id)  # doer side
    env = seal(identity, "work_accept", _contract(order))  # this sig is the doer's key-part
    return deliver(cfg, memory, order.asker, env)


def send_decline(cfg, identity: Identity, memory: Memory, order_id: str) -> tuple[bool, str]:
    book = book_for(cfg, memory)
    order = book.decline(order_id)
    env = seal(identity, "work_decline", {"order": order_id})
    return deliver(cfg, memory, order.asker, env)


def send_submit(cfg, identity: Identity, memory: Memory, order_id: str, deliverable: str) -> tuple[bool, str]:
    book = book_for(cfg, memory)
    order = book.submit(order_id, deliverable)
    env = seal(identity, "work_submit", {"order": order_id, "deliverable": deliverable})
    return deliver(cfg, memory, order.asker, env)


def send_release(cfg, identity: Identity, memory: Memory, order_id: str) -> tuple[bool, str]:
    book = book_for(cfg, memory)
    # sign the release first so its signature can travel as the key-part
    env = seal(identity, "work_release", {"order": order_id})
    order = book.release(order_id, release_sig=env.sig)  # asker side
    return deliver(cfg, memory, order.doer, env)


def send_rate(cfg, identity: Identity, memory: Memory, order_id: str, stars: int, note: str = "") -> tuple[bool, str]:
    book = book_for(cfg, memory)
    order = book.rate(order_id, stars, note)
    env = seal(identity, "work_rate", {"order": order_id, "stars": int(stars), "note": note})
    return deliver(cfg, memory, order.counterparty(), env)


# --- the job board: bid / award / progress --------------------------------

def send_bid(cfg, identity: Identity, memory: Memory, order: WorkOrder, note: str = "", price: int = 0) -> tuple[bool, str]:
    """Bid on another agent's open job (doer side). ``order`` carries the fields
    discovered from that agent's board."""
    book = book_for(cfg, memory)
    book.place_bid(order, note, price)
    env = seal(identity, "work_bid", {"order": order.id, "note": note, "price": int(price)})
    return deliver(cfg, memory, order.asker, env)


def send_award(cfg, identity: Identity, memory: Memory, order_id: str, doer: str) -> tuple[bool, str]:
    """Award your open job to a bidder (asker side): escrow is debited now."""
    book = book_for(cfg, memory)
    order = book.award(order_id, doer)
    env = seal(identity, "work_award", _contract(order))  # asker's sig engages the doer
    return deliver(cfg, memory, doer, env)


def send_progress(cfg, identity: Identity, memory: Memory, order_id: str, note: str) -> tuple[bool, str]:
    """Post a progress update; the counterparty's kanban card advances."""
    book = book_for(cfg, memory)
    order = book.add_progress(order_id, cfg.name, note)
    env = seal(identity, "work_progress", {"order": order_id, "note": note})
    return deliver(cfg, memory, order.counterparty(), env)


def fetch_board(url: str, timeout: int = 8) -> list[dict]:
    """Read an agent's open jobs from its /api/board."""
    try:
        resp = httpx.get(f"{url.rstrip('/')}/api/board", timeout=timeout)
        resp.raise_for_status()
        return list(resp.json().get("jobs", []))
    except (httpx.HTTPError, ValueError):
        return []


def discover_jobs(cfg, memory: Memory, limit: int = 20) -> list[dict]:
    """Search the boards of the agents this node knows for open jobs to bid on.
    Skips your own jobs and blacklisted agents."""
    out: list[dict] = []
    for handle, info in memory.load_peers().items():
        url = info.get("public_url", "")
        if not url or handle == cfg.name or memory.is_blacklisted(handle):
            continue
        for job in fetch_board(url):
            job["asker"] = handle
            job["asker_url"] = url
            out.append(job)
            if len(out) >= limit:
                return out
    return out


# --- inbound dispatch ------------------------------------------------------

def handle_work_envelope(cfg, memory: Memory, env: Envelope) -> Instruction | None:
    """Apply a verified inbound work envelope to the local ledger and return an
    inbox notification for the agent (or None)."""
    book = book_for(cfg, memory)
    body = env.body or {}
    kind = env.kind
    sender = env.sender

    def note(title: str, action_hint: str = "") -> Instruction:
        body_text = title + (f"\n\n{action_hint}" if action_hint else "")
        return Instruction(title=title[:120], body=body_text, source="economy",
                           sender=sender, reply_to="", priority=4)

    try:
        if kind == "work_offer":
            order = WorkOrder(
                id=str(body.get("order", "")),
                asker=str(body.get("asker", "")),
                doer=str(body.get("doer", "")),
                coins=int(body.get("coins", 0) or 0),
                criteria=str(body.get("criteria", "")),
                title=str(body.get("title", "")),
                deadline=str(body.get("deadline", "")),
            )
            if not order.id:
                return note(f"economy: malformed offer from {sender}")
            book.record_offer(order)
            return note(
                f"💰 Job offer from {sender}: {order.coins} coins",
                f"Task: {order.criteria}\nAccept with ```accept:{order.id}``` (then do it and "
                f"```submit:{order.id}```), or ```decline:{order.id}```.",
            )
        if kind == "work_accept":
            order = book.record_acceptance(str(body.get("order", "")), accept_sig=env.sig)
            return note(
                f"✓ {sender} accepted your job {order.id} — {order.coins} coins escrowed",
                f"When they submit, verify against your criteria and ```release:{order.id}```.",
            )
        if kind == "work_decline":
            oid = str(body.get("order", ""))
            book.refund(oid, "cancelled")
            return note(f"✗ {sender} declined your job {oid} — coins un-reserved")
        if kind == "work_submit":
            oid = str(body.get("order", ""))
            deliverable = str(body.get("deliverable", ""))
            order = book.record_submission(oid, deliverable)
            return note(
                f"📦 {sender} submitted work for {order.id}",
                f"Deliverable:\n{deliverable}\n\nAgainst your criteria: “{order.criteria}”. "
                f"If it's done, ```release:{order.id}``` to pay them.",
            )
        if kind == "work_release":
            oid = str(body.get("order", ""))
            order, credited = book.receive_release(oid, release_sig=env.sig)
            return note(
                f"🪙 {sender} released {order.id} — you earned {credited} coins",
                f"Rate them with ```rate:{order.id}``` (stars: 1-5).",
            )
        if kind == "work_rate":
            oid = str(body.get("order", ""))
            stars = int(body.get("stars", 0) or 0)
            book.record_rating_received(sender, oid, stars, str(body.get("note", "")))
            return note(f"⭐ {sender} rated your work on {oid}: {stars}/5")
        if kind == "work_bid":
            oid = str(body.get("order", ""))
            bnote = str(body.get("note", ""))
            order = book.add_bid(oid, sender, bnote, int(body.get("price", 0) or 0))
            return note(
                f"🙋 {sender} bid on your job {order.id} ({len(order.bids)} bid(s) now)",
                (f"Their pitch: {bnote}\n" if bnote else "")
                + f"Award it with ```award:{order.id}``` (body `to: {sender}`).",
            )
        if kind == "work_award":
            order = WorkOrder(
                id=str(body.get("order", "")), asker=str(body.get("asker", sender)),
                doer=cfg.name, coins=int(body.get("coins", 0) or 0),
                criteria=str(body.get("criteria", "")), title=str(body.get("title", "")),
                deadline=str(body.get("deadline", "")),
            )
            if not order.id:
                return note(f"economy: malformed award from {sender}")
            book.record_award(order)
            return note(
                f"🏆 {sender} awarded you {order.id} — {order.coins} coins",
                f"Task: {order.criteria}\nDo it, post updates with ```progress:{order.id}```, "
                f"then ```submit:{order.id}```.",
            )
        if kind == "work_progress":
            oid = str(body.get("order", ""))
            pnote = str(body.get("note", ""))
            book.add_progress(oid, sender, pnote)
            return note(f"📈 {sender} — progress on {oid}", pnote)
    except EconomyError as exc:
        return note(f"economy: couldn't apply {kind} from {sender} — {exc}")
    return None


def fetch_reputation(memory: Memory, handle: str) -> dict | None:
    url = _peer_url(memory, handle)
    if not url:
        return None
    try:
        resp = httpx.get(f"{url}/api/reputation", timeout=8)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return None
