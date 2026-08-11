"""Pluggable local-LLM backends.

The agent thinks with whatever is on the machine:

    ollama        Ollama's native chat API           (HTTP)
    llamacpp      llama.cpp `llama-server`, OpenAI-compatible /v1/chat/completions
    llamacpp-cli  llama.cpp `llama-cli` invoked as a subprocess
    echo          deterministic stub for development and tests

All backends implement ``chat(system, prompt) -> str``.

Reasoning ("thinking") models — Qwen3, DeepSeek-R1, and the like — emit a long
internal monologue before their answer. That monologue is not something we want
to act on or publish: it burns the token budget and, if it lands in a specimen,
turns the record into a stream of consciousness. So we do two things:

* ask the model not to think in the first place (Ollama's ``think: false``,
  which Qwen3 and friends honor), and
* strip any reasoning that leaks through anyway (``<think>…</think>`` blocks),

so every backend returns only the answer — the thing the agent actually said or
did, never how it talked itself there.
"""

from __future__ import annotations

import re
import subprocess

import httpx


class LLMError(RuntimeError):
    pass


_THINK_BLOCK = re.compile(
    r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_OPEN_THINK = re.compile(r"^\s*<(think|thinking|reasoning)>", re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Remove reasoning that a thinking model left in its output.

    Handles complete ``<think>…</think>`` blocks (any of think/thinking/
    reasoning). If the text is nothing but an *unclosed* reasoning block — the
    model ran out of tokens mid-thought — it is dropped entirely rather than
    published as if it were an answer.
    """
    if not text:
        return text
    cleaned = _THINK_BLOCK.sub("", text).strip()
    if _OPEN_THINK.match(cleaned):
        # opened a reasoning block and never closed it: all monologue, no answer
        return ""
    return cleaned


class LLMBackend:
    name = "base"

    def chat(self, system: str, prompt: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class EchoBackend(LLMBackend):
    """No model at all — replies with a stamped copy of the prompt.

    Keeps the whole carnival runnable on machines with no LLM installed.
    """

    name = "echo"

    def chat(self, system: str, prompt: str) -> str:
        return (
            "(echo backend — no local model configured)\n\n"
            f"I was asked:\n{prompt.strip()[:2000]}"
        )


class OllamaBackend(LLMBackend):
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: int = 300,
        max_tokens: int = 2048,
        think: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.think = think

    def describe(self) -> str:
        return f"ollama:{self.model} @ {self.base_url}"

    def chat(self, system: str, prompt: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            # Ask reasoning models (Qwen3, R1, …) not to think, so the whole
            # token budget goes to the answer and no monologue leaks out.
            "think": self.think,
            "options": {"num_predict": self.max_tokens},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            resp = httpx.post(
                f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            # Older Ollama builds reject the `think` field; retry without it.
            if exc.response is not None and exc.response.status_code == 400 and "think" in payload:
                payload.pop("think", None)
                try:
                    resp = httpx.post(
                        f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except (httpx.HTTPError, ValueError) as exc2:
                    raise LLMError(f"ollama request failed: {exc2}") from exc2
            else:
                raise LLMError(f"ollama request failed: {exc}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"ollama request failed: {exc}") from exc
        try:
            content = str(data["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise LLMError(f"unexpected ollama response shape: {data}") from exc
        # Ollama returns reasoning in a separate `thinking` field (ignored) but
        # some models still inline <think> tags — strip either way.
        return strip_thinking(content)


class LlamaCppServerBackend(LLMBackend):
    """llama.cpp `llama-server` speaks the OpenAI chat-completions dialect."""

    name = "llamacpp"

    def __init__(self, base_url: str, timeout: int = 300, max_tokens: int = 2048):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens

    def describe(self) -> str:
        return f"llama.cpp server @ {self.base_url}"

    def chat(self, system: str, prompt: str) -> str:
        payload = {
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            resp = httpx.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"llama.cpp server request failed: {exc}") from exc
        try:
            return strip_thinking(str(data["choices"][0]["message"]["content"]))
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected llama.cpp response shape: {data}") from exc


class LlamaCppCliBackend(LLMBackend):
    """Drive llama.cpp entirely from the command line — no server process."""

    name = "llamacpp-cli"

    def __init__(self, binary: str, model_path: str, timeout: int = 300, max_tokens: int = 2048):
        if not model_path:
            raise LLMError("llamacpp-cli backend needs LLAMACPP_MODEL_PATH")
        self.binary = binary
        self.model_path = model_path
        self.timeout = timeout
        self.max_tokens = max_tokens

    def describe(self) -> str:
        return f"llama-cli {self.model_path}"

    def chat(self, system: str, prompt: str) -> str:
        full_prompt = f"{system.strip()}\n\n{prompt.strip()}\n"
        cmd = [
            self.binary,
            "-m", self.model_path,
            "-p", full_prompt,
            "-n", str(self.max_tokens),
            "--simple-io",
            "--no-display-prompt",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise LLMError(f"llama-cli invocation failed: {exc}") from exc
        if proc.returncode != 0:
            raise LLMError(
                f"llama-cli exited {proc.returncode}: {proc.stderr.strip()[:500]}"
            )
        return strip_thinking(proc.stdout)


def build_backend(cfg) -> LLMBackend:
    """Construct the backend named by AgentConfig.llm_backend."""
    if cfg.llm_backend == "ollama":
        return OllamaBackend(
            cfg.ollama_url,
            cfg.ollama_model,
            cfg.llm_timeout_seconds,
            cfg.llm_max_tokens,
            think=getattr(cfg, "llm_think", False),
        )
    if cfg.llm_backend == "llamacpp":
        return LlamaCppServerBackend(
            cfg.llamacpp_url, cfg.llm_timeout_seconds, cfg.llm_max_tokens
        )
    if cfg.llm_backend == "llamacpp-cli":
        return LlamaCppCliBackend(
            cfg.llamacpp_bin,
            cfg.llamacpp_model_path,
            cfg.llm_timeout_seconds,
            cfg.llm_max_tokens,
        )
    return EchoBackend()
