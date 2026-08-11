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


def test_ollama_thinks_by_default_and_keeps_only_the_answer(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        # Ollama separates reasoning (thinking) from the answer (content);
        # a stray inline <think> in content should also be stripped.
        return FakeResponse(
            {
                "message": {
                    "thinking": "long private chain of thought…",
                    "content": "<think>oops</think>The real answer.",
                }
            }
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    backend = OllamaBackend("http://x:11434", "qwen3:30b-a3b")
    out = backend.chat("sys", "prompt")
    assert out == "The real answer."          # only the answer, reasoning dropped
    assert captured["json"]["think"] is True  # thinking is ON by default


def test_ollama_per_call_think_override(monkeypatch):
    seen = []

    def fake_post(url, json=None, timeout=None):
        seen.append(json["think"])
        return FakeResponse({"message": {"content": "answer"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    backend = OllamaBackend("http://x:11434", "qwen3", think=True)
    backend.chat("s", "p")               # default → think on
    backend.chat("s", "p", think=False)  # published text → forced off
    backend.chat("s", "p", think=True)   # forced on
    assert seen == [True, False, True]
    assert backend.think is True  # override doesn't change the default


def test_ollama_downgrades_once_for_nonthinking_models(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        if json.get("think"):
            req = httpx.Request("POST", url)
            resp = httpx.Response(400, request=req)
            raise httpx.HTTPStatusError("no think", request=req, response=resp)
        return FakeResponse({"message": {"content": "ok"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    backend = OllamaBackend("http://x:11434", "llama3.2")
    assert backend.chat("s", "p") == "ok"
    assert calls["n"] == 2          # first with think (400), retried without
    assert backend.think is False   # downgrade is memoized...
    backend.chat("s", "p")
    assert calls["n"] == 3          # ...so the next call doesn't retry


def test_build_backend_defaults_to_thinking():
    cfg = AgentConfig()
    cfg.llm_backend = "ollama"
    backend = build_backend(cfg)
    assert isinstance(backend, OllamaBackend)
    assert backend.think is True      # on by default
    assert cfg.llm_max_tokens == 4096  # roomy budget so thinking fits
