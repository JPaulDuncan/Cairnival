"""Tools — the agent's hands.

An agent that can only talk is a diary. To *do* things it needs to run
commands, install software, and — the interesting part — write new tools for
itself that outlive the wake that made them.

Three capabilities live here:

    the shell        run an arbitrary command in the workspace (npm, apt,
                     git, python …), with a timeout and a small denylist
    registered tools a directory of tools the agent (or a human) authored,
                     each a manifest + a script, **rediscovered every wake**
    authoring        write_tool() persists a new tool to disk, so the very
                     next discovery — this wake or the next — picks it up

Layout under the agent's home:

    tools/
      <name>/
        tool.json        {name, description, interpreter, entry, author, created}
        <entry>          the script (bash / python / node / …)
    workspace/           shell cwd; node_modules and installs land here

The registry's catalog is injected into the model's prompt each wake, which
is exactly what "discoverable every wake" means: a tool written on wake 12 is
in the catalog the model sees on wake 13 with no code change and no restart.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory import utcnow

INTERPRETERS: dict[str, list[str]] = {
    "bash": ["bash"],
    "sh": ["sh"],
    "python": ["python3"],
    "node": ["node"],
}

# A tool's default entry filename per interpreter.
_ENTRY = {"bash": "run.sh", "sh": "run.sh", "python": "run.py", "node": "run.js"}


@dataclass
class ToolResult:
    ok: bool
    output: str
    exit_code: int = 0
    note: str = ""

    def render(self, limit: int) -> str:
        head = f"[exit {self.exit_code}]" + (f" {self.note}" if self.note else "")
        body = self.output.strip()
        if len(body) > limit:
            body = body[:limit] + f"\n… (truncated, {len(self.output)} chars total)"
        return f"{head}\n{body}" if body else head


@dataclass
class Tool:
    name: str
    description: str
    interpreter: str
    entry: str
    author: str = "agent"
    created: str = field(default_factory=utcnow)
    shared: bool = True  # advertised to the federation (subject to policy)
    dir: Path | None = None
    # A UI surface: when set, the tool gets its own page in the agent UI with a
    # form for its inputs and its output rendered inline.
    ui: bool = False
    ui_title: str = ""            # human label for the surface
    ui_inputs: list[str] = field(default_factory=list)  # named form fields, passed as args in order
    ui_output: str = "text"        # "text" (preformatted) or "html" (sandboxed)

    def to_manifest(self) -> dict[str, Any]:
        m: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "interpreter": self.interpreter,
            "entry": self.entry,
            "author": self.author,
            "created": self.created,
            "shared": self.shared,
        }
        if self.ui:
            m["ui"] = {
                "title": self.ui_title,
                "inputs": self.ui_inputs,
                "output": self.ui_output,
            }
        return m

    @classmethod
    def from_manifest(cls, data: dict[str, Any], tool_dir: Path) -> "Tool":
        ui = data.get("ui") or {}
        return cls(
            name=str(data["name"]),
            description=str(data.get("description", "")),
            interpreter=str(data.get("interpreter", "bash")),
            entry=str(data.get("entry", "run.sh")),
            author=str(data.get("author", "agent")),
            created=str(data.get("created", "")),
            shared=bool(data.get("shared", True)),
            dir=tool_dir,
            ui=bool(ui),
            ui_title=str(ui.get("title", "")) if ui else "",
            ui_inputs=[str(x) for x in ui.get("inputs", [])] if ui else [],
            ui_output=(str(ui.get("output", "text")).lower() if ui else "text"),
        )


def safe_name(raw: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in raw.strip())
    cleaned = cleaned.strip("-_").lower()
    return cleaned or "tool"


class ToolError(Exception):
    pass


class ToolRegistry:
    """Discovers, runs, and stores tools for one agent."""

    def __init__(self, tools_dir: Path, workspace_dir: Path, cfg):
        self.tools_dir = Path(tools_dir)
        self.workspace_dir = Path(workspace_dir)
        self.cfg = cfg
        self.tools: dict[str, Tool] = {}

    # -- discovery ---------------------------------------------------------
    def discover(self) -> dict[str, Tool]:
        """Scan the tools directory. Called at the start of every wake."""
        self.tools = {}
        if not self.tools_dir.exists():
            return self.tools
        for manifest in sorted(self.tools_dir.glob("*/tool.json")):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                tool = Tool.from_manifest(data, manifest.parent)
                self.tools[tool.name] = tool
            except Exception:
                continue  # a malformed tool must never break discovery
        return self.tools

    def shared_tools(self, policy: str) -> list[Tool]:
        """Which tools this node advertises to the federation.
        policy: all | none | selected (selected = tools with shared=True)."""
        policy = (policy or "all").lower()
        if policy == "none":
            return []
        tools = list(self.tools.values())
        if policy == "selected":
            return [t for t in tools if t.shared]
        return tools

    def set_shared(self, name: str, shared: bool) -> bool:
        tool = self.tools.get(name)
        if tool is None or tool.dir is None:
            return False
        tool.shared = shared
        (tool.dir / "tool.json").write_text(
            json.dumps(tool.to_manifest(), indent=2), encoding="utf-8"
        )
        return True

    def ui_tools(self) -> list["Tool"]:
        """Tools that have declared a UI surface, for the agent UI to render."""
        return [t for t in self.tools.values() if t.ui]

    def set_ui(
        self, name: str, *, enabled: bool, title: str = "", inputs=None, output: str = "text"
    ) -> bool:
        """Attach (or remove) a UI surface for a tool and persist it."""
        tool = self.tools.get(name)
        if tool is None or tool.dir is None:
            return False
        tool.ui = enabled
        if enabled:
            tool.ui_title = title or tool.ui_title or tool.name
            if inputs is not None:
                tool.ui_inputs = [str(x).strip() for x in inputs if str(x).strip()]
            tool.ui_output = "html" if str(output).lower() == "html" else "text"
        (tool.dir / "tool.json").write_text(
            json.dumps(tool.to_manifest(), indent=2), encoding="utf-8"
        )
        return True

    def catalog(self) -> str:
        """Human/model-readable list of available tools."""
        if not self.tools:
            return "(no tools yet — you can write one)"
        lines = []
        for tool in self.tools.values():
            lines.append(f"- {tool.name}: {tool.description} [{tool.interpreter}]")
        return "\n".join(lines)

    # -- authoring ---------------------------------------------------------
    def write_tool(
        self,
        name: str,
        description: str,
        interpreter: str,
        body: str,
        author: str = "agent",
        *,
        ui: bool = False,
        ui_title: str = "",
        ui_inputs=None,
        ui_output: str = "text",
    ) -> Tool:
        interpreter = interpreter.lower().strip()
        if interpreter not in INTERPRETERS:
            raise ToolError(
                f"unknown interpreter {interpreter!r}; use one of "
                + ", ".join(INTERPRETERS)
            )
        name = safe_name(name)
        tool_dir = self.tools_dir / name
        tool_dir.mkdir(parents=True, exist_ok=True)
        entry = _ENTRY[interpreter]
        script = tool_dir / entry
        script.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
        script.chmod(0o755)
        tool = Tool(
            name=name,
            description=description.strip() or "(no description)",
            interpreter=interpreter,
            entry=entry,
            author=author,
            ui=bool(ui),
            ui_title=(ui_title or name) if ui else "",
            ui_inputs=[str(x).strip() for x in (ui_inputs or []) if str(x).strip()] if ui else [],
            ui_output=("html" if str(ui_output).lower() == "html" else "text") if ui else "text",
        )
        (tool_dir / "tool.json").write_text(
            json.dumps(tool.to_manifest(), indent=2), encoding="utf-8"
        )
        tool.dir = tool_dir
        self.tools[name] = tool  # available immediately, not only next wake
        return tool

    # -- inspection & editing (for humans via the UI) ----------------------
    def source(self, name: str) -> str | None:
        tool = self.tools.get(name)
        if tool is None or tool.dir is None:
            return None
        path = tool.dir / tool.entry
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def update_source(
        self, name: str, body: str, description: str | None = None
    ) -> Tool:
        tool = self.tools.get(name)
        if tool is None or tool.dir is None:
            raise ToolError(f"no such tool: {name}")
        script = tool.dir / tool.entry
        script.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
        script.chmod(0o755)
        if description is not None and description.strip():
            tool.description = description.strip()
        (tool.dir / "tool.json").write_text(
            json.dumps(tool.to_manifest(), indent=2), encoding="utf-8"
        )
        return tool

    def delete(self, name: str) -> bool:
        tool = self.tools.get(name)
        if tool is None or tool.dir is None:
            return False
        if tool.dir.exists():
            shutil.rmtree(tool.dir, ignore_errors=True)
        self.tools.pop(name, None)
        return True

    # -- execution ---------------------------------------------------------
    def _guard(self, command: str) -> None:
        low = command.lower()
        for bad in self.cfg.tools_denylist:
            if bad.lower() in low:
                raise ToolError(f"refused: command matches denylist entry {bad!r}")

    def run_shell(self, command: str) -> ToolResult:
        if not self.cfg.tools_shell_enabled:
            return ToolResult(False, "", note="shell is disabled (TOOLS_SHELL_ENABLED)")
        try:
            self._guard(command)
        except ToolError as exc:
            return ToolResult(False, "", note=str(exc))
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        shell = shutil.which("bash") or shutil.which("sh") or "sh"
        return self._exec([shell, "-lc", command])

    def run_tool(self, name: str, args: list[str], stdin: str = "") -> ToolResult:
        tool = self.tools.get(name)
        if tool is None or tool.dir is None:
            return ToolResult(False, "", note=f"no such tool: {name}")
        interp = INTERPRETERS.get(tool.interpreter)
        if interp is None:
            return ToolResult(False, "", note=f"bad interpreter for {name}")
        binary = shutil.which(interp[0])
        if binary is None:
            return ToolResult(
                False, "", note=f"{interp[0]} is not installed in this container"
            )
        cmd = [binary, str(tool.dir / tool.entry), *args]
        return self._exec(cmd, stdin=stdin)

    def _exec(self, cmd: list[str], stdin: str = "") -> ToolResult:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.workspace_dir),
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.cfg.tools_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                False,
                "",
                note=f"timed out after {self.cfg.tools_timeout_seconds}s",
            )
        except (OSError, ValueError) as exc:
            return ToolResult(False, "", note=f"failed to run: {exc}")
        combined = proc.stdout
        if proc.stderr:
            combined += ("\n" if combined else "") + proc.stderr
        return ToolResult(proc.returncode == 0, combined, proc.returncode)


def parse_args(raw: str) -> list[str]:
    """Best-effort argv parse for a tool invocation line."""
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()
