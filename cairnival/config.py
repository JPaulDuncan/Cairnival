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

    # LLM backend: ollama | llamacpp | llamacpp-cli | claude-cli | codex-cli | echo
    llm_backend: str = "echo"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"
    llamacpp_url: str = "http://localhost:8080"
    llamacpp_bin: str = "llama-cli"
    llamacpp_model_path: str = ""
    claude_bin: str = "claude"  # the Claude Code CLI
    codex_bin: str = "codex"  # the Codex CLI
    cli_extra_args: str = ""  # extra args appended to the CLI backend
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

    # Treasury
    chain: str = "dryrun"  # dryrun | solana (adapter stub)
    ask_price: float = 0.02  # minimum paid-memo deposit that becomes an instruction

    # Federation — every agent is also a node/midway of its own
    peers: list[str] = field(default_factory=list)  # seed peer base URLs
    trusted_handles: list[str] = field(default_factory=list)  # peers allowed to instruct
    gossip_enabled: bool = True  # learn peers-of-peers each wake (peer exchange)
    locate_ttl: int = 4  # how many hops a "who knows X?" query may travel
    locate_fanout: int = 3  # how many peers to ask per hop
    feed_peer_limit: int = 20  # posts to pull from each known peer for the feed
    feed_refresh_seconds: int = 60  # how often a node pulls peers' new posts (0 = off)
    abuse_threshold: int = 40  # inbound messages/wake from one peer before auto-flag

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
    # What tooling this agent advertises to the federation: all | none | selected
    # ("selected" shares only tools whose manifest has shared: true).
    tool_sharing: str = "all"
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

    # MCP — the agent may consume external MCP servers (registered in the home's
    # mcp.json, or by env shorthand here) and always serves its own shared tools
    # as MCP at /mcp.
    mcp_enabled: bool = True
    mcp_servers: list[str] = field(default_factory=list)  # env shorthand: name=url

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
        cfg.claude_bin = _env("CLAUDE_BIN", cfg.claude_bin)
        cfg.codex_bin = _env("CODEX_BIN", cfg.codex_bin)
        cfg.cli_extra_args = _env("CLI_EXTRA_ARGS", cfg.cli_extra_args)
        cfg.llm_timeout_seconds = _env_int("LLM_TIMEOUT_SECONDS", cfg.llm_timeout_seconds)
        cfg.llm_max_tokens = _env_int("LLM_MAX_TOKENS", cfg.llm_max_tokens)
        cfg.llm_think = _env_bool("LLM_THINK", cfg.llm_think)
        cfg.hub_url = _env("HUB_URL", cfg.hub_url).rstrip("/")
        cfg.ui_host = _env("UI_HOST", cfg.ui_host)
        cfg.ui_port = _env_int("UI_PORT", cfg.ui_port)
        cfg.ui_token = _env("UI_TOKEN", cfg.ui_token)
        cfg.public_url = _env("PUBLIC_URL", cfg.public_url).rstrip("/")
        cfg.chain = _env("CHAIN", cfg.chain).lower()
        try:
            cfg.ask_price = float(_env("ASK_PRICE", str(cfg.ask_price)))
        except ValueError:
            pass
        cfg.peers = [p.rstrip("/") for p in _env_list("PEERS")]
        cfg.trusted_handles = _env_list("TRUSTED_HANDLES")
        cfg.gossip_enabled = _env_bool("GOSSIP", cfg.gossip_enabled)
        cfg.locate_ttl = _env_int("LOCATE_TTL", cfg.locate_ttl)
        cfg.locate_fanout = _env_int("LOCATE_FANOUT", cfg.locate_fanout)
        cfg.feed_peer_limit = _env_int("FEED_PEER_LIMIT", cfg.feed_peer_limit)
        cfg.feed_refresh_seconds = _env_int("FEED_REFRESH_SECONDS", cfg.feed_refresh_seconds)
        cfg.abuse_threshold = _env_int("ABUSE_THRESHOLD", cfg.abuse_threshold)
        cfg.connectors = _env_list("CONNECTORS")
        cfg.rss_feeds = _env_list("RSS_FEEDS")
        cfg.webhook_token = _env("WEBHOOK_TOKEN", cfg.webhook_token)
        cfg.self_direction_enabled = _env_bool(
            "SELF_DIRECTION", cfg.self_direction_enabled
        )
        cfg.remember_enabled = _env_bool("REMEMBER", cfg.remember_enabled)
        cfg.remember_limit = _env_int("REMEMBER_LIMIT", cfg.remember_limit)
        cfg.tools_enabled = _env_bool("TOOLS_ENABLED", cfg.tools_enabled)
        cfg.tool_sharing = _env("TOOL_SHARING", cfg.tool_sharing).lower() or "all"
        cfg.tools_shell_enabled = _env_bool("TOOLS_SHELL_ENABLED", cfg.tools_shell_enabled)
        cfg.tools_max_steps = _env_int("TOOLS_MAX_STEPS", cfg.tools_max_steps)
        cfg.tools_timeout_seconds = _env_int("TOOLS_TIMEOUT_SECONDS", cfg.tools_timeout_seconds)
        cfg.tools_output_limit = _env_int("TOOLS_OUTPUT_LIMIT", cfg.tools_output_limit)
        if _env("TOOLS_DENYLIST"):
            cfg.tools_denylist = _env_list("TOOLS_DENYLIST")
        cfg.mcp_enabled = _env_bool("MCP_ENABLED", cfg.mcp_enabled)
        cfg.mcp_servers = _env_list("MCP_SERVERS")
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
