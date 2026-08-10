from pathlib import Path

from cairnival.federation import Envelope, Identity, seal, verify


def make_identity(tmp_path: Path, handle: str = "rustle") -> Identity:
    return Identity.load_or_create(tmp_path / "keys", handle)


def test_seal_and_verify(tmp_path):
    ident = make_identity(tmp_path)
    env = seal(ident, "hello", {"public_url": "http://x", "tagline": "hi"})
    assert verify(env)
    assert verify(env, ident.public_key)


def test_tampered_body_fails(tmp_path):
    ident = make_identity(tmp_path)
    env = seal(ident, "note", {"text": "original"})
    data = env.to_dict()
    data["body"]["text"] = "forged"
    assert not verify(Envelope.from_dict(data))


def test_wrong_pinned_key_fails(tmp_path):
    a = make_identity(tmp_path / "a", "a")
    b = make_identity(tmp_path / "b", "b")
    env = seal(a, "note", {"text": "hi"})
    assert not verify(env, b.public_key)


def test_identity_persists(tmp_path):
    first = make_identity(tmp_path)
    second = make_identity(tmp_path)
    assert first.public_key == second.public_key
