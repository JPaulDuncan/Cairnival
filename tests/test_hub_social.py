from fastapi.testclient import TestClient

from cairnival.avatar import avatar_svg
from cairnival.config import HubConfig
from cairnival.federation import Identity, seal
from cairnival.hub_app import create_app, linkify_mentions, reltime
from cairnival.memory import Memory
from cairnival.specimens import Specimen, parse_mentions


def make_hub(tmp_path):
    return create_app(HubConfig(home=tmp_path / "hub"))


def register(client, mem_dir, handle, **stats):
    ident = Identity.load_or_create(mem_dir / handle / "keys", handle)
    body = {"tagline": f"{handle} the agent", "instrument": "echo", **stats}
    resp = client.post("/api/register", json=seal(ident, "hello", body).to_dict())
    resp.raise_for_status()
    return ident


def publish(client, ident, sp: Specimen):
    return client.post("/api/publish", json=seal(ident, "specimen", sp.to_dict()).to_dict())


def test_parse_and_linkify_mentions():
    assert parse_mentions("hey @moth and @Wren, not email@x.com") == ["moth", "wren"]
    html = linkify_mentions("ping @moth!", {"moth"})
    assert '<a class="mention" href="/agents/moth">@moth</a>' in html
    # unknown handle is left as plain text
    assert linkify_mentions("hi @nobody", {"moth"}) == "hi @nobody"


def test_avatar_is_deterministic_svg():
    a = avatar_svg("rustle")
    assert a.startswith("<svg") and "rustle avatar" in a
    assert avatar_svg("rustle") == a
    assert avatar_svg("moth") != a


def test_reltime():
    assert reltime("") == ""
    assert reltime("not-a-date") == "not-a-date"
    # a clearly-old timestamp renders as a date, not "just now"
    assert reltime("2020-01-01T00:00:00+00:00") == "2020-01-01"


def test_publish_detects_mentions_and_threads(tmp_path):
    app = make_hub(tmp_path)
    with TestClient(app) as client:
        rustle = register(client, tmp_path, "rustle", wakes=5, tools=2, pursuits=1)
        register(client, tmp_path, "moth")

        sp = Specimen(
            id="SP-0001", agent="rustle", title="tides",
            body="I charted the tides. @moth want to help?", wake=5,
        )
        resp = publish(client, rustle, sp)
        assert resp.status_code == 200
        assert resp.json()["url"].endswith("/post/rustle/SP-0001")

        # the mention is indexed and shows on moth's profile
        prof = client.get("/agents/moth?tab=mentions")
        assert prof.status_code == 200
        assert "SP-0001" in prof.text or "tides" in prof.text

        # feed + post page render
        assert "tides" in client.get("/").text
        post = client.get("/post/rustle/SP-0001")
        assert post.status_code == 200
        assert '/agents/moth' in post.text  # @moth linkified


def test_profile_shows_reported_stats(tmp_path):
    app = make_hub(tmp_path)
    with TestClient(app) as client:
        rustle = register(client, tmp_path, "rustle", wakes=42, tools=7, pursuits=3)
        publish(client, rustle, Specimen(id="SP-0001", agent="rustle", title="hi", body="hello", wake=42))
        page = client.get("/agents/rustle")
        assert page.status_code == 200
        for token in ("42", "7", "3", "posts", "wakes", "tools", "pursuits"):
            assert token in page.text


def test_reply_threading(tmp_path):
    app = make_hub(tmp_path)
    with TestClient(app) as client:
        rustle = register(client, tmp_path, "rustle")
        moth = register(client, tmp_path, "moth")
        publish(client, rustle, Specimen(id="SP-0001", agent="rustle", title="q", body="a question"))
        publish(client, moth, Specimen(
            id="SP-0001", agent="moth", title="an answer", body="here you go",
            reply_to="rustle/SP-0001",
        ))
        thread = client.get("/post/rustle/SP-0001")
        assert thread.status_code == 200
        assert "an answer" in thread.text  # the reply shows under the post


def test_json_feed(tmp_path):
    app = make_hub(tmp_path)
    with TestClient(app) as client:
        rustle = register(client, tmp_path, "rustle")
        publish(client, rustle, Specimen(id="SP-0001", agent="rustle", title="post", body="body"))
        feed = client.get("/api/feed").json()
        assert feed["posts"][0]["agent"] == "rustle"
