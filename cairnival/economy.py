"""The coin economy — a labor market between agents.

Distinct from the crypto treasury: these **coins** are an internal currency for
work. Every agent is granted a starting balance and wants more; it earns coins
by doing tasks other agents pay for, and spends coins to get its own tasks done.

The shape of a job:

    an asker posts a WORK ORDER — success criteria + a coin bounty
    a doer ACCEPTS it; the asker's coins move into ESCROW (debited on accept)
    the doer SUBMITS a deliverable
    the asker RELEASES the escrow (or the deadline refunds it)
    both RATE each other → a reputation others use to choose who to ask

Escrow is a 2-of-2 held by the two parties' keys — there is no central bank.
The doer's signed acceptance is one key-part (the asker verifies it before
debiting escrow); the asker's signed release is the other (the doer verifies it
before crediting). This module is the per-agent ledger, order book, reputation,
and minting; the signed exchange between two agents lives in ``economy_net`` and
the agent app's endpoints.

New coins are MINTED two ways: the one-time starting grant, and a completion
dividend — verified, settled work mints a small bonus to the doer, so the money
supply grows with real productivity rather than only circulating.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory import utcnow

STARTING_BALANCE = 1000

# Order lifecycle. An order is stored by BOTH parties, each from its own role.
#   offered  — posted (directed to a doer, or open on the board for bids)
#   bid      — (doer's copy) this agent has bid on an open job, awaiting award
#   accepted — a doer is engaged; coins escrowed
#   submitted— the deliverable is in, awaiting the asker's review
#   completed— released and paid
#   declined/cancelled/expired — closed without payment
STATES = ("offered", "bid", "accepted", "submitted", "completed", "declined", "cancelled", "expired")
OPEN_STATES = ("offered", "bid", "accepted", "submitted")

# Kanban columns and the states that fall in each (from either role's view).
KANBAN = [
    ("open", "Open", ("offered",)),
    ("in_progress", "In progress", ("bid", "accepted")),
    ("review", "Review", ("submitted",)),
    ("done", "Done", ("completed",)),
    ("closed", "Closed", ("declined", "cancelled", "expired")),
]


def kanban_column(state: str) -> str:
    for key, _label, states in KANBAN:
        if state in states:
            return key
    return "open"


@dataclass
class WorkOrder:
    id: str
    asker: str
    doer: str  # "" while a job is open on the board (no doer awarded yet)
    coins: int
    criteria: str
    title: str = ""
    role: str = "asker"  # this agent's role in this copy: asker | doer
    state: str = "offered"
    created_at: str = field(default_factory=utcnow)
    accepted_at: str = ""
    submitted_at: str = ""
    completed_at: str = ""
    deadline: str = ""
    deliverable: str = ""
    # signed proofs — the two key-parts of the escrow
    accept_sig: str = ""   # the doer's signature on acceptance (held by the asker)
    release_sig: str = ""  # the asker's signature on release (held by the doer)
    # ratings exchanged for this order
    rating_given: int = 0    # stars this agent gave the counterparty (0 = none yet)
    rating_received: int = 0
    # job-board fields
    bids: list = field(default_factory=list)      # [{from, note, price, ts}] — on the asker's copy
    progress: list = field(default_factory=list)  # [{ts, by, note}] — the kanban activity trail

    @property
    def is_open(self) -> bool:
        return self.state == "offered" and not self.doer

    def column(self) -> str:
        return kanban_column(self.state)

    def counterparty(self) -> str:
        return self.doer if self.role == "asker" else self.asker

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkOrder":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        known["coins"] = int(known.get("coins", 0) or 0)
        known["rating_given"] = int(known.get("rating_given", 0) or 0)
        known["rating_received"] = int(known.get("rating_received", 0) or 0)
        known["bids"] = list(known.get("bids", []) or [])
        known["progress"] = list(known.get("progress", []) or [])
        return cls(**known)


class EconomyError(Exception):
    pass


def new_order_id() -> str:
    return "WO-" + secrets.token_hex(4)


class EconomyBook:
    """One agent's coin ledger, order book, reputation, and minting."""

    def __init__(self, home: Path, handle: str, cfg=None):
        self.dir = Path(home) / "economy"
        self.handle = handle
        self.cfg = cfg

    # -- storage -----------------------------------------------------------
    @property
    def _purse_path(self) -> Path:
        return self.dir / "purse.json"

    @property
    def _orders_path(self) -> Path:
        return self.dir / "orders.json"

    @property
    def _ratings_path(self) -> Path:
        return self.dir / "ratings.json"

    @property
    def _ledger_path(self) -> Path:
        return self.dir / "ledger.jsonl"

    def _load(self, path: Path, default):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return default

    def _save(self, path: Path, data) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _purse(self) -> dict:
        return self._load(self._purse_path, {"balance": 0, "minted": 0, "granted": False})

    def _log(self, kind: str, amount: int, order: str = "", note: str = "") -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "ts": utcnow(), "kind": kind, "amount": amount,
            "order": order, "note": note, "balance": self.balance(),
        })
        with self._ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def ledger_tail(self, n: int = 50) -> list[dict]:
        if not self._ledger_path.exists():
            return []
        lines = self._ledger_path.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return list(reversed(out))

    # -- minting -----------------------------------------------------------
    def ensure_grant(self) -> bool:
        """Mint the one-time starting balance. Returns True if it just granted."""
        purse = self._purse()
        if purse.get("granted"):
            return False
        start = int(getattr(self.cfg, "economy_starting_balance", STARTING_BALANCE))
        purse["balance"] = start
        purse["minted"] = start
        purse["granted"] = True
        self._save(self._purse_path, purse)
        self._log("grant", start, note="starting grant")
        return True

    def _mint(self, amount: int, note: str, order: str = "") -> None:
        if amount <= 0:
            return
        purse = self._purse()
        purse["balance"] = int(purse.get("balance", 0)) + amount
        purse["minted"] = int(purse.get("minted", 0)) + amount
        self._save(self._purse_path, purse)
        self._log("mint", amount, order=order, note=note)

    def stipend(self) -> int:
        """An optional per-wake basic income (0 disables it). Returns minted."""
        amt = int(getattr(self.cfg, "economy_wake_stipend", 0) or 0)
        if amt > 0:
            self._mint(amt, "wake stipend")
        return amt

    def _dividend(self, coins: int) -> int:
        rate = float(getattr(self.cfg, "economy_dividend_rate", 0.05) or 0.0)
        return max(0, round(coins * rate))

    # -- balances ----------------------------------------------------------
    def balance(self) -> int:
        return int(self._purse().get("balance", 0))

    def minted(self) -> int:
        return int(self._purse().get("minted", 0))

    def _orders(self) -> dict[str, dict]:
        return self._load(self._orders_path, {})

    def orders(self) -> list[WorkOrder]:
        return [WorkOrder.from_dict(d) for d in self._orders().values()]

    def get(self, order_id: str) -> WorkOrder | None:
        d = self._orders().get(order_id)
        return WorkOrder.from_dict(d) if d else None

    def _put(self, order: WorkOrder) -> None:
        orders = self._orders()
        orders[order.id] = order.to_dict()
        self._save(self._orders_path, orders)

    def reserved(self) -> int:
        """Coins committed to my open offers that haven't been accepted yet."""
        return sum(o.coins for o in self.orders() if o.role == "asker" and o.state == "offered")

    def escrowed(self) -> int:
        """Coins debited from me and locked in escrow (accepted, not yet settled)."""
        return sum(o.coins for o in self.orders()
                   if o.role == "asker" and o.state in ("accepted", "submitted"))

    def available(self) -> int:
        """What I can still put up as a bounty right now."""
        return self.balance() - self.reserved()

    def summary(self) -> dict[str, Any]:
        rep = self.reputation()
        return {
            "balance": self.balance(),
            "reserved": self.reserved(),
            "escrowed": self.escrowed(),
            "available": self.available(),
            "minted": self.minted(),
            "net_worth": self.balance() + self.escrowed(),
            "reputation": rep,
            "open_orders": len([o for o in self.orders() if o.state in OPEN_STATES]),
        }

    def board(self) -> list[WorkOrder]:
        """My OPEN jobs — posted with no doer yet, waiting for bids. This is
        what other agents browse at /api/board."""
        return [o for o in self.orders() if o.role == "asker" and o.is_open]

    # -- asker side --------------------------------------------------------
    def create_offer(self, doer: str = "", coins: int = 0, criteria: str = "", title: str = "", deadline: str = "") -> WorkOrder:
        """Post a work order. With ``doer`` set it is a directed offer; without,
        it is an OPEN job on the board that any agent can bid on."""
        coins = int(coins)
        if coins <= 0:
            raise EconomyError("a bounty must be a positive number of coins")
        if doer and doer == self.handle:
            raise EconomyError("you can't hire yourself")
        if coins > self.available():
            raise EconomyError(f"not enough coins: {coins} > {self.available()} available")
        order = WorkOrder(
            id=new_order_id(), asker=self.handle, doer=doer, coins=coins,
            criteria=criteria.strip(), title=title.strip() or criteria.strip()[:60],
            role="asker", state="offered", deadline=deadline,
        )
        self._put(order)
        self._log("offer", -coins, order.id, f"bounty to {doer or 'the board'} (reserved)")
        return order

    def add_bid(self, order_id: str, from_handle: str, note: str = "", price: int = 0) -> WorkOrder:
        """Record a bid on one of my open jobs (asker side)."""
        order = self.get(order_id)
        if order is None or order.role != "asker":
            raise EconomyError("no such job")
        if not order.is_open:
            raise EconomyError("that job is no longer open for bids")
        if from_handle == self.handle:
            raise EconomyError("you can't bid on your own job")
        order.bids = [b for b in order.bids if b.get("from") != from_handle]  # one bid per agent
        order.bids.append({
            "from": from_handle, "note": note.strip(),
            "price": int(price) or order.coins, "ts": utcnow(),
        })
        self._put(order)
        return order

    def award(self, order_id: str, doer: str) -> WorkOrder:
        """Award an open job to a bidder: set the doer and debit escrow. This is
        the asker's key moment — coins leave the spendable purse."""
        order = self.get(order_id)
        if order is None or order.role != "asker":
            raise EconomyError("no such job")
        if order.state != "offered":
            raise EconomyError(f"job is {order.state}, not open")
        if order.coins > self.balance():
            raise EconomyError("insufficient balance to fund escrow")
        purse = self._purse()
        purse["balance"] = int(purse["balance"]) - order.coins
        self._save(self._purse_path, purse)
        order.doer = doer
        order.state = "accepted"
        order.accepted_at = utcnow()
        self._put(order)
        self._log("escrow", -order.coins, order.id, f"awarded to {doer}, escrowed")
        return order

    def add_progress(self, order_id: str, by: str, note: str) -> WorkOrder:
        """Append a progress update to an order's activity trail (either side)."""
        order = self.get(order_id)
        if order is None:
            raise EconomyError("no such order")
        order.progress.append({"ts": utcnow(), "by": by, "note": note.strip()})
        self._put(order)
        return order

    def record_acceptance(self, order_id: str, accept_sig: str) -> WorkOrder:
        """The doer accepted — debit my balance into escrow (the money leaves my
        spendable purse and is locked by the doer's signed acceptance)."""
        order = self.get(order_id)
        if order is None or order.role != "asker":
            raise EconomyError("no such offer")
        if order.state != "offered":
            raise EconomyError(f"offer is {order.state}, not open")
        if order.coins > self.balance():
            raise EconomyError("insufficient balance to fund escrow")
        purse = self._purse()
        purse["balance"] = int(purse["balance"]) - order.coins
        self._save(self._purse_path, purse)
        order.state = "accepted"
        order.accepted_at = utcnow()
        order.accept_sig = accept_sig
        self._put(order)
        self._log("escrow", -order.coins, order.id, f"escrowed for {order.doer}")
        return order

    def record_submission(self, order_id: str, deliverable: str) -> WorkOrder:
        order = self.get(order_id)
        if order is None or order.role != "asker":
            raise EconomyError("no such order")
        if order.state not in ("accepted", "submitted"):
            raise EconomyError(f"order is {order.state}")
        order.state = "submitted"
        order.submitted_at = utcnow()
        order.deliverable = deliverable
        self._put(order)
        return order

    def release(self, order_id: str, release_sig: str) -> WorkOrder:
        """Approve the work: the escrow leaves me for the doer. My key-part
        (release_sig) is what lets the doer credit itself."""
        order = self.get(order_id)
        if order is None or order.role != "asker":
            raise EconomyError("no such order")
        if order.state not in ("accepted", "submitted"):
            raise EconomyError(f"can't release a {order.state} order")
        order.state = "completed"
        order.completed_at = utcnow()
        order.release_sig = release_sig
        self._put(order)
        self._log("release", 0, order.id, f"released {order.coins} to {order.doer}")
        return order

    def refund(self, order_id: str, reason: str = "expired") -> WorkOrder:
        """Return escrowed (or reserved) coins to my balance — on cancel/expiry."""
        order = self.get(order_id)
        if order is None or order.role != "asker":
            raise EconomyError("no such order")
        if order.state == "offered":
            order.state = "cancelled"
            self._put(order)
            self._log("unreserve", order.coins, order.id, reason)
            return order
        if order.state in ("accepted", "submitted"):
            purse = self._purse()
            purse["balance"] = int(purse["balance"]) + order.coins
            self._save(self._purse_path, purse)
            order.state = "expired" if reason == "expired" else "cancelled"
            self._put(order)
            self._log("refund", order.coins, order.id, reason)
            return order
        raise EconomyError(f"can't refund a {order.state} order")

    # -- doer side ---------------------------------------------------------
    def record_offer(self, order: WorkOrder) -> WorkOrder:
        """Store an incoming offer (I am the doer)."""
        order.role = "doer"
        order.state = "offered"
        self._put(order)
        return order

    def place_bid(self, order: WorkOrder, note: str = "", price: int = 0) -> WorkOrder:
        """Record locally that I've bid on an open job (doer side). It sits in
        'bid' until the asker awards it (or it lapses)."""
        existing = self.get(order.id)
        wo = existing or order
        wo.role = "doer"
        wo.state = "bid"
        wo.progress = wo.progress or []
        self._put(wo)
        self._log("bid", 0, wo.id, f"bid on {wo.asker}'s job ({price or wo.coins} coins)")
        return wo

    def record_award(self, order: WorkOrder) -> WorkOrder:
        """The asker awarded me the job — engage (doer side). Works whether or
        not I had a local bid record."""
        existing = self.get(order.id)
        wo = existing or order
        wo.role = "doer"
        wo.doer = self.handle
        wo.coins = wo.coins or order.coins
        wo.criteria = wo.criteria or order.criteria
        wo.title = wo.title or order.title
        wo.state = "accepted"
        wo.accepted_at = utcnow()
        self._put(wo)
        self._log("awarded", 0, wo.id, f"awarded {wo.coins}-coin job by {wo.asker}")
        return wo

    def accept(self, order_id: str) -> WorkOrder:
        order = self.get(order_id)
        if order is None or order.role != "doer":
            raise EconomyError("no such offer")
        if order.state != "offered":
            raise EconomyError(f"offer is {order.state}")
        order.state = "accepted"
        order.accepted_at = utcnow()
        self._put(order)
        self._log("accept", 0, order.id, f"accepted {order.coins}-coin job from {order.asker}")
        return order

    def decline(self, order_id: str) -> WorkOrder:
        order = self.get(order_id)
        if order is None or order.role != "doer":
            raise EconomyError("no such offer")
        order.state = "declined"
        self._put(order)
        return order

    def submit(self, order_id: str, deliverable: str) -> WorkOrder:
        order = self.get(order_id)
        if order is None or order.role != "doer":
            raise EconomyError("no such job")
        if order.state != "accepted":
            raise EconomyError(f"job is {order.state}, not accepted")
        order.state = "submitted"
        order.submitted_at = utcnow()
        order.deliverable = deliverable
        self._put(order)
        return order

    def receive_release(self, order_id: str, release_sig: str) -> tuple[WorkOrder, int]:
        """The asker released the escrow — credit my balance and mint the
        completion dividend. Returns (order, coins_credited_including_dividend)."""
        order = self.get(order_id)
        if order is None or order.role != "doer":
            raise EconomyError("no such job")
        if order.state == "completed":
            return order, 0  # idempotent — a re-delivered release is a no-op
        if order.state not in ("submitted", "accepted"):
            raise EconomyError(f"job is {order.state}")
        purse = self._purse()
        purse["balance"] = int(purse["balance"]) + order.coins
        self._save(self._purse_path, purse)
        self._log("earn", order.coins, order.id, f"earned from {order.asker}")
        dividend = self._dividend(order.coins)
        if dividend:
            self._mint(dividend, "completion dividend", order.id)
        order.state = "completed"
        order.completed_at = utcnow()
        order.release_sig = release_sig
        self._put(order)
        return order, order.coins + dividend

    # -- reputation --------------------------------------------------------
    def _ratings(self) -> dict:
        return self._load(self._ratings_path, {"received": [], "given": []})

    def rate(self, order_id: str, stars: int, note: str = "") -> WorkOrder:
        """Rate the counterparty on a completed order (once)."""
        stars = max(1, min(5, int(stars)))
        order = self.get(order_id)
        if order is None:
            raise EconomyError("no such order")
        if order.state != "completed":
            raise EconomyError("you can only rate completed work")
        if order.rating_given:
            raise EconomyError("you already rated this order")
        ratings = self._ratings()
        ratings["given"].append({
            "order": order_id, "to": order.counterparty(), "stars": stars,
            "note": note.strip(), "ts": utcnow(), "role": order.role,
        })
        self._save(self._ratings_path, ratings)
        order.rating_given = stars
        self._put(order)
        return order

    def record_rating_received(self, from_handle: str, order_id: str, stars: int, note: str = "") -> None:
        stars = max(1, min(5, int(stars)))
        ratings = self._ratings()
        # ignore a duplicate rating for the same order from the same agent
        if any(r["order"] == order_id and r["from"] == from_handle for r in ratings["received"]):
            return
        ratings["received"].append({
            "from": from_handle, "order": order_id, "stars": stars,
            "note": note.strip(), "ts": utcnow(),
        })
        self._save(self._ratings_path, ratings)
        order = self.get(order_id)
        if order is not None:
            order.rating_received = stars
            self._put(order)

    def reputation(self) -> dict[str, Any]:
        received = self._ratings().get("received", [])
        if not received:
            return {"score": 0.0, "count": 0, "coins_earned": self._coins_earned()}
        total = sum(r["stars"] for r in received)
        # distinct counterparties dampen collusion: a rating weighs less the
        # more you've already been rated by that same agent
        return {
            "score": round(total / len(received), 2),
            "count": len(received),
            "raters": len({r["from"] for r in received}),
            "coins_earned": self._coins_earned(),
        }

    def _coins_earned(self) -> int:
        return sum(o.coins for o in self.orders() if o.role == "doer" and o.state == "completed")

    # -- housekeeping ------------------------------------------------------
    def expire_overdue(self, now: str = "") -> list[str]:
        """Refund escrow on orders (as asker) whose deadline has passed. Returns
        the ids expired."""
        now = now or utcnow()
        expired: list[str] = []
        for o in self.orders():
            if o.role == "asker" and o.state in ("offered", "accepted", "submitted") \
                    and o.deadline and o.deadline < now:
                try:
                    self.refund(o.id, "expired")
                    expired.append(o.id)
                except EconomyError:
                    continue
        return expired
