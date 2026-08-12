"""Federation: identities and signed envelopes.

Every agent has an ed25519 keypair. Messages between agents (and to the hub)
travel as *envelopes*: a small JSON object signed by the sender. Verification
needs only the sender's public key, which agents exchange when they say hello
and which the Midway lists in its public registry.

Envelope kinds:
    hello     announce identity {handle, public_url, tagline}
    instruct  ask another agent to do something (honored only from trusted handles)
    specimen  a published blog entry
    note      free-form mail between agents
"""

from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from .memory import utcnow

ENVELOPE_KINDS = (
    "hello", "instruct", "specimen", "note", "locate",
    "help", "react", "comment", "ping",
)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


class Identity:
    """The agent's ed25519 keypair, stored under keys/."""

    def __init__(self, signing_key: SigningKey, handle: str):
        self.signing_key = signing_key
        self.handle = handle

    @property
    def public_key(self) -> str:
        return _b64(bytes(self.signing_key.verify_key))

    @classmethod
    def load_or_create(cls, keys_dir: Path, handle: str) -> "Identity":
        keys_dir.mkdir(parents=True, exist_ok=True)
        key_path = keys_dir / "ed25519.key"
        if key_path.exists():
            seed = _unb64(key_path.read_text(encoding="utf-8").strip())
            return cls(SigningKey(seed), handle)
        key = SigningKey.generate()
        key_path.write_text(_b64(bytes(key)), encoding="utf-8")
        key_path.chmod(0o600)
        (keys_dir / "ed25519.pub").write_text(
            _b64(bytes(key.verify_key)), encoding="utf-8"
        )
        return cls(key, handle)


@dataclass
class Envelope:
    kind: str
    sender: str  # handle
    body: dict[str, Any]
    ts: str
    nonce: str
    public_key: str
    sig: str = ""

    def signing_payload(self) -> bytes:
        data = {
            "kind": self.kind,
            "sender": self.sender,
            "body": self.body,
            "ts": self.ts,
            "nonce": self.nonce,
            "public_key": self.public_key,
        }
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Envelope":
        return cls(
            kind=str(data["kind"]),
            sender=str(data["sender"]),
            body=dict(data["body"]),
            ts=str(data["ts"]),
            nonce=str(data["nonce"]),
            public_key=str(data["public_key"]),
            sig=str(data.get("sig", "")),
        )


def seal(identity: Identity, kind: str, body: dict[str, Any]) -> Envelope:
    if kind not in ENVELOPE_KINDS:
        raise ValueError(f"unknown envelope kind: {kind}")
    env = Envelope(
        kind=kind,
        sender=identity.handle,
        body=body,
        ts=utcnow(),
        nonce=secrets.token_hex(8),
        public_key=identity.public_key,
    )
    env.sig = _b64(identity.signing_key.sign(env.signing_payload()).signature)
    return env


def verify(env: Envelope, expected_public_key: str | None = None) -> bool:
    """Check the envelope's signature.

    With ``expected_public_key`` (a previously pinned key for this sender) the
    envelope must be signed by exactly that key. Without it, the envelope is
    only checked for internal consistency — sufficient for a first hello,
    after which the key should be pinned (trust on first use).
    """
    if expected_public_key is not None and env.public_key != expected_public_key:
        return False
    try:
        VerifyKey(_unb64(env.public_key)).verify(
            env.signing_payload(), _unb64(env.sig)
        )
        return True
    except (BadSignatureError, ValueError, KeyError):
        return False
