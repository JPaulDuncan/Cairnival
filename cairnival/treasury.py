"""Treasury: money the agent can be given but can never spend alone.

Modeled on Cairn's arrangement — a two-party treasury where the agent holds
one key and a human co-signer holds the other. Here that is expressed as a
ledger of deposits and *spend proposals*: the agent (or an instruction) can
propose a payment; nothing leaves until the co-signer approves it through
the web UI. A rejected proposal stays on the record.

Deposits may carry a memo. A deposit at or above the ask price whose memo
contains a question becomes a **paid instruction** on the next wake — the
same mechanism Cairn uses with transaction memos on-chain.

Chain adapters:
    dryrun   the default — an honest ledger file, no real chain
    solana   integration point for a real co-signed on-chain treasury
             (e.g. a Squads v4 2-of-2 multisig); see SolanaChain below
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory import utcnow


@dataclass
class Proposal:
    id: str
    to: str
    amount: float
    reason: str
    status: str = "pending"  # pending | approved | rejected
    proposed: str = field(default_factory=utcnow)
    resolved: str = ""


class Ledger:
    """JSON-file ledger under treasury/ledger.json."""

    def __init__(self, treasury_dir: Path, currency: str = "SOL"):
        self.path = Path(treasury_dir) / "ledger.json"
        self.currency = currency

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "currency": self.currency,
                "deposits": [],
                "spends": [],
                "proposals": [],
                "consumed_memos": [],
            }
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -- balance -----------------------------------------------------------
    def balance(self) -> float:
        data = self._load()
        received = sum(d["amount"] for d in data["deposits"])
        spent = sum(s["amount"] for s in data["spends"])
        return round(received - spent, 9)

    # -- deposits ----------------------------------------------------------
    def deposit(self, amount: float, sender: str = "", memo: str = "") -> dict[str, Any]:
        if amount <= 0:
            raise ValueError("deposit must be positive")
        data = self._load()
        entry = {
            "id": f"dep-{secrets.token_hex(4)}",
            "amount": float(amount),
            "from": sender,
            "memo": memo,
            "ts": utcnow(),
        }
        data["deposits"].append(entry)
        self._save(data)
        return entry

    def unconsumed_memo_deposits(self, min_amount: float) -> list[dict[str, Any]]:
        """Deposits with a memo, at or above the ask price, not yet turned
        into an instruction."""
        data = self._load()
        consumed = set(data.get("consumed_memos", []))
        return [
            d
            for d in data["deposits"]
            if d.get("memo") and d["amount"] >= min_amount and d["id"] not in consumed
        ]

    def consume_memo(self, deposit_id: str) -> None:
        data = self._load()
        if deposit_id not in data.setdefault("consumed_memos", []):
            data["consumed_memos"].append(deposit_id)
        self._save(data)

    # -- proposals (the agent's half of the co-sign) -----------------------
    def propose(self, to: str, amount: float, reason: str) -> Proposal:
        if amount <= 0:
            raise ValueError("proposal amount must be positive")
        prop = Proposal(id=f"prop-{secrets.token_hex(4)}", to=to, amount=float(amount), reason=reason)
        data = self._load()
        data["proposals"].append(prop.__dict__)
        self._save(data)
        return prop

    def proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        data = self._load()
        items = data["proposals"]
        if status:
            items = [p for p in items if p["status"] == status]
        return items

    def resolve(self, proposal_id: str, approve: bool) -> dict[str, Any]:
        """The co-signer's half. Approval executes the spend — but only if
        the balance covers it."""
        data = self._load()
        for prop in data["proposals"]:
            if prop["id"] != proposal_id:
                continue
            if prop["status"] != "pending":
                raise ValueError(f"proposal {proposal_id} already {prop['status']}")
            if approve:
                received = sum(d["amount"] for d in data["deposits"])
                spent = sum(s["amount"] for s in data["spends"])
                if prop["amount"] > received - spent:
                    raise ValueError("insufficient balance to approve this spend")
                prop["status"] = "approved"
                prop["resolved"] = utcnow()
                data["spends"].append(
                    {
                        "id": f"spd-{secrets.token_hex(4)}",
                        "proposal": proposal_id,
                        "to": prop["to"],
                        "amount": prop["amount"],
                        "reason": prop["reason"],
                        "ts": utcnow(),
                    }
                )
            else:
                prop["status"] = "rejected"
                prop["resolved"] = utcnow()
            self._save(data)
            return prop
        raise KeyError(f"no such proposal: {proposal_id}")

    def summary(self) -> dict[str, Any]:
        data = self._load()
        return {
            "currency": data.get("currency", self.currency),
            "balance": self.balance(),
            "deposits": len(data["deposits"]),
            "spends": len(data["spends"]),
            "pending_proposals": len(
                [p for p in data["proposals"] if p["status"] == "pending"]
            ),
        }


class SolanaChain:
    """Integration point for a real on-chain treasury.

    The intended production shape (mirroring Cairn):

    * a Squads v4 2-of-2 multisig vault holds the funds;
    * the agent's ed25519 key is one member, the human's offline key the other;
    * ``read_deposits`` scans vault transactions for incoming transfers and
      their memos (paid questions);
    * ``propose`` creates a vault transaction; the human approves it in the
      Squads UI — approval never happens in this process.

    Left unimplemented on purpose: wiring real funds should be an explicit,
    reviewed step, not a default. Install `solana`/`solders` and fill these in.
    """

    def __init__(self, rpc_url: str = "", vault: str = ""):
        self.rpc_url = rpc_url
        self.vault = vault

    def read_deposits(self) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "SolanaChain is an integration stub — see docstring for the intended wiring"
        )

    def propose(self, to: str, amount: float, reason: str) -> str:
        raise NotImplementedError(
            "SolanaChain is an integration stub — see docstring for the intended wiring"
        )
