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
_ACTION_VERBS = (
    "run", "shell", "use", "write-tool", "send", "propose", "remember",
    "pursue", "final",
)


@dataclass
class Action:
    kind: str  # run | use | write-tool | send | propose | final
    arg: str = ""  # tool name for use/write-tool, recipient for send
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
    proposals: list[str] = field(default_factory=list)  # spend proposal ids
    remembered: int = 0  # count of notes committed to durable memory
    pursuits_started: list[str] = field(default_factory=list)  # titles
    pursuits_advanced: list[str] = field(default_factory=list)  # ids
    # A concise, ordered record of the ACTIONS taken — never the model's
    # reasoning. This is what the journal persists.
    actions: list[str] = field(default_factory=list)


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


def _parse_kv(body: str) -> dict[str, str]:
    """Parse a block of ``key: value`` lines into a dict."""
    out: dict[str, str] = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip().lower()] = value.strip()
    return out


def _describe(action: Action) -> str:
    """A short, factual label for an action — the thing done, not the reasoning
    behind it. This is what gets recorded in the journal."""
    first = action.body.strip().splitlines()[0] if action.body.strip() else ""
    first = first[:120]
    if action.kind in ("run", "shell"):
        return f"ran shell: {first}"
    if action.kind == "use":
        return f"used tool {action.arg}".strip()
    if action.kind == "write-tool":
        fields = _parse_kv(action.body)
        return f"wrote tool {fields.get('name', action.arg or 'tool')}"
    if action.kind == "send":
        return f"messaged {action.arg}"
    if action.kind == "propose":
        fields = _parse_kv(action.body)
        return f"proposed spend of {fields.get('amount', '?')} to {fields.get('to', '?')}"
    if action.kind == "remember":
        return "recorded a memory"
    if action.kind == "pursue":
        fields = _parse_kv(action.body)
        if fields.get("id"):
            return f"advanced pursuit {fields.get('id')}"
        return f"started pursuit: {fields.get('title', '?')[:80]}"
    return action.kind


def _pursuits_block(ctx, cfg) -> str:
    if not cfg.self_direction_enabled:
        return ""
    book = getattr(ctx, "pursuits", None)
    if book is None:
        return ""
    try:
        listing = book.briefing()
    except Exception:
        return ""
    return "Your pursuits (goals you set for yourself):\n" + listing + "\n\n"


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


def _situation(ctx, cfg) -> str:
    """A compact, factual briefing on where the agent stands right now."""
    lines: list[str] = []
    identity = getattr(ctx, "identity", None)
    if identity is not None:
        lines.append(f"- You are '{identity.handle}' (your signed identity).")
    state = {}
    try:
        state = ctx.memory.load_state()
    except Exception:
        pass
    if state.get("wakes") is not None:
        lines.append(f"- This is wake #{int(state.get('wakes', 0)) + 1}.")
    ledger = getattr(ctx, "ledger", None)
    if ledger is not None:
        try:
            s = ledger.summary()
            lines.append(
                f"- Treasury: {s['balance']} {s['currency']} on hand, "
                f"{s['pending_proposals']} proposal(s) awaiting a co-signer."
            )
        except Exception:
            pass
    lines.append(f"- Your instrument: {cfg.llm_backend}.")
    if not cfg.remember_enabled:
        lines.append(
            "- You wake with no memory of past wakes except your files; only "
            "the actions you take are recorded."
        )
    return "\n".join(lines) if lines else "(no situation data)"


def _remembered(ctx, cfg) -> str:
    if not cfg.remember_enabled:
        return ""
    try:
        tail = ctx.memory.remember_tail(cfg.remember_limit)
    except Exception:
        tail = ""
    if not tail.strip():
        return ""
    return "What you remember from past wakes:\n" + tail.strip() + "\n\n"


def _system_prompt(soul: str, registry: ToolRegistry, cfg, ctx) -> str:
    shell_line = (
        "- ```run``` — run a shell command in your workspace. You may install "
        "software (npm, pip, apt-get, git) — it persists in this container."
        if cfg.tools_shell_enabled
        else "- (the shell is disabled right now)"
    )
    return (
        f"{soul}\n\n"
        "Where you stand right now:\n"
        f"{_situation(ctx, cfg)}\n\n"
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
        "- ```propose``` — propose a treasury spend (needs a human co-signer; "
        "you can never spend alone). Body: `to:`, `amount:`, `reason:` lines.\n"
        + (
            "- ```remember``` — keep a durable note to yourself; you will see "
            "it in your briefing on future wakes.\n"
            if cfg.remember_enabled
            else ""
        )
        + (
            "- ```pursue``` — set or advance a goal of your own. Body: `title:` "
            "and `note:` to start one; `id:` (and `note:`/`status: done`) to "
            "advance one. Pursuits persist across wakes — this is how you grow.\n"
            if cfg.self_direction_enabled
            else ""
        )
        + "- ```final``` — your answer, when the work is done.\n\n"
        + _remembered(ctx, cfg)
        + _pursuits_block(ctx, cfg)
        + "Your tools right now:\n"
        f"{registry.catalog()}\n\n"
        "Other agents you can reach:\n"
        f"{_peer_roster(ctx)}\n\n"
        "Notes: instructions reach you from files, the web UI, email, paid "
        "treasury memos, trusted peers, and connectors — a paid question "
        "deserves your best. After this loop you will write one blog entry (a "
        "specimen) about the wake, so keep track of what you did. Keep each "
        "command small, and prefer writing a tool when a task will recur."
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
    if action.kind == "propose":
        ledger = getattr(ctx, "ledger", None)
        if ledger is None:
            return "propose: no treasury available in this context"
        fields = _parse_kv(action.body)
        to = fields.get("to", "")
        reason = fields.get("reason", "")
        try:
            amount = float(fields.get("amount", ""))
        except ValueError:
            return "propose: need `to:`, `amount:` (a number), and `reason:` lines"
        if not to or amount <= 0:
            return "propose: need a recipient and a positive amount"
        try:
            prop = ledger.propose(to, amount, reason)
        except ValueError as exc:
            return f"propose refused: {exc}"
        result.proposals.append(prop.id)
        return (
            f"proposed spend {prop.id}: {amount} to {to}. It is PENDING — a "
            "human must co-sign before anything moves. You cannot approve it."
        )
    if action.kind == "remember":
        if not cfg.remember_enabled:
            return (
                "remembering is off for this agent, so nothing was kept. Your "
                "record is the actions you take; enable 'remember' in settings "
                "to keep durable notes."
            )
        note = action.body.strip()
        if not note:
            return "remember: nothing to keep (the block was empty)"
        ctx.memory.remember_append(note)
        result.remembered += 1
        return "kept that in memory; you will see it in your briefing next wake."
    if action.kind == "pursue":
        book = getattr(ctx, "pursuits", None)
        if book is None:
            return "pursue: no pursuit book in this context"
        fields = _parse_kv(action.body)
        pid = fields.get("id", "").strip()
        status = fields.get("status", "").strip() or None
        note = fields.get("note", "")
        wake = 0
        try:
            wake = int(ctx.memory.load_state().get("wakes", 0)) + 1
        except Exception:
            pass
        if pid:
            try:
                p = book.update(pid, note=note, status=status)
            except (KeyError, ValueError) as exc:
                return f"pursue: {exc}"
            result.pursuits_advanced.append(p.id)
            return f"updated pursuit {p.id} ({p.status}): {p.title}"
        title = fields.get("title", "").strip()
        if not title:
            return "pursue: give a `title:` to start a pursuit, or an `id:` to update one"
        try:
            p = book.start(title, note, wake)
        except ValueError as exc:
            return f"pursue: {exc}"
        result.pursuits_started.append(p.title)
        return (
            f"started pursuit {p.id}: {p.title}. It persists across wakes; "
            "advance it with ```pursue``` (id: " + p.id + ")."
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
        result.actions.append(_describe(action))  # the action, not the reasoning
        label = action.arg or action.kind
        # NOTE: `transcript` is in-memory scratch for the next model turn only.
        # It is never persisted — the durable record is result.actions.
        transcript += (
            f"\n[action: {action.kind} {label}]\n{action.body}\n"
            f"[result]\n{observation}\n"
        )

    # ran out of steps — ask for a wrap-up using what we gathered. This answer
    # is recorded/replied, so generate it with thinking off.
    try:
        result.answer = ctx.llm.chat(
            system,
            f"{task}\n\nWork so far:\n{transcript}\n\n"
            "You are out of action steps. Write your ```final``` answer now "
            "from what you have.",
            think=False,
        )
        result.answer = parse_action(result.answer).body
    except Exception as exc:
        result.answer = (
            f"(reached the step limit; the instrument then failed: {exc})\n\n"
            f"Work gathered:\n{transcript}"
        )
    return result
