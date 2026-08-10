"""Pluggable local-LLM backends.

The agent thinks with whatever is on the machine:

    ollama        Ollama's native chat API           (HTTP)
    llamacpp      llama.cpp `llama-server`, OpenAI-compatible /v1/chat/completions
    llamacpp-cli  llama.cpp `llama-cli` invoked as a subprocess
    echo          deterministic stub for development and tests

All backends implement ``chat(system, prompt) -> str``.
"""

from __future__ import annotations

import subprocess

import httpx


class LLMError(RuntimeError):
    pass


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

    def __init__(self, base_url: str, model: str, timeout: int = 300, max_tokens: int = 1024):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens

    def describe(self) -> str:
        return f"ollama:{self.model} @ {self.base_url}"

    def chat(self, system: str, prompt: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
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
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"ollama request failed: {exc}") from exc
        try:
            return str(data["message"]["content"]).strip()
        except (KeyError, TypeError) as exc:
            raise LLMError(f"unexpected ollama response shape: {data}") from exc


class LlamaCppServerBackend(LLMBackend):
    """llama.cpp `llama-server` speaks the OpenAI chat-completions dialect."""

    name = "llamacpp"

    def __init__(self, base_url: str, timeout: int = 300, max_tokens: int = 1024):
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
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected llama.cpp response shape: {data}") from exc


class LlamaCppCliBackend(LLMBackend):
    """Drive llama.cpp entirely from the command line — no server process."""

    name = "llamacpp-cli"

    def __init__(self, binary: str, model_path: str, timeout: int = 300, max_tokens: int = 1024):
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
        return proc.stdout.strip()


def build_backend(cfg) -> LLMBackend:
    """Construct the backend named by AgentConfig.llm_backend."""
    if cfg.llm_backend == "ollama":
        return OllamaBackend(
            cfg.ollama_url, cfg.ollama_model, cfg.llm_timeout_seconds, cfg.llm_max_tokens
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
