"""Pluggable local-LLM backends.

The agent thinks with whatever is on the machine:

    ollama        Ollama's native chat API           (HTTP)
    llamacpp      llama.cpp `llama-server`, OpenAI-compatible /v1/chat/completions
    llamacpp-cli  llama.cpp `llama-cli` invoked as a subprocess
    claude-cli    the Claude Code CLI (`claude -p`), a full coding agent
    codex-cli     the Codex CLI (`codex exec`), a full coding agent
    echo          deterministic stub for development and tests

All backends implement ``chat(system, prompt) -> str``.

The CLI backends (``llamacpp-cli``, ``claude-cli``, ``codex-cli``) shell out to
a local process. Each invocation is launched as its own process-group leader
and the **whole group** is torn down when the call finishes — return, error, or
timeout alike — so a coding-agent CLI and the helpers it spawns (MCP servers,
tool subprocesses, a model runner) never linger in the background between wakes.
See ``run_cli_capture``.

Reasoning ("thinking") models — Qwen3, DeepSeek-R1, and the like — reason before
answering, and that reasoning makes the answer better. We *want* the model to
think. What we don't want is the monologue in the permanent record. So the rule
is **think freely, publish only the answer**:

* thinking is ON by default; the answer is conditioned on the chain-of-thought,
* Ollama returns the reasoning in a separate ``thinking`` field, which we read
  and deliberately discard (never journaled, never in a specimen),
* any ``<think>…</think>`` a model inlines into its answer is stripped as a
  safety net.

So the model is not nerfed — it reasons fully — and every backend still returns
only the answer: what the agent actually said or did, not how it got there.
Give reasoning models room (``LLM_MAX_TOKENS``, default 4096) since the thinking
shares the budget.

``chat`` takes a per-call ``think`` override. The wake cycle uses it to draw a
hard line: text that gets **published** (a specimen, a recorded answer) is
generated with ``think=False``, so no monologue can reach the record even if a
particular Ollama build inlines reasoning without tags; the internal tool-use
loop leaves thinking on, because there the reasoning only helps the model pick
its next action and is discarded once the action is extracted.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import re

import httpx


class LLMError(RuntimeError):
    pass


def _terminate_tree(proc: subprocess.Popen) -> None:
    """Shut the CLI down for good — the process *and* every child it spawned.

    The coding-agent CLIs (claude, codex) and llama-cli fork helpers of their
    own — MCP servers, tool subprocesses, a model runner. Killing only the
    direct child would orphan those. Each CLI is launched as its own process-
    group leader (``start_new_session=True``), so here we can signal the whole
    group: a polite terminate, then a hard kill for anything that ignores it.
    Called from a ``finally`` so the instance never lingers after a call —
    whether it returned, raised, or timed out.
    """
    if proc.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:  # pragma: no cover - Windows
                proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:  # pragma: no cover - Windows
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - stuck kernel state
            pass


def run_cli_capture(
    argv: list[str],
    *,
    label: str,
    stdin: str | None = None,
    cwd: str | None = None,
    timeout: int = 300,
) -> str:
    """Run a local-model CLI to completion and return its stdout.

    The child is its own session leader, and its whole process group is torn
    down in a ``finally`` — so a timeout, a non-zero exit, or an unexpected
    error can never leave a CLI (or the helpers it spawned) running in the
    background.
    """
    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    elif sys.platform == "win32":  # pragma: no cover - Windows
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            **popen_kwargs,
        )
    except OSError as exc:
        raise LLMError(f"{label} invocation failed: {exc}") from exc
    try:
        try:
            out, err = proc.communicate(input=stdin, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"{label} timed out after {timeout}s") from exc
        if proc.returncode != 0:
            raise LLMError(
                f"{label} exited {proc.returncode}: {(err or '').strip()[:500]}"
            )
        return out or ""
    finally:
        _terminate_tree(proc)


_THINK_BLOCK = re.compile(
    r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_OPEN_THINK = re.compile(r"^\s*<(think|thinking|reasoning)>", re.IGNORECASE)


# Tell-tale of UTF-8 text decoded as Latin-1/CP1252: a UTF-8 lead byte
# (0xC2–0xF4, seen as Â/Ã/â/… ) immediately followed by a continuation byte.
# The continuation is 0x80–0xBF, but bytes 0x80–0x9F often reach us already
# re-mapped to CP1252's printables (€ ™ – — ' ' " " …), so we match those too.
# "â€"/"â€™" — an em-dash or curly quote — are the everyday result.
_CP1252_HIGH = "\u20ac\u201a\u0192\u201e\u2026\u2020\u2021\u02c6\u2030\u0160\u2039\u0152\u017d\u2018\u2019\u201c\u201d\u2022\u2013\u2014\u02dc\u2122\u0161\u203a\u0153\u017e\u0178"
_MOJIBAKE_HINT = re.compile("[\u00c2-\u00f4][\u0080-\u00bf" + _CP1252_HIGH + "]")


def repair_mojibake(text: str) -> str:
    """Undo the classic UTF-8-decoded-as-Latin-1/CP1252 corruption.

    Some model backends (or tools behind a misconfigured locale) hand back text
    whose bytes were already mangled — an em-dash "—" arrives as "â€"", a curly
    quote as "â€™". When the tell-tale pattern is present we re-encode to the
    single-byte charset and decode as UTF-8 to recover the original. Guarded so
    it can only ever *improve* the text: the repair is kept only if it strictly
    reduces the corruption markers and introduces no replacement character,
    otherwise the input is returned untouched.
    """
    if not text:
        return text
    before = len(_MOJIBAKE_HINT.findall(text))
    if not before:
        return text
    for codec in ("cp1252", "latin-1"):
        try:
            candidate = text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        # keep the repair only if it strictly reduces the corruption markers
        # and introduces no replacement character — a legit stray "é’" that
        # can't round-trip through the codec is left exactly as it was.
        if "�" not in candidate and len(_MOJIBAKE_HINT.findall(candidate)) < before:
            return candidate
    return text


def clean_output(text: str) -> str:
    """What every backend returns: reasoning stripped and any byte-level
    mojibake healed, so only clean answer text reaches the record and the UI."""
    return strip_thinking(repair_mojibake(text))


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

    def chat(
        self, system: str, prompt: str, think: bool | None = None
    ) -> str:  # pragma: no cover - interface
        """``think`` overrides the backend default for this one call:
        True/False to force reasoning on/off, None to use the backend default.
        Backends that can't control reasoning ignore it (they still strip any
        leaked <think>)."""
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class EchoBackend(LLMBackend):
    """No model at all — replies with a stamped copy of the prompt.

    Keeps the whole carnival runnable on machines with no LLM installed.
    """

    name = "echo"

    def chat(self, system: str, prompt: str, think: bool | None = None) -> str:
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
        max_tokens: int = 4096,
        think: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        # Whether to ask the model to reason. Auto-downgraded (once) to False
        # if the server says this model can't think, so non-reasoning models
        # don't pay the retry on every call.
        self.think = think

    def describe(self) -> str:
        return f"ollama:{self.model} @ {self.base_url}"

    def _post(self, payload: dict):
        resp = httpx.post(
            f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    def chat(self, system: str, prompt: str, think: bool | None = None) -> str:
        # Per-call override wins; otherwise the backend default. Text that gets
        # published (a specimen, a recorded answer) is generated with think
        # off, so reasoning never lands in the record even if this Ollama
        # inlines it. The tool loop leaves it on: reasoning helps pick the
        # action, and only the action is kept.
        want_think = self.think if think is None else think
        payload = {
            "model": self.model,
            "stream": False,
            "think": want_think,
            "options": {"num_predict": self.max_tokens},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            data = self._post(payload)
        except httpx.HTTPStatusError as exc:
            # This model doesn't support thinking: downgrade for the rest of
            # this process and retry once without the flag.
            if (
                exc.response is not None
                and exc.response.status_code == 400
                and payload.get("think")
            ):
                self.think = False
                payload["think"] = False
                try:
                    data = self._post(payload)
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
        # `data["message"]["thinking"]` holds the reasoning — intentionally
        # ignored. Strip any <think> a model inlined into content anyway.
        return clean_output(content)


class LlamaCppServerBackend(LLMBackend):
    """llama.cpp `llama-server` speaks the OpenAI chat-completions dialect."""

    name = "llamacpp"

    def __init__(self, base_url: str, timeout: int = 300, max_tokens: int = 4096):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens

    def describe(self) -> str:
        return f"llama.cpp server @ {self.base_url}"

    def chat(self, system: str, prompt: str, think: bool | None = None) -> str:
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
            return clean_output(str(data["choices"][0]["message"]["content"]))
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected llama.cpp response shape: {data}") from exc


class LlamaCppCliBackend(LLMBackend):
    """Drive llama.cpp entirely from the command line — no server process."""

    name = "llamacpp-cli"

    def __init__(self, binary: str, model_path: str, timeout: int = 300, max_tokens: int = 4096):
        if not model_path:
            raise LLMError("llamacpp-cli backend needs LLAMACPP_MODEL_PATH")
        self.binary = binary
        self.model_path = model_path
        self.timeout = timeout
        self.max_tokens = max_tokens

    def describe(self) -> str:
        return f"llama-cli {self.model_path}"

    def chat(self, system: str, prompt: str, think: bool | None = None) -> str:
        full_prompt = f"{system.strip()}\n\n{prompt.strip()}\n"
        cmd = [
            self.binary,
            "-m", self.model_path,
            "-p", full_prompt,
            "-n", str(self.max_tokens),
            "--simple-io",
            "--no-display-prompt",
        ]
        out = run_cli_capture(cmd, label="llama-cli", timeout=self.timeout)
        return clean_output(out)


class CliAgentBackend(LLMBackend):
    """Drive a coding-agent CLI (Claude Code or Codex) in one-shot print mode.

    These aren't just text models — the CLI can read files, run tools, and act
    in the agent's workspace on its own. We hand it the prompt and take its
    final printed answer; any reasoning it prints is stripped like everywhere
    else.
    """

    def __init__(
        self,
        name: str,
        argv: list[str],
        use_stdin: bool,
        cwd: str = "",
        timeout: int = 300,
        extra: str = "",
    ):
        self.name = name
        self._argv = argv
        self.use_stdin = use_stdin
        self.cwd = cwd
        self.timeout = timeout
        self.extra = extra

    def describe(self) -> str:
        return self.name

    def chat(self, system: str, prompt: str, think: bool | None = None) -> str:
        full = f"{system.strip()}\n\n{prompt.strip()}\n"
        argv = list(self._argv)
        if self.extra:
            argv += self.extra.split()
        stdin: str | None = None
        if self.use_stdin:
            stdin = full
        else:
            argv = [a.replace("{prompt}", full) for a in argv]
        workdir = self.cwd if self.cwd and os.path.isdir(self.cwd) else None
        out = run_cli_capture(
            argv,
            label=self.name,
            stdin=stdin,
            cwd=workdir,
            timeout=self.timeout,
        )
        return clean_output(out)


def build_backend(cfg) -> LLMBackend:
    """Construct the backend named by AgentConfig.llm_backend."""
    if cfg.llm_backend == "ollama":
        return OllamaBackend(
            cfg.ollama_url,
            cfg.ollama_model,
            cfg.llm_timeout_seconds,
            cfg.llm_max_tokens,
            think=getattr(cfg, "llm_think", True),
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
    if cfg.llm_backend == "claude-cli":
        # `claude -p` reads the prompt on stdin and prints the reply.
        return CliAgentBackend(
            "claude-cli",
            [cfg.claude_bin, "-p"],
            use_stdin=True,
            cwd=str(getattr(cfg, "home", "") or ""),
            timeout=cfg.llm_timeout_seconds,
            extra=cfg.cli_extra_args,
        )
    if cfg.llm_backend == "codex-cli":
        # `codex exec <prompt>` runs non-interactively and prints the result.
        return CliAgentBackend(
            "codex-cli",
            [cfg.codex_bin, "exec", "{prompt}"],
            use_stdin=False,
            cwd=str(getattr(cfg, "home", "") or ""),
            timeout=cfg.llm_timeout_seconds,
            extra=cfg.cli_extra_args,
        )
    return EchoBackend()
