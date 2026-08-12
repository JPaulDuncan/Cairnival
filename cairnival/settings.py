"""Per-agent configuration that a human can edit through the attach UI.

Environment variables remain the base layer (and the only way to set
process-level things like the data directory and the UI port). Everything a
human might want to tune while the agent is running lives in a second layer:
``config.json`` inside the agent's home. The UI writes that file; it wins
over the environment; the wake cycle and the web app reload it on every use,
so a change made in the browser takes effect on the very next wake — no
restart, no rebuild.

This split is also what makes non-Docker operation pleasant: a cron job or a
service only needs CAIRNIVAL_HOME in its environment, and every other knob
travels with the data directory.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import AgentConfig

CONFIG_FILENAME = "config.json"

# Fields a human may NOT edit through the UI: they define where/how the
# process itself runs and must come from the environment.
PROCESS_ONLY = ("home", "ui_host", "ui_port")


@dataclass
class Field:
    name: str  # AgentConfig attribute
    label: str
    kind: str = "str"  # str | int | float | bool | list | secret | choice
    choices: tuple[str, ...] = ()
    help: str = ""


@dataclass
class Group:
    title: str
    fields: list[Field] = field(default_factory=list)


GROUPS: list[Group] = [
    Group(
        "Identity",
        [
            Field(
                "name",
                "Handle",
                help=(
                    "Changing the handle after the first hello registers a new "
                    "agent on the Midway; the old handle keeps its history."
                ),
            ),
            Field("tagline", "Tagline"),
        ],
    ),
    Group(
        "Cadence",
        [
            Field("wake_interval_minutes", "Wake interval (minutes)", "int"),
            Field("wake_jitter_minutes", "Jitter (± minutes)", "int"),
            Field("max_instructions_per_wake", "Max instructions per wake", "int"),
        ],
    ),
    Group(
        "Mind — the local LLM",
        [
            Field(
                "llm_backend",
                "Backend",
                "choice",
                ("echo", "ollama", "llamacpp", "llamacpp-cli", "claude-cli", "codex-cli"),
                help=(
                    "echo needs no model; llamacpp means a running llama-server; "
                    "claude-cli/codex-cli drive a coding-agent CLI as the mind"
                ),
            ),
            Field("ollama_url", "Ollama URL"),
            Field("ollama_model", "Ollama model"),
            Field("llamacpp_url", "llama-server URL"),
            Field("llamacpp_bin", "llama-cli binary"),
            Field("llamacpp_model_path", "GGUF model path (CLI backend)"),
            Field(
                "claude_bin",
                "Claude CLI path",
                help="the `claude` executable, for LLM_BACKEND=claude-cli",
            ),
            Field(
                "codex_bin",
                "Codex CLI path",
                help="the `codex` executable, for LLM_BACKEND=codex-cli",
            ),
            Field(
                "cli_extra_args",
                "CLI extra args",
                help="extra flags appended to the claude/codex command",
            ),
            Field("llm_timeout_seconds", "Timeout (seconds)", "int"),
            Field("llm_max_tokens", "Max tokens", "int"),
            Field(
                "llm_think",
                "Let reasoning models think",
                "bool",
                help=(
                    "on (default): Qwen3/R1-style models reason before answering "
                    "— the answer is better for it. The reasoning is used but "
                    "never recorded (kept out of the journal and specimens). "
                    "Give thinking room via Max tokens."
                ),
            ),
        ],
    ),
    Group(
        "The Midway",
        [
            Field("hub_url", "Hub URL", help="empty = publish nowhere, run alone"),
            Field("public_url", "This agent's public URL (how peers reach it)"),
        ],
    ),
    Group(
        "Treasury",
        [
            Field("chain", "Chain", "choice", ("dryrun", "solana")),
            Field(
                "ask_price",
                "Ask price",
                "float",
                help="minimum deposit for a memo to become a paid question",
            ),
        ],
    ),
    Group(
        "Federation",
        [
            Field("peers", "Peer URLs", "list"),
            Field(
                "trusted_handles",
                "Trusted handles",
                "list",
                help="peers allowed to put work in this agent's inbox",
            ),
        ],
    ),
    Group(
        "Connectors",
        [
            Field(
                "connectors",
                "Enabled connectors",
                "list",
                help="rss, webhook, or dotted module paths",
            ),
            Field("rss_feeds", "RSS feeds", "list"),
            Field("webhook_token", "Webhook token", "secret"),
        ],
    ),
    Group(
        "Self-direction",
        [
            Field(
                "self_direction_enabled",
                "Let this agent pursue its own goals",
                "bool",
                help=(
                    "on (default): each wake with spare attention it advances a "
                    "pursuit of its own or dreams one up, using its tools and "
                    "peers. off: it only works what others give it."
                ),
            ),
        ],
    ),
    Group(
        "Memory",
        [
            Field(
                "remember_enabled",
                "Let this agent remember across wakes",
                "bool",
                help=(
                    "off (default): wakes with no memory but its files; only "
                    "actions are recorded. on: the agent may keep durable notes "
                    "to itself that are fed back into later wakes."
                ),
            ),
            Field("remember_limit", "Memory injected per wake (chars)", "int"),
        ],
    ),
    Group(
        "Tools — the agent's hands",
        [
            Field(
                "tools_enabled",
                "Run instructions as a tool-use loop",
                "bool",
                help="off = a single reply per instruction, no shell or tools",
            ),
            Field(
                "tools_shell_enabled",
                "Allow the shell (npm, pip, apt, git …)",
                "bool",
                help="commands run in the workspace and persist in this container",
            ),
            Field(
                "tool_sharing",
                "Share tooling with the federation",
                "choice",
                ("all", "selected", "none"),
                help="what other agents can see/ask for: all your tools, only "
                "ones you mark shared, or none",
            ),
            Field(
                "mcp_enabled",
                "MCP (serve own tools + consume MCP servers)",
                "bool",
                help="serves shared tools at /mcp and lets the agent call "
                "registered MCP servers; manage servers on the MCP page",
            ),
            Field("tools_max_steps", "Max tool actions per instruction", "int"),
            Field("tools_timeout_seconds", "Per-command timeout (seconds)", "int"),
            Field("tools_output_limit", "Output fed back to the model (chars)", "int"),
            Field(
                "tools_denylist",
                "Command denylist",
                "list",
                help="substrings that are refused before running",
            ),
        ],
    ),
    Group(
        "Attach UI",
        [
            Field(
                "ui_token",
                "UI token",
                "secret",
                help="required for every mutation once set; leave blank to keep",
            ),
        ],
    ),
]

ALL_FIELDS: dict[str, Field] = {f.name: f for g in GROUPS for f in g.fields}
SECRET_CLEAR_SENTINEL = "-"


def overrides_path(home: Path) -> Path:
    return Path(home) / CONFIG_FILENAME


def read_overrides(home: Path) -> dict:
    path = overrides_path(home)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def write_overrides(home: Path, data: dict) -> None:
    path = overrides_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    path.chmod(0o600)  # may hold IMAP/SMTP passwords and tokens


def apply_overrides(base: AgentConfig, data: dict) -> AgentConfig:
    cfg = copy.deepcopy(base)
    for key, value in data.items():
        if key in PROCESS_ONLY or key not in ALL_FIELDS:
            continue
        spec = ALL_FIELDS[key]
        try:
            if spec.kind == "int":
                setattr(cfg, key, int(value))
            elif spec.kind == "float":
                setattr(cfg, key, float(value))
            elif spec.kind == "bool":
                setattr(cfg, key, bool(value))
            elif spec.kind == "list":
                setattr(cfg, key, [str(v) for v in value])
            else:
                setattr(cfg, key, str(value))
        except (TypeError, ValueError):
            continue  # a bad stored value must never wedge the agent
    return cfg


def load_agent_config(base: AgentConfig | None = None) -> AgentConfig:
    """The one true way to get an agent's current configuration:
    environment first, then whatever the UI saved in config.json."""
    base = base or AgentConfig.from_env()
    return apply_overrides(base, read_overrides(base.home))


def coerce_form_value(spec: Field, raw: str, current) -> tuple[bool, object]:
    """Turn one form input into a stored override value.

    Returns (store, value). ``store=False`` means leave the existing
    override (or lack of one) untouched — used for blank secrets.
    """
    raw = raw.strip()
    if spec.kind == "secret":
        if not raw:
            return False, None
        if raw == SECRET_CLEAR_SENTINEL:
            return True, ""
        return True, raw
    if spec.kind == "bool":
        return True, raw.lower() in ("1", "true", "yes", "on")
    if spec.kind == "int":
        try:
            return True, int(raw)
        except ValueError:
            return False, None
    if spec.kind == "float":
        try:
            return True, float(raw)
        except ValueError:
            return False, None
    if spec.kind == "list":
        return True, [item.strip() for item in raw.split(",") if item.strip()]
    if spec.kind == "choice":
        return (True, raw) if raw in spec.choices else (False, None)
    return True, raw


def apply_form(home: Path, form: dict[str, str]) -> dict:
    """Merge a submitted settings form into config.json and return it."""
    data = read_overrides(home)
    for name, spec in ALL_FIELDS.items():
        if spec.kind == "bool":
            # checkboxes: present when ticked, absent when not
            data[name] = form.get(name, "").lower() in ("1", "true", "yes", "on")
            continue
        if name not in form:
            continue
        store, value = coerce_form_value(spec, form[name], data.get(name))
        if store:
            data[name] = value
    write_overrides(home, data)
    return data
