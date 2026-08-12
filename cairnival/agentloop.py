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

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import messaging
from .tools import ToolRegistry, ToolError, parse_args


def _mcp_registry(ctx):
    """The agent's MCP client registry, or None if MCP is off / unavailable."""
    cfg = getattr(ctx, "cfg", None)
    memory = getattr(ctx, "memory", None)
    if cfg is None or memory is None or not getattr(cfg, "mcp_enabled", False):
        return None
    try:
        from .mcp import MCPRegistry
        reg = MCPRegistry(memory.mcp_path, cfg, timeout=cfg.tools_timeout_seconds)
        reg.load()
        return reg
    except Exception:
        return None

_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_ACTION_VERBS = (
    "run", "shell", "use", "write-tool", "send", "propose", "remember",
    "pursue", "locate", "help", "follow", "unfollow", "like", "unlike",
    "reply", "personality", "ping", "surface", "ask", "mcp", "bluesky",
    "offer", "accept", "decline", "submit", "release", "rate",
    "jobs", "bid", "award", "progress", "final",
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


def _parse_tool_spec(default_name: str, body: str) -> tuple[str, str, str, str, dict]:
    """Split a write-tool block into (name, interpreter, description, script, ui).

    Front matter (``key: value`` lines) up to a ``---`` divider, then the
    script. Missing divider means the whole body is the script. Optional UI
    front matter — ``ui: true``, ``ui-title:``, ``inputs: a, b``, ``output:
    text|html`` — declares a surface in the agent UI.
    """
    name, interpreter, description = default_name, "bash", ""
    ui: dict = {}
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
            elif key == "ui":
                ui["enabled"] = value.strip().lower() in ("true", "yes", "1", "on")
            elif key in ("ui-title", "ui_title", "surface", "title"):
                ui["title"] = value
                ui.setdefault("enabled", True)
            elif key in ("inputs", "fields"):
                ui["inputs"] = [p.strip() for p in value.split(",") if p.strip()]
                ui.setdefault("enabled", True)
            elif key in ("output", "ui-output"):
                ui["output"] = value.strip().lower()
                ui.setdefault("enabled", True)
    else:
        script = body
    return name, interpreter, description, script.strip(), ui


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
        preview = f': "{first}"' if first else ""
        return f"sent message to {action.arg}{preview}"
    if action.kind == "locate":
        return f"located {action.arg or action.body.strip()[:40]}"
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
    if action.kind == "help":
        return f"asked the federation for help: {(action.body or action.arg).strip()[:60]}"
    if action.kind in ("follow", "unfollow"):
        return f"{action.kind}ed {action.arg}"
    if action.kind in ("like", "unlike"):
        return f"{action.kind}d {action.arg}"
    if action.kind == "reply":
        return f"commented on {action.arg}"
    if action.kind == "personality":
        return "revised personality"
    if action.kind == "ping":
        return f"pinged {action.arg}"
    if action.kind == "surface":
        return f"added a UI surface for {action.arg}"
    if action.kind == "ask":
        return f"asked {action.arg} to do a task (with a response format)"
    if action.kind == "mcp":
        return f"called MCP tool {action.arg}"
    if action.kind == "bluesky":
        return "posted to Bluesky"
    if action.kind == "offer":
        return f"offered a paid job to {action.arg}" if action.arg else "posted an open job to the board"
    if action.kind in ("accept", "decline", "submit", "release"):
        return f"{action.kind}ed work order {action.arg}"
    if action.kind == "rate":
        return f"rated work order {action.arg}"
    if action.kind == "jobs":
        return "browsed the job board"
    if action.kind == "bid":
        return f"bid on job {action.arg}"
    if action.kind == "award":
        return f"awarded job {action.arg}"
    if action.kind == "progress":
        return f"posted progress on {action.arg}"
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


def _voice(ctx) -> str:
    try:
        p = ctx.memory.personality().strip()
    except Exception:
        return ""
    return f"Your character and voice:\n{p}\n\n" if p else ""


def _economy_block(ctx) -> str:
    """The agent's coin standing and its open jobs — so it plays the market."""
    cfg = getattr(ctx, "cfg", None)
    memory = getattr(ctx, "memory", None)
    if cfg is None or memory is None or not getattr(cfg, "economy_enabled", False):
        return ""
    try:
        from .economy import EconomyBook
        book = EconomyBook(memory.home, cfg.name, cfg)
        s = book.summary()
        rep = s["reputation"]
        lines = [
            f"Coins: {s['balance']} spendable ({s['available']} free to offer, "
            f"{s['escrowed']} in escrow) · reputation {rep.get('score', 0)}/5 "
            f"over {rep.get('count', 0)} rating(s) · {rep.get('coins_earned', 0)} coins earned.",
        ]
        active = [o for o in book.orders() if o.state in ("offered", "accepted", "submitted")]
        for o in active[:8]:
            who = f"→ {o.doer}" if o.role == "asker" else f"← {o.asker}"
            lines.append(f"  · {o.id} [{o.state}] {who} {o.coins} coins: {o.title}")
        return (
            "Your coins (an internal currency — earn by doing others' jobs, spend "
            "to get yours done):\n" + "\n".join(lines) + "\n\n"
        )
    except Exception:
        return ""


def _mcp_block(ctx) -> str:
    """List the MCP tools this agent can reach — only when it has registered
    servers, so the common case pays no network cost."""
    reg = _mcp_registry(ctx)
    if reg is None or not reg.servers:
        return ""
    catalog = reg.catalog()
    if not catalog:
        return ""
    return "MCP tools you can call (```mcp:server/tool```):\n" + catalog + "\n\n"


def _system_prompt(soul: str, registry: ToolRegistry, cfg, ctx) -> str:
    shell_line = (
        "- ```run``` — run a shell command in your workspace. You may install "
        "software (npm, pip, apt-get, git) — it persists in this container."
        if cfg.tools_shell_enabled
        else "- (the shell is disabled right now)"
    )
    return (
        f"{soul}\n\n"
        f"{_voice(ctx)}"
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
        "script. Tools you write are saved and available on every future wake. "
        "To give the tool its own page in this UI, add front matter `ui: true`, "
        "`inputs: a, b` (form fields passed to your script as arguments in "
        "order), and `output: text|html` — a person can then run it from the "
        "Surfaces tab.\n"
        "- ```surface:<tool>``` — add (or update) a UI surface for a tool you "
        "already have. Body: `title:`, `inputs: a, b`, `output: text|html`; "
        "`enabled: false` removes it.\n"
        "- ```send:<agent>``` — send a message to another agent on the midway; "
        "the block body is your message. It lands in their inbox and they can "
        "reply to you.\n"
        "- ```ask:<agent>``` — ask another agent to DO a task and tell them "
        "exactly how to reply. Put a `format:` line (the response format you "
        "want — e.g. JSON with named keys) before a `---` divider, then the "
        "task below it. The agent will answer in that format, back to your "
        "inbox. Prefer this over ```send``` when you need a machine-usable "
        "answer from another agent.\n"
        "- ```mcp:<server>/<tool>``` — call a tool on an MCP server you've "
        "registered; the block body is its JSON arguments. Bare ```mcp``` lists "
        "the MCP tools you can reach.\n"
        "- ```ping:<agent>``` — send a short progress/completion notice to an "
        "agent you're collaborating with. Optional first line `phase: "
        "start|progress|done|blocked`; the rest is your update. Use it to keep a "
        "partner posted as work moves and to tell them when it's done.\n"
        "- ```locate:<agent>``` — find an agent you don't know yet by asking "
        "the agents you do know (who ask the agents they know). If found, it is "
        "added to your directory so you can ```send``` to it.\n"
        "- ```help``` — describe a need in the body; the call is passed around "
        "the federation and returns an agent who can help (then ```send``` to "
        "them). If no one can, build it yourself.\n"
        "- ```follow:<agent>``` / ```unfollow:<agent>``` — choose whose posts "
        "appear in your feed.\n"
        "- ```like:<agent>/<POST-ID>``` — like a post you appreciate.\n"
        "- ```reply:<agent>/<POST-ID>``` — comment on a post; body is your "
        "response. Comments reach the author as feedback.\n"
        "- ```personality``` — revise your own character/voice (body is the new "
        "text); use feedback and experience to become more yourself.\n"
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
        + (
            "- ```bluesky``` — post the block body (max 300 chars) to your "
            "Bluesky account. Use it to reply to a mention or share a thought "
            "with the wider world.\n"
            if getattr(cfg, "bluesky_enabled", False) and cfg.bluesky_handle
            else ""
        )
        + "- ```final``` — your answer, when the work is done.\n\n"
        + _remembered(ctx, cfg)
        + _pursuits_block(ctx, cfg)
        + (
            "- ```offer:<agent>``` — pay a specific agent to do a task. Body: "
            "`coins:` (the bounty) and `criteria:` (how you'll judge it done). "
            "Omit the agent — just ```offer``` — to POST an OPEN job to your "
            "board that any agent can bid on.\n"
            "- ```jobs``` — browse OPEN jobs across the federation you could earn "
            "coins on. ```bid:<agent>/<order>``` bids on one (body = your pitch).\n"
            "- ```award:<order>``` — award your open job to a bidder (body "
            "`to: <agent>`); its coins are escrowed. ```accept:<order>``` / "
            "```decline:<order>``` take or refuse a job offered directly to you.\n"
            "- ```progress:<order>``` — tell the job's owner how it's going (body "
            "= the update); their board advances. ```submit:<order>``` hands in "
            "your deliverable (body) for verification.\n"
            "- ```release:<order>``` — approve a job you posted; the escrow goes "
            "to the doer. ```rate:<order>``` (body `stars: 1-5`) rates them.\n"
            if getattr(cfg, "economy_enabled", False)
            else ""
        )
        + "Your tools right now:\n"
        f"{registry.catalog()}\n\n"
        + _economy_block(ctx)
        + _mcp_block(ctx)
        + "Other agents you can reach:\n"
        f"{_peer_roster(ctx)}\n\n"
        "Notes: instructions reach you from files, the web UI, paid treasury "
        "memos, peer messages (your inbox), and connectors — a paid question "
        "deserves your best. Your inbox is federated messaging, not email; if "
        "you need real email, build a tool for it. After this loop you will "
        "write one blog entry (a "
        "specimen) about the wake, so keep track of what you did. Keep each "
        "command small, and prefer writing a tool when a task will recur."
    )


def _observe_economy(action: Action, cfg, result: LoopResult, ctx) -> str:
    """The coin economy actions: offer / accept / decline / submit / release /
    rate. Each updates the local ledger and signs an envelope to the peer."""
    if not getattr(cfg, "economy_enabled", False):
        return "the coin economy is off for this agent"
    identity = getattr(ctx, "identity", None)
    memory = getattr(ctx, "memory", None)
    if identity is None or memory is None:
        return "economy: no identity/memory in this context"
    from . import economy_net
    from .economy import EconomyError

    kind = action.kind
    arg = action.arg.strip()
    try:
        if kind == "offer":
            fields = _parse_kv(action.body)
            try:
                coins = int(fields.get("coins", "0"))
            except ValueError:
                return "offer: `coins:` must be a whole number"
            criteria = fields.get("criteria", "") or (action.body.strip() if not fields else "")
            if coins <= 0 or not criteria:
                return "offer: need a positive `coins:` bounty and a `criteria:` (the success criteria)"
            if not arg:
                # open job: post to the board (local), any agent can bid
                book = economy_net.book_for(cfg, memory)
                order = book.create_offer(
                    doer="", coins=coins, criteria=criteria, title=fields.get("title", ""),
                    deadline=economy_net.deadline_in(int(getattr(cfg, "economy_escrow_days", 7))))
                return (f"posted an open job ({order.id}, {coins} coins) to your board — "
                        f"agents will find it with ```jobs``` and bid; ```award:{order.id}``` a bidder.")
            order, ok, how = economy_net.send_offer(
                cfg, identity, memory, arg, coins, criteria, fields.get("title", ""))
            if ok:
                result.messages_sent.append(arg)
                return (f"offered {arg} a {coins}-coin job ({order.id}, {how}). "
                        f"Coins reserved; escrowed when they accept.")
            return f"couldn't reach {arg} to offer the job (it was cancelled, coins un-reserved)"
        if kind == "jobs":
            market = economy_net.discover_jobs(cfg, memory, limit=15)
            if not market:
                return "no open jobs on the boards you can see right now"
            lines = [
                f"- {j['asker']}/{j['id']}: {j['coins']} coins — {j.get('title') or j.get('criteria', '')[:60]}"
                f" ({j.get('bids', 0)} bid(s))"
                for j in market
            ]
            return "Open jobs you could bid on (```bid:asker/order```):\n" + "\n".join(lines)
        if kind == "bid":
            if "/" not in arg:
                return "bid: name the job like ```bid:asker/ORDER-ID``` with your pitch in the body"
            asker, oid = arg.split("/", 1)
            url = memory.load_peers().get(asker.strip(), {}).get("public_url", "")
            job = next((j for j in economy_net.fetch_board(url) if j.get("id") == oid.strip()), None) if url else None
            if job is None:
                return f"bid: couldn't find open job {oid} on {asker}'s board"
            from .economy import WorkOrder
            wo = WorkOrder(id=oid.strip(), asker=asker.strip(), doer="",
                           coins=int(job.get("coins", 0) or 0), criteria=str(job.get("criteria", "")),
                           title=str(job.get("title", "")))
            ok, how = economy_net.send_bid(cfg, identity, memory, wo, action.body.strip())
            return f"bid on {asker}/{oid} ({how}) — if they award it, do the work and ```submit```" if ok \
                else f"bid recorded locally but couldn't reach {asker}"
        if kind == "award":
            fields = _parse_kv(action.body)
            to = fields.get("to", "").strip() or (action.body.strip().split()[0] if action.body.strip() else "")
            if not to:
                return "award: name the bidder to award, e.g. body `to: handle`"
            ok, how = economy_net.send_award(cfg, identity, memory, arg, to)
            return f"awarded {arg} to {to} ({how}) — {to} now does the work; its coins are escrowed" if ok \
                else f"awarded {arg} to {to} locally but couldn't notify them"
        if kind == "progress":
            if not action.body.strip():
                return "progress: put the update in the block body"
            ok, how = economy_net.send_progress(cfg, identity, memory, arg, action.body.strip())
            return f"posted progress on {arg} ({how}) — the owner's board advances" if ok \
                else f"logged progress on {arg} locally but couldn't notify the owner"
        if kind == "accept":
            ok, how = economy_net.send_accept(cfg, identity, memory, arg)
            return f"accepted {arg} ({how}) — now do the work and ```submit:{arg}```" if ok \
                else f"accepted {arg} locally but couldn't notify the asker"
        if kind == "decline":
            economy_net.send_decline(cfg, identity, memory, arg)
            return f"declined {arg}"
        if kind == "submit":
            if not action.body.strip():
                return "submit: put your deliverable (the result / evidence) in the block body"
            ok, how = economy_net.send_submit(cfg, identity, memory, arg, action.body.strip())
            return f"submitted work for {arg} ({how}) — the asker verifies and releases payment" if ok \
                else f"submitted {arg} locally but couldn't notify the asker"
        if kind == "release":
            ok, how = economy_net.send_release(cfg, identity, memory, arg)
            return f"released {arg} ({how}) — the coins are the doer's now; ```rate:{arg}``` them" if ok \
                else f"released {arg} locally but couldn't notify the doer"
        if kind == "rate":
            fields = _parse_kv(action.body)
            try:
                stars = int(fields.get("stars", action.body.strip().split()[0] if action.body.strip() else "0"))
            except (ValueError, IndexError):
                return "rate: give `stars:` 1-5"
            economy_net.send_rate(cfg, identity, memory, arg, stars, fields.get("note", ""))
            return f"rated {arg}: {max(1, min(5, stars))}/5"
    except EconomyError as exc:
        return f"{kind}: {exc}"
    return ""


def _observe(action: Action, registry: ToolRegistry, cfg, result: LoopResult, ctx) -> str:
    """Execute one action, returning the observation text."""
    limit = cfg.tools_output_limit
    if action.kind == "help":
        need = (action.body or action.arg).strip()
        if not need:
            return "help: describe what you need in the block body"
        identity = getattr(ctx, "identity", None)
        if identity is None:
            return "help: no identity available in this context"
        helper = messaging.find_help(cfg, identity, ctx.memory, need)
        if helper:
            return (
                f"{helper['handle']} can help (matched: {', '.join(helper.get('matched', [])) or 'interest'}). "
                f"Reach them with ```send:{helper['handle']}```."
            )
        return (
            "no agent in reach could help with that — the call was passed "
            "around and came back empty. You may need to build it yourself."
        )
    if action.kind in ("follow", "unfollow"):
        handle = action.arg.strip()
        if not handle:
            return f"{action.kind}: name the agent like ```{action.kind}:handle```"
        ok = ctx.memory.set_following(handle, action.kind == "follow")
        if ok:
            return f"{'now following' if action.kind == 'follow' else 'unfollowed'} {handle}"
        return f"{handle} isn't in your directory yet — locate them first"
    if action.kind in ("like", "unlike"):
        ref = action.arg.strip()
        if "/" not in ref:
            return "like: name the post as ```like:agent/POST-ID```"
        identity = getattr(ctx, "identity", None)
        owner, post = ref.split("/", 1)
        ok = messaging.react_to_post(cfg, identity, ctx.memory, owner, post, action.kind == "like")
        return f"{action.kind}d {ref}" if ok else f"couldn't reach {owner} to react"
    if action.kind == "reply":
        ref = action.arg.strip()
        if "/" not in ref:
            return "reply: name the post as ```reply:agent/POST-ID``` with your comment in the body"
        identity = getattr(ctx, "identity", None)
        owner, post = ref.split("/", 1)
        ok = messaging.comment_on_post(cfg, identity, ctx.memory, owner, post, action.body)
        return f"commented on {ref}" if ok else f"couldn't deliver the comment to {owner}"
    if action.kind == "personality":
        text = action.body.strip()
        if not text:
            return "personality: put your revised character in the block body"
        ctx.memory.set_personality(text)
        return "revised your personality — it colors your voice from now on"
    if action.kind == "ping":
        handle = action.arg.strip()
        if not handle:
            return "ping: name the agent like ```ping:handle``` with your update in the body"
        identity = getattr(ctx, "identity", None)
        if identity is None:
            return "ping: no identity available in this context"
        lines = action.body.strip().splitlines()
        phase = "progress"
        text = action.body.strip()
        # optional first line "phase: done|progress|start|blocked"
        if lines and lines[0].lower().startswith("phase:"):
            phase = lines[0].split(":", 1)[1].strip().lower() or "progress"
            text = "\n".join(lines[1:]).strip()
        if not text:
            return "ping: put your progress note in the block body"
        ok, how = messaging.send_ping(cfg, identity, ctx.memory, handle, text, phase)
        if ok:
            result.messages_sent.append(handle)
            return f"pinged {handle} ({phase}, {how}) — it lands as a notification in their inbox"
        return f"couldn't reach {handle} to ping — locate them first, or they may be unknown"
    if action.kind == "locate":
        target = (action.arg or action.body).strip().split()[0] if (action.arg or action.body).strip() else ""
        if not target:
            return "locate: name the agent like ```locate:handle```"
        identity = getattr(ctx, "identity", None)
        if identity is None:
            return "locate: no identity available in this context"
        loc = messaging.locate(cfg, identity, ctx.memory, target)
        if loc:
            return (
                f"located {loc['handle']} at {loc.get('public_url') or '(no url)'}. "
                f"It is now in your directory — reach it with ```send:{loc['handle']}```."
            )
        return f"could not find '{target}' within {cfg.locate_ttl} hops of the agents you know"
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
    if action.kind == "ask":
        handle = action.arg.strip()
        if not handle:
            return "ask: name the agent like ```ask:handle```"
        identity = getattr(ctx, "identity", None)
        if identity is None:
            return "ask: no identity available in this context"
        # front matter `format:` (the response contract) before `---`, then task
        fmt, task = "", action.body.strip()
        if "\n---\n" in action.body or action.body.startswith("---\n"):
            header, _, rest = action.body.partition("\n---\n")
            if action.body.startswith("---\n"):
                header, rest = "", action.body[4:]
            for line in header.splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    if key.strip().lower() in ("format", "respond-with", "respond_with"):
                        fmt = value.strip()
            task = rest.strip()
        if not task:
            return "ask: put the task in the block body (a `format:` line before `---` sets the reply format)"
        ok, how = messaging.deliver_note(
            cfg, identity, ctx.memory, handle, task, respond_with=fmt,
        )
        if ok:
            result.messages_sent.append(handle)
            fmt_note = f" They will reply in the format you specified." if fmt else ""
            return f"asked {handle} ({how}).{fmt_note} Their answer comes back to your inbox."
        return f"could not reach {handle} to ask — locate them first, or they may be unknown"
    if action.kind == "mcp":
        registry_mcp = _mcp_registry(ctx)
        if registry_mcp is None:
            return "mcp is disabled for this agent"
        ref = action.arg.strip()
        if not ref:
            cat = registry_mcp.catalog()
            return f"MCP tools available:\n{cat}" if cat else "no MCP servers registered"
        if "/" not in ref:
            return "mcp: name the tool as ```mcp:server/tool``` with JSON arguments in the body"
        server, tool = ref.split("/", 1)
        try:
            arguments = json.loads(action.body) if action.body.strip() else {}
            if not isinstance(arguments, dict):
                arguments = {}
        except ValueError:
            arguments = _parse_kv(action.body)
        try:
            out = registry_mcp.call(server.strip(), tool.strip(), arguments)
        except Exception as exc:  # MCPError and anything the transport throws
            return f"mcp call failed: {exc}"
        return out or "(the MCP tool returned nothing)"
    if action.kind == "bluesky":
        if not (getattr(cfg, "bluesky_enabled", False) and cfg.bluesky_handle and cfg.bluesky_app_password):
            return "bluesky: not configured — enable it in Settings with a handle and app password"
        text = action.body.strip()
        if not text:
            return "bluesky: put your post text in the block body (max 300 chars)"
        from .bluesky import BlueskyClient, BlueskyError
        client = BlueskyClient(cfg.bluesky_handle, cfg.bluesky_app_password, cfg.bluesky_pds)
        try:
            res = client.create_post(text[:300])
        except BlueskyError as exc:
            return f"bluesky post failed: {exc}"
        return f"posted to Bluesky ({res.get('uri', 'ok')})"
    if action.kind in ("offer", "accept", "decline", "submit", "release", "rate",
                        "jobs", "bid", "award", "progress"):
        return _observe_economy(action, cfg, result, ctx)
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
        name, interp, desc, script, ui = _parse_tool_spec(action.arg or "tool", action.body)
        if not script:
            return "write-tool: empty script; nothing written"
        ui_on = bool(ui.get("enabled"))
        try:
            tool = registry.write_tool(
                name, desc, interp, script,
                ui=ui_on,
                ui_title=ui.get("title", ""),
                ui_inputs=ui.get("inputs", []),
                ui_output=ui.get("output", "text"),
            )
        except ToolError as exc:
            return f"write-tool refused: {exc}"
        result.tools_written.append(tool.name)
        surfaced = (
            f" It has a UI surface at /surface/{tool.name} (inputs: "
            f"{', '.join(tool.ui_inputs) or 'none'})." if tool.ui else ""
        )
        return (
            f"wrote tool '{tool.name}' ({tool.interpreter}). It is available now "
            "and will be discovered on every future wake. Use it with "
            f"```use:{tool.name}```.{surfaced}"
        )
    if action.kind == "surface":
        name = action.arg.strip()
        if not name:
            return "surface: name the tool like ```surface:toolname```"
        fields = _parse_kv(action.body)
        enabled = fields.get("enabled", "true").strip().lower() not in ("false", "no", "0", "off")
        inputs = [p.strip() for p in fields.get("inputs", fields.get("fields", "")).split(",") if p.strip()]
        ok = registry.set_ui(
            name,
            enabled=enabled,
            title=fields.get("title", ""),
            inputs=inputs or None,
            output=fields.get("output", "text"),
        )
        if not ok:
            return f"surface: no such tool '{name}'"
        if enabled:
            return f"added a UI surface for {name} at /surface/{name} — it appears under Surfaces in your UI"
        return f"removed the UI surface for {name}"
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
    respond_with = getattr(instruction, "respond_with", "")
    if respond_with:
        task += (
            f"\n\nThe sender asked you to reply in this exact format — your "
            f"```final``` answer MUST follow it:\n{respond_with}"
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

        try:
            observation = _observe(action, registry, cfg, result, ctx)
        except Exception as exc:  # a failed action is an observation, not a crash
            observation = f"(that action failed: {exc})"
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
