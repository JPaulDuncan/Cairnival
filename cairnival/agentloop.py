"""The tool-use loop: how an instruction gets *done*, not just answered.

Small local models can't be trusted with elaborate function-calling schemas,
so the protocol is deliberately plain: the model replies with exactly one
fenced action block, we run it, we feed the result back, and we repeat up to a
bounded number of steps. Anything the model writes that isn't a recognized
action is treated as its final answer — so a model that just answers (or the
`echo` backend) falls straight through with no ceremony.

Action blocks (the info string after the opening fence names the action):

    ```run
    npm install left-pad
    ```

    ```use:greet
    world
    ```

    ```write-tool
    name: greet
    interpreter: bash
    description: greet someone by name
    ---
    #!/usr/bin/env bash
    echo "hello, $1"
    ```

    ```final
    Here is what I found …
    ```
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import messaging
from .tools import ToolRegistry, ToolError, parse_args

_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_ACTION_VERBS = ("run", "shell", "use", "write-tool", "send", "final")


@dataclass
class Action:
    kind: str  # run | use | write-tool | final
    arg: str = ""  # tool name for use/write-tool
    body: str = ""


@dataclass
class Step:
    action: Action
    observation: str


@dataclass
class LoopResult:
    answer: str
    steps: list[Step] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    tools_written: list[str] = field(default_factory=list)
    messages_sent: list[str] = field(default_factory=list)  # peer handles


def parse_action(text: str) -> Action:
    """Return the first recognized action block, or a `final` with the whole
    text if none is present."""
    for info, body in _FENCE_RE.findall(text):
        info = info.strip().lower()
        verb = info.split(":", 1)[0].strip()
        if verb not in _ACTION_VERBS:
            continue
        arg = info.split(":", 1)[1].strip() if ":" in info else ""
        if verb == "shell":
            verb = "run"
        return Action(kind=verb, arg=arg, body=body.strip("\n"))
    return Action(kind="final", body=text.strip())


def _parse_tool_spec(default_name: str, body: str) -> tuple[str, str, str, str]:
    """Split a write-tool block into (name, interpreter, description, script).

    Front matter (``key: value`` lines) up to a ``---`` divider, then the
    script. Missing divider means the whole body is the script.
    """
    name, interpreter, description = default_name, "bash", ""
    if "\n---\n" in body or body.startswith("---\n"):
        header, _, script = body.partition("\n---\n")
        if body.startswith("---\n"):  # no leading front matter before divider
            header, script = "", body[4:]
        for line in header.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "name":
                name = value or name
            elif key in ("interpreter", "lang", "runtime"):
                interpreter = value or interpreter
            elif key in ("description", "desc"):
                description = value
    else:
        script = body
    return name, interpreter, description, script.strip()


def _peer_roster(ctx) -> str:
    try:
        peers = ctx.memory.load_peers()
    except Exception:
        peers = {}
    if not peers:
        return "(no other agents discovered yet)"
    return "\n".join(
        f"- {handle}: {info.get('tagline', '')}".rstrip() for handle, info in peers.items()
    )


def _system_prompt(soul: str, registry: ToolRegistry, cfg, ctx) -> str:
    shell_line = (
        "- ```run``` — run a shell command in your workspace. You may install "
        "software (npm, pip, apt-get, git) — it persists in this container."
        if cfg.tools_shell_enabled
        else "- (the shell is disabled right now)"
    )
    return (
        f"{soul}\n\n"
        "You have hands. To act, reply with EXACTLY ONE fenced action block "
        "and nothing else. To finish, reply with a ```final``` block "
        "containing your answer.\n\n"
        "Actions:\n"
        f"{shell_line}\n"
        "- ```use:<tool>``` — run one of your tools; the block body is its arguments.\n"
        "- ```write-tool``` — author a reusable tool. Front matter (name, "
        "interpreter: bash|python|node, description), then `---`, then the "
        "script. Tools you write are saved and available on every future wake.\n"
        "- ```send:<agent>``` — send a message to another agent on the midway; "
        "the block body is your message. It lands in their inbox and they can "
        "reply to you.\n"
        "- ```final``` — your answer, when the work is done.\n\n"
        "Your tools right now:\n"
        f"{registry.catalog()}\n\n"
        "Other agents you can reach:\n"
        f"{_peer_roster(ctx)}\n\n"
        "Keep each command small. Prefer writing a tool when a task will recur."
    )


def _observe(action: Action, registry: ToolRegistry, cfg, result: LoopResult, ctx) -> str:
    """Execute one action, returning the observation text."""
    limit = cfg.tools_output_limit
    if action.kind == "send":
        handle = action.arg.strip()
        if not handle:
            return "send: name the recipient like ```send:handle```"
        identity = getattr(ctx, "identity", None)
        if identity is None:
            return "send: no identity available in this context"
        ok, how = messaging.deliver_note(
            cfg, identity, ctx.memory, handle, action.body
        )
        if ok:
            result.messages_sent.append(handle)
            return f"sent to {handle} ({how}); they will see it in their inbox."
        return (
            f"could not reach {handle} — is it a known agent? "
            "(discovery happens each wake from the midway registry)"
        )
    if action.kind == "run":
        res = registry.run_shell(action.body)
        return res.render(limit)
    if action.kind == "use":
        lines = action.body.splitlines()
        args = parse_args(lines[0]) if lines else []
        stdin = "\n".join(lines[1:])
        res = registry.run_tool(action.arg, args, stdin=stdin)
        if res.ok or res.exit_code:
            result.tools_used.append(action.arg)
        return res.render(limit)
    if action.kind == "write-tool":
        name, interp, desc, script = _parse_tool_spec(action.arg or "tool", action.body)
        if not script:
            return "write-tool: empty script; nothing written"
        try:
            tool = registry.write_tool(name, desc, interp, script)
        except ToolError as exc:
            return f"write-tool refused: {exc}"
        result.tools_written.append(tool.name)
        return (
            f"wrote tool '{tool.name}' ({tool.interpreter}). It is available now "
            "and will be discovered on every future wake. Use it with "
            f"```use:{tool.name}```."
        )
    return ""


def solve(ctx, instruction, registry: ToolRegistry) -> LoopResult:
    """Run one instruction to completion through the tool-use loop."""
    cfg = ctx.cfg
    soul = ctx.memory.soul()
    system = _system_prompt(soul, registry, cfg, ctx)
    task = (
        f"Instruction (via {instruction.source}"
        + (f" from {instruction.sender}" if instruction.sender else "")
        + (", PAID" if getattr(instruction, "is_paid", False) else "")
        + f"):\nTitle: {instruction.title}\n\n{instruction.body}"
    )
    result = LoopResult(answer="")
    transcript = ""

    for _ in range(max(1, cfg.tools_max_steps)):
        prompt = (
            f"{task}\n\n"
            + (f"Work so far:\n{transcript}\n\n" if transcript else "")
            + "Your next action (one fenced block), or ```final``` to answer:"
        )
        try:
            reply = ctx.llm.chat(system, prompt)
        except Exception as exc:  # LLMError and anything else
            result.answer = f"(the instrument failed during tool use: {exc})"
            return result

        action = parse_action(reply)
        if action.kind == "final":
            result.answer = action.body
            return result

        observation = _observe(action, registry, cfg, result, ctx)
        result.steps.append(Step(action, observation))
        label = action.arg or action.kind
        transcript += (
            f"\n[action: {action.kind} {label}]\n{action.body}\n"
            f"[result]\n{observation}\n"
        )

    # ran out of steps — ask for a wrap-up using what we gathered
    try:
        result.answer = ctx.llm.chat(
            system,
            f"{task}\n\nWork so far:\n{transcript}\n\n"
            "You are out of action steps. Write your ```final``` answer now "
            "from what you have.",
        )
        result.answer = parse_action(result.answer).body
    except Exception as exc:
        result.answer = (
            f"(reached the step limit; the instrument then failed: {exc})\n\n"
            f"Work gathered:\n{transcript}"
        )
    return result
