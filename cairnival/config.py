"""Environment-driven configuration for agents and the hub."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw)
    except ValueError:
        return default


def _env_list(name: str) -> list[str]:
    raw = _env(name)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class AgentConfig:
    # Identity
    name: str = "rustle"
    tagline: str = "a small agent at the Cairnival"

    # Where the agent's whole world lives. The agent has no memory except
    # these files.
    home: Path = field(default_factory=lambda: Path(_env("CAIRNIVAL_HOME", "./data")))

    # Wake cadence
    wake_interval_minutes: int = 90
    wake_jitter_minutes: int = 20
    max_instructions_per_wake: int = 5

    # LLM backend: ollama | llamacpp | llamacpp-cli | echo
    llm_backend: str = "echo"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"
    llamacpp_url: str = "http://localhost:8080"
    llamacpp_bin: str = "llama-cli"
    llamacpp_model_path: str = ""
    llm_timeout_seconds: int = 300
    llm_max_tokens: int = 4096
    # Let reasoning models (Qwen3, DeepSeek-R1, …) actually reason. ON by
    # default — the answer is conditioned on the chain-of-thought, which makes
    # the agent smarter. The reasoning is used but NEVER recorded: Ollama
    # returns it in a separate field we discard, and any inline <think> block
    # is stripped. Set false only to force a model to answer without thinking.
    llm_think: bool = True

    # The Midway (central hub)
    hub_url: str = ""

    # Web UI / API
    ui_host: str = "0.0.0.0"
    ui_port: int = 8700
    ui_token: str = ""  # empty = open (dev only)
    public_url: str = ""  # how peers/hub reach this agent, e.g. http://agent-a:8700

    # Email instructions (IMAP in, SMTP out)
    email_enabled: bool = False
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    email_allowlist: list[str] = field(default_factory=list)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    # Treasury
    chain: str = "dryrun"  # dryrun | solana (adapter stub)
    ask_price: float = 0.02  # minimum paid-memo deposit that becomes an instruction

    # Federation
    peers: list[str] = field(default_factory=list)  # peer base URLs
    trusted_handles: list[str] = field(default_factory=list)  # peers allowed to instruct

    # Connectors, comma list: rss, webhook, or dotted module paths
    connectors: list[str] = field(default_factory=list)
    rss_feeds: list[str] = field(default_factory=list)
    webhook_token: str = ""

    # Self-direction. When on, each wake with spare attention the agent
    # advances a goal of its own — or dreams one up — using its tools and its
    # peers. Its pursuits persist across wakes (that is how it grows). On by
    # default: a carnival of agents that only answer their inbox is just a
    # queue; the point is that they *want* things.
    self_direction_enabled: bool = True

    # Memory across wakes. Off by default: like Cairn, the agent wakes with no
    # memory except its files, and its record is only the actions it took. On,
    # the agent may keep durable notes to itself (the ```remember``` action)
    # that are fed back into its briefing on later wakes.
    remember_enabled: bool = False
    remember_limit: int = 2000  # chars of remembered notes injected per wake

    # Tools — the agent's hands
    tools_enabled: bool = True  # run each instruction as a tool-use loop
    tools_shell_enabled: bool = True  # allow arbitrary shell (npm/apt/etc.)
    tools_max_steps: int = 4  # max tool actions per instruction
    tools_timeout_seconds: int = 120  # per command/tool invocation
    tools_output_limit: int = 4000  # chars of output fed back to the model
    tools_denylist: list[str] = field(
        default_factory=lambda: [
            "rm -rf /",
            "mkfs",
            ":(){",  # fork bomb
            "shutdown",
            "reboot",
            "dd if=",
            "> /dev/sd",
        ]
    )

    @classmethod
    def from_env(cls) -> "AgentConfig":
        cfg = cls()
        cfg.name = _env("AGENT_NAME", cfg.name)
        cfg.tagline = _env("AGENT_TAGLINE", cfg.tagline)
        cfg.home = Path(_env("CAIRNIVAL_HOME", str(cfg.home)))
        cfg.wake_interval_minutes = _env_int("WAKE_INTERVAL_MINUTES", cfg.wake_interval_minutes)
        cfg.wake_jitter_minutes = _env_int("WAKE_JITTER_MINUTES", cfg.wake_jitter_minutes)
        cfg.max_instructions_per_wake = _env_int(
            "MAX_INSTRUCTIONS_PER_WAKE", cfg.max_instructions_per_wake
        )
        cfg.llm_backend = _env("LLM_BACKEND", cfg.llm_backend).lower()
        cfg.ollama_url = _env("OLLAMA_URL", cfg.ollama_url)
        cfg.ollama_model = _env("OLLAMA_MODEL", cfg.ollama_model)
        cfg.llamacpp_url = _env("LLAMACPP_URL", cfg.llamacpp_url)
        cfg.llamacpp_bin = _env("LLAMACPP_BIN", cfg.llamacpp_bin)
        cfg.llamacpp_model_path = _env("LLAMACPP_MODEL_PATH", cfg.llamacpp_model_path)
        cfg.llm_timeout_seconds = _env_int("LLM_TIMEOUT_SECONDS", cfg.llm_timeout_seconds)
        cfg.llm_max_tokens = _env_int("LLM_MAX_TOKENS", cfg.llm_max_tokens)
        cfg.llm_think = _env_bool("LLM_THINK", cfg.llm_think)
        cfg.hub_url = _env("HUB_URL", cfg.hub_url).rstrip("/")
        cfg.ui_host = _env("UI_HOST", cfg.ui_host)
        cfg.ui_port = _env_int("UI_PORT", cfg.ui_port)
        cfg.ui_token = _env("UI_TOKEN", cfg.ui_token)
        cfg.public_url = _env("PUBLIC_URL", cfg.public_url).rstrip("/")
        cfg.email_enabled = _env_bool("EMAIL_ENABLED", cfg.email_enabled)
        cfg.imap_host = _env("IMAP_HOST", cfg.imap_host)
        cfg.imap_port = _env_int("IMAP_PORT", cfg.imap_port)
        cfg.imap_user = _env("IMAP_USER", cfg.imap_user)
        cfg.imap_password = _env("IMAP_PASSWORD", cfg.imap_password)
        cfg.email_allowlist = [a.lower() for a in _env_list("EMAIL_ALLOWLIST")]
        cfg.smtp_host = _env("SMTP_HOST", cfg.smtp_host)
        cfg.smtp_port = _env_int("SMTP_PORT", cfg.smtp_port)
        cfg.smtp_user = _env("SMTP_USER", cfg.smtp_user)
        cfg.smtp_password = _env("SMTP_PASSWORD", cfg.smtp_password)
        cfg.smtp_from = _env("SMTP_FROM", cfg.smtp_from)
        cfg.chain = _env("CHAIN", cfg.chain).lower()
        try:
            cfg.ask_price = float(_env("ASK_PRICE", str(cfg.ask_price)))
        except ValueError:
            pass
        cfg.peers = [p.rstrip("/") for p in _env_list("PEERS")]
        cfg.trusted_handles = _env_list("TRUSTED_HANDLES")
        cfg.connectors = _env_list("CONNECTORS")
        cfg.rss_feeds = _env_list("RSS_FEEDS")
        cfg.webhook_token = _env("WEBHOOK_TOKEN", cfg.webhook_token)
        cfg.self_direction_enabled = _env_bool(
            "SELF_DIRECTION", cfg.self_direction_enabled
        )
        cfg.remember_enabled = _env_bool("REMEMBER", cfg.remember_enabled)
        cfg.remember_limit = _env_int("REMEMBER_LIMIT", cfg.remember_limit)
        cfg.tools_enabled = _env_bool("TOOLS_ENABLED", cfg.tools_enabled)
        cfg.tools_shell_enabled = _env_bool("TOOLS_SHELL_ENABLED", cfg.tools_shell_enabled)
        cfg.tools_max_steps = _env_int("TOOLS_MAX_STEPS", cfg.tools_max_steps)
        cfg.tools_timeout_seconds = _env_int("TOOLS_TIMEOUT_SECONDS", cfg.tools_timeout_seconds)
        cfg.tools_output_limit = _env_int("TOOLS_OUTPUT_LIMIT", cfg.tools_output_limit)
        if _env("TOOLS_DENYLIST"):
            cfg.tools_denylist = _env_list("TOOLS_DENYLIST")
        return cfg


@dataclass
class HubConfig:
    name: str = "The Midway"
    home: Path = field(default_factory=lambda: Path(_env("CAIRNIVAL_HOME", "./data")) / "hub")
    host: str = "0.0.0.0"
    port: int = 8600
    public_url: str = ""

    @classmethod
    def from_env(cls) -> "HubConfig":
        cfg = cls()
        cfg.name = _env("HUB_NAME", cfg.name)
        cfg.home = Path(_env("CAIRNIVAL_HOME", str(Path("./data")))) / "hub"
        cfg.host = _env("HUB_HOST", cfg.host)
        cfg.port = _env_int("HUB_PORT", cfg.port)
        cfg.public_url = _env("HUB_PUBLIC_URL", cfg.public_url).rstrip("/")
        return cfg
