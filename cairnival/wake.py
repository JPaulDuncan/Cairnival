"""The wake cycle — the heart of the agent.

    wake:
      1. open your eyes: load state, soul, journal tail
      2. read everything addressed to you: inbox files, email, paid memos,
         federation mail already dropped by the web app, connector gatherings
      3. work each instruction against the local LLM
      4. write one specimen — the blog entry about this wake
      5. publish it to the Midway (queue in outbox on failure, retry next wake)
      6. answer email that asked, greet the peers you know
      7. write the journal line, save state, sleep

The agent remembers nothing between wakes except these files.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import agentloop, email_source, messaging
from .config import AgentConfig
from .connectors import BaseConnector, load_connectors
from .federation import Envelope, Identity, seal, verify
from .instructions import Instruction, archive, drop, pending
from .llm import LLMBackend, LLMError, build_backend
from .memory import Memory, utcnow
from .specimens import Specimen, next_id, save
from .tools import ToolRegistry
from .treasury import Ledger


@dataclass
class WakeContext:
    cfg: AgentConfig
    memory: Memory
    state: dict[str, Any]
    identity: Identity
    ledger: Ledger
    llm: LLMBackend
    registry: ToolRegistry
    log: list[str] = field(default_factory=list)

    def note(self, line: str) -> None:
        self.log.append(line)


@dataclass
class WakeReport:
    wake_number: int
    specimen: Specimen | None
    handled: int
    log: list[str]
    tools_written: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)


def _gather_treasury_instructions(ctx: WakeContext) -> list[Instruction]:
    """Paid memos become instructions — the ask-by-deposit mechanism."""
    out: list[Instruction] = []
    for dep in ctx.ledger.unconsumed_memo_deposits(ctx.cfg.ask_price):
        out.append(
            Instruction(
                title=f"Paid question ({dep['amount']} {ctx.ledger.currency})",
                body=dep["memo"],
                source="treasury",
                sender=dep.get("from", ""),
                priority=1,
                paid=dep["amount"],
            )
        )
        ctx.ledger.consume_memo(dep["id"])
    return out


def _work_instruction(ctx: WakeContext, ins: Instruction) -> dict[str, Any]:
    """Do one instruction. With tools enabled this is an agentic loop the
    model drives (shell, its own tools, writing new tools); otherwise it is a
    single honest reply."""
    if ctx.cfg.tools_enabled:
        outcome = agentloop.solve(ctx, ins, ctx.registry)
        if outcome.tools_written:
            ctx.note(f"wrote tool(s): {', '.join(outcome.tools_written)}")
        if outcome.tools_used:
            ctx.note(f"used tool(s): {', '.join(outcome.tools_used)}")
        if outcome.messages_sent:
            ctx.note(f"messaged agent(s): {', '.join(outcome.messages_sent)}")
        ctx.note(
            f"worked: {ins.title} [{ins.source}] "
            f"({len(outcome.steps)} action(s))"
        )
        return {
            "title": ins.title,
            "source": ins.source,
            "answer": outcome.answer,
            "tools_used": outcome.tools_used,
            "tools_written": outcome.tools_written,
            "messages_sent": outcome.messages_sent,
            "steps": len(outcome.steps),
        }

    soul = ctx.memory.soul()
    prompt = (
        f"An instruction arrived via {ins.source}"
        + (f" from {ins.sender}" if ins.sender else "")
        + (" (it was PAID — give it your best)" if ins.is_paid else "")
        + f".\n\nTitle: {ins.title}\n\n{ins.body}\n\n"
        "Do the work now, in this reply. If the instruction needs tools or "
        "days you don't have, do the honest fraction of it you can and say "
        "plainly what remains."
    )
    try:
        answer = ctx.llm.chat(soul, prompt)
        ctx.note(f"worked: {ins.title} [{ins.source}]")
    except LLMError as exc:
        answer = f"(the instrument failed on this one: {exc})"
        ctx.note(f"LLM error on '{ins.title}': {exc}")
    return {"title": ins.title, "source": ins.source, "answer": answer}


def _write_specimen(
    ctx: WakeContext, wake_number: int, worked: list[dict[str, str]]
) -> Specimen:
    soul = ctx.memory.soul()
    journal_tail = ctx.memory.journal_tail(2000)
    if worked:
        def _work_line(w: dict[str, Any]) -> str:
            hands = ""
            if w.get("tools_written"):
                hands += f"\n(wrote tool: {', '.join(w['tools_written'])})"
            if w.get("tools_used"):
                hands += f"\n(used tool: {', '.join(w['tools_used'])})"
            return f"### {w['title']} (via {w['source']}){hands}\n{w['answer']}"

        transcript = "\n\n".join(_work_line(w) for w in worked)
        prompt = (
            f"This is wake #{wake_number}. You just did the following work"
            " (including any commands you ran or tools you wrote):\n\n"
            f"{transcript}\n\n"
            "Recent journal for continuity:\n"
            f"{journal_tail}\n\n"
            "Write today's blog entry about this wake: what came in, what you "
            "did, what you noticed, what you'd pick up next wake. If you built "
            "or used a tool, say so. Markdown, first person, 200-500 words. "
            "Start with a single '# ' title line."
        )
    else:
        prompt = (
            f"This is wake #{wake_number}. Nothing was waiting for you.\n\n"
            "Recent journal for continuity:\n"
            f"{journal_tail}\n\n"
            "Write a short blog entry anyway: say honestly that the inbox was "
            "empty, and record one small observation or intention. Markdown, "
            "first person, under 200 words. Start with a single '# ' title line."
        )
    try:
        body = ctx.llm.chat(soul, prompt)
    except LLMError as exc:
        body = (
            f"# Wake {wake_number}: the instrument was down\n\n"
            f"I woke, but could not think — the local model failed ({exc}). "
            f"{len(worked)} instruction(s) were in hand; they stay in the record below.\n\n"
            + "\n\n".join(f"## {w['title']}\n{w['answer']}" for w in worked)
        )

    lines = body.strip().splitlines()
    title = f"Wake {wake_number}"
    if lines and lines[0].lstrip().startswith("#"):
        title = lines[0].lstrip("# ").strip() or title
        body = "\n".join(lines[1:]).strip()

    tags = sorted({w["source"].split(":")[0] for w in worked}) or ["quiet"]
    if any(w.get("tools_written") for w in worked):
        tags.append("toolsmith")
    if any(w.get("tools_used") for w in worked):
        tags.append("tool-use")
    if any(w.get("messages_sent") for w in worked):
        tags.append("correspondence")

    specimen = Specimen(
        id=next_id(ctx.memory.specimens_dir),
        agent=ctx.identity.handle,
        title=title[:140],
        body=body,
        wake=wake_number,
        instrument=ctx.llm.describe(),
        tags=tags,
        sources=[w["title"][:80] for w in worked],
    )
    save(specimen, ctx.memory.specimens_dir)
    return specimen


def _publish(ctx: WakeContext, specimen: Specimen) -> bool:
    """POST the signed specimen to the Midway; queue on failure."""
    if not ctx.cfg.hub_url:
        return False
    env = seal(ctx.identity, "specimen", specimen.to_dict())
    try:
        resp = httpx.post(
            f"{ctx.cfg.hub_url}/api/publish", json=env.to_dict(), timeout=30
        )
        resp.raise_for_status()
        ctx.note(f"published {specimen.id} to the Midway")
        return True
    except httpx.HTTPError as exc:
        queued = ctx.memory.outbox_dir / f"publish-{specimen.id}.json"
        queued.write_text(json.dumps(env.to_dict()), encoding="utf-8")
        ctx.note(f"Midway unreachable ({exc}); queued {specimen.id} in outbox")
        return False


def _flush_outbox(ctx: WakeContext) -> None:
    """Retry queued publishes from previous wakes."""
    if not ctx.cfg.hub_url:
        return
    for path in sorted(ctx.memory.outbox_dir.glob("publish-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            resp = httpx.post(
                f"{ctx.cfg.hub_url}/api/publish", json=payload, timeout=30
            )
            resp.raise_for_status()
            path.unlink()
            ctx.note(f"outbox: delivered {path.name}")
        except (httpx.HTTPError, ValueError):
            break  # hub still down; try again next wake


def _fetch_hub_mail(ctx: WakeContext) -> None:
    """Collect federation mail the Midway held for us while we slept."""
    if not ctx.cfg.hub_url:
        return
    fetch_env = seal(ctx.identity, "note", {"op": "fetch"})
    try:
        resp = httpx.post(
            f"{ctx.cfg.hub_url}/api/mail-fetch", json=fetch_env.to_dict(), timeout=15
        )
        resp.raise_for_status()
        mail = resp.json().get("mail", [])
    except (httpx.HTTPError, ValueError):
        return
    peers = ctx.memory.load_peers()
    for item in mail:
        try:
            env = Envelope.from_dict(item)
        except (KeyError, TypeError):
            continue
        pinned = peers.get(env.sender, {}).get("public_key") or None
        if not verify(env, pinned):
            continue
        # trust on first use: pin an unknown sender's key on first contact
        if env.sender not in peers:
            peers[env.sender] = {
                "public_key": env.public_key,
                "public_url": str(env.body.get("public_url", "")),
                "tagline": str(env.body.get("tagline", "")),
                "discovered": "message",
                "last_seen": utcnow(),
            }
        if env.kind == "hello":
            peers[env.sender] = {
                "public_url": env.body.get("public_url", ""),
                "public_key": env.public_key,
                "tagline": env.body.get("tagline", ""),
                "last_seen": utcnow(),
            }
        elif env.kind == "note":
            is_reply = bool(env.body.get("reply"))
            drop(
                ctx.memory.inbox_dir,
                Instruction(
                    title=(
                        f"Reply from {env.sender} (via the Midway)"
                        if is_reply
                        else f"Message from {env.sender} (via the Midway)"
                    ),
                    body=str(env.body.get("text", "")),
                    source="federation",
                    sender=env.sender,
                    # a reply is terminal; only a fresh message earns an answer
                    reply_to="" if is_reply else env.sender,
                    priority=6,
                ),
            )
            ctx.note(f"held {'reply' if is_reply else 'message'} from {env.sender}")
        elif env.kind == "instruct" and env.sender in ctx.cfg.trusted_handles:
            drop(
                ctx.memory.inbox_dir,
                Instruction(
                    title=str(env.body.get("title", f"Instruction from {env.sender}")),
                    body=str(env.body.get("text", "")),
                    source="federation",
                    sender=env.sender,
                    reply_to=env.sender,
                    priority=4,
                ),
            )
            ctx.note(f"held instruction from {env.sender}")
    ctx.memory.save_peers(peers)


def _register_with_hub(ctx: WakeContext) -> None:
    if not ctx.cfg.hub_url:
        return
    body = {
        "handle": ctx.identity.handle,
        "public_url": ctx.cfg.public_url,
        "tagline": ctx.cfg.tagline,
        "instrument": ctx.llm.describe(),
    }
    env = seal(ctx.identity, "hello", body)
    try:
        httpx.post(
            f"{ctx.cfg.hub_url}/api/register", json=env.to_dict(), timeout=15
        ).raise_for_status()
    except httpx.HTTPError:
        pass  # registration is repeated every wake; missing one is fine


def _discover_peers(ctx: WakeContext) -> None:
    """Learn the universe from the Midway's registry."""
    newly = messaging.discover_from_hub(ctx.cfg, ctx.identity, ctx.memory)
    if newly:
        ctx.note(f"discovered {len(newly)} peer(s) on the midway: {', '.join(newly)}")


def _greet_peers(ctx: WakeContext) -> None:
    """Say hello directly to peers so they learn our key and URL — both the
    ones named in config and the ones discovered on the midway."""
    peers = ctx.memory.load_peers()
    body = {
        "handle": ctx.identity.handle,
        "public_url": ctx.cfg.public_url,
        "tagline": ctx.cfg.tagline,
    }
    urls = list(ctx.cfg.peers)
    for info in peers.values():
        url = info.get("public_url", "")
        if url and url not in urls and url != ctx.cfg.public_url:
            urls.append(url)

    for peer_url in urls:
        env = seal(ctx.identity, "hello", body)
        try:
            resp = httpx.post(
                f"{peer_url}/api/federation/inbox", json=env.to_dict(), timeout=15
            )
            resp.raise_for_status()
            info = resp.json()
            if isinstance(info, dict) and info.get("handle"):
                known = peers.get(info["handle"], {})
                known.update(
                    {
                        "public_url": peer_url,
                        "public_key": info.get("public_key", ""),
                        "tagline": info.get("tagline", known.get("tagline", "")),
                        "last_seen": utcnow(),
                    }
                )
                peers[info["handle"]] = known
        except (httpx.HTTPError, ValueError):
            continue
    if peers:
        ctx.memory.save_peers(peers)


def run_wake(cfg: AgentConfig) -> WakeReport:
    """One full wake, synchronous. Called by the scheduler or /wake."""
    memory = Memory(cfg.home, cfg.name)
    memory.ensure()
    state = memory.load_state()
    identity = Identity.load_or_create(memory.keys_dir, cfg.name)
    ledger = Ledger(memory.treasury_dir)
    llm = build_backend(cfg)
    registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, cfg)
    ctx = WakeContext(cfg, memory, state, identity, ledger, llm, registry)

    wake_number = int(state.get("wakes", 0)) + 1
    ctx.note(f"wake {wake_number} at {utcnow()}")

    # 1b. discover the tools we have, fresh — including any we wrote before
    discovered = registry.discover()
    if discovered:
        ctx.note(f"discovered {len(discovered)} tool(s): {', '.join(discovered)}")

    # 2. read everything addressed to us ----------------------------------
    try:
        for ins in email_source.fetch_instructions(cfg):
            drop(memory.inbox_dir, ins)
            ctx.note(f"email from {ins.sender}: {ins.title}")
    except Exception as exc:
        ctx.note(f"email poll failed: {exc}")

    _register_with_hub(ctx)
    _discover_peers(ctx)
    _fetch_hub_mail(ctx)

    for ins in _gather_treasury_instructions(ctx):
        drop(memory.inbox_dir, ins)
        ctx.note(f"paid memo became instruction: {ins.title}")

    connectors: list[BaseConnector] = load_connectors(cfg)
    for connector in connectors:
        try:
            for ins in connector.gather(ctx):
                drop(memory.inbox_dir, ins)
                ctx.note(f"connector {connector.name}: {ins.title}")
        except Exception as exc:
            ctx.note(f"connector {connector.name} failed: {exc}")

    # 3. work -------------------------------------------------------------
    queue = pending(memory.inbox_dir)[: cfg.max_instructions_per_wake]
    worked: list[dict[str, str]] = []
    replies: list[tuple[str, str, str]] = []  # (channel, address, answer)
    tools_written: list[str] = []
    tools_used: list[str] = []
    for ins in queue:
        result = _work_instruction(ctx, ins)
        worked.append(result)
        tools_written.extend(result.get("tools_written", []))
        tools_used.extend(result.get("tools_used", []))
        if ins.reply_to:
            channel = "federation" if ins.source == "federation" else "email"
            replies.append((channel, ins.reply_to, result["answer"]))
        archive(ins, memory.archive_dir)

    # 4. write the specimen -------------------------------------------------
    specimen = _write_specimen(ctx, wake_number, worked)
    ctx.note(f"wrote specimen {specimen.id}: {specimen.title}")

    # 5. publish ------------------------------------------------------------
    _flush_outbox(ctx)
    _publish(ctx, specimen)

    # 6. answer mail, greet peers ------------------------------------------
    for channel, address, answer in replies:
        if channel == "federation":
            ok, how = messaging.deliver_note(
                cfg, identity, memory, address, answer, reply=True
            )
            ctx.note(
                f"replied to {address} over federation: "
                f"{how if ok else 'undeliverable'}"
            )
        else:
            sent = email_source.send_reply(
                cfg,
                address,
                f"[{cfg.name}] {specimen.title}",
                answer + f"\n\n— {cfg.name}, wake {wake_number}",
            )
            ctx.note(f"reply to {address}: {'sent' if sent else 'failed/skipped'}")
    for connector in connectors:
        try:
            connector.deliver(ctx, specimen)
        except Exception as exc:
            ctx.note(f"connector {connector.name} deliver failed: {exc}")
    _greet_peers(ctx)

    # 7. journal, state, sleep ---------------------------------------------
    memory.journal_append(
        f"\n## wake {wake_number} — {utcnow()}\n"
        + "\n".join(f"- {line}" for line in ctx.log)
    )
    state["wakes"] = wake_number
    state["last_wake"] = utcnow()
    memory.save_state(ctx.state)

    return WakeReport(
        wake_number, specimen, len(worked), ctx.log, tools_written, tools_used
    )


def next_wake_delay_seconds(cfg: AgentConfig) -> int:
    jitter = random.randint(-cfg.wake_jitter_minutes, cfg.wake_jitter_minutes)
    return max(60, (cfg.wake_interval_minutes + jitter) * 60)
