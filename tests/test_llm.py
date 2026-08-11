import httpx
import pytest

from cairnival.config import AgentConfig
from cairnival.llm import OllamaBackend, build_backend, strip_thinking


def test_strip_thinking_removes_blocks():
    assert strip_thinking("<think>plan plan</think>Answer") == "Answer"
    assert strip_thinking("<think>\nmulti\nline\n</think>\n\nThe answer.") == "The answer."
    # case-insensitive and variant tags
    assert strip_thinking("<THINK>x</THINK>hi") == "hi"
    assert strip_thinking("<reasoning>x</reasoning>done") == "done"


def test_strip_thinking_unclosed_is_dropped():
    # ran out of tokens mid-thought: no answer, so keep nothing rather than
    # publish the monologue
    assert strip_thinking("<think>I should probably start by") == ""


def test_strip_thinking_leaves_normal_text():
    assert strip_thinking("Just a normal answer.") == "Just a normal answer."
    assert strip_thinking("") == ""


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_ollama_sends_think_false_and_strips(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse(
            {"message": {"content": "<think>reasoning here</think>The real answer."}}
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    backend = OllamaBackend("http://x:11434", "qwen3:30b-a3b")
    out = backend.chat("sys", "prompt")
    assert out == "The real answer."
    assert captured["json"]["think"] is False  # thinking disabled by default


def test_ollama_retries_without_think_on_400(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        if "think" in json:
            req = httpx.Request("POST", url)
            resp = httpx.Response(400, request=req)
            raise httpx.HTTPStatusError("bad", request=req, response=resp)
        return FakeResponse({"message": {"content": "ok"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    backend = OllamaBackend("http://x:11434", "llama3.2")
    assert backend.chat("s", "p") == "ok"
    assert calls["n"] == 2  # first with think (400), retried without


def test_build_backend_threads_think_flag():
    cfg = AgentConfig()
    cfg.llm_backend = "ollama"
    cfg.llm_think = True
    backend = build_backend(cfg)
    assert isinstance(backend, OllamaBackend)
    assert backend.think is True
    assert cfg.llm_max_tokens == 2048  # roomier default for real work
