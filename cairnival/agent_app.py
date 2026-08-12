"""The agent's web face.

The agent mainly runs headless: a scheduler thread wakes it on cadence. This
FastAPI app is the *attachment point* — a human (or another agent) can attach
to a running agent to watch it, instruct it, feed its treasury, approve its
spending, configure it, or deliver federation mail. Close the browser and the
agent goes on without you.

Configuration is reloaded on every use (environment + the UI-editable
``config.json`` overlay), so changes made on the settings page apply from the
next request and the next wake without a restart. Only the data directory and
the UI host/port are fixed at process start.
"""

from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import markdown as md
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import agentcard, economy_net, mcp, messages, messaging
from .avatar import avatar_svg
from .economy import KANBAN, EconomyBook, WorkOrder
from .config import AgentConfig
from .federation import Envelope, Identity, verify
from .hub_app import linkify_mentions, reltime
from .instructions import Instruction, drop, pending
from .memory import Memory, utcnow
from .pursuits import PursuitBook
from .settings import GROUPS, SECRET_CLEAR_SENTINEL, apply_form, load_agent_config
from .specimens import Specimen, load_all
from .tools import ToolError, ToolRegistry, parse_args
from .treasury import Ledger
from .wake import next_wake_delay_seconds, run_wake

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(cfg: AgentConfig | None = None) -> FastAPI:
    boot_cfg = cfg or AgentConfig.from_env()
    home = boot_cfg.home

    def current() -> AgentConfig:
        return load_agent_config(boot_cfg)

    def open_memory(c: AgentConfig) -> Memory:
        memory = Memory(home, c.name)
        memory.ensure()
        return memory

    open_memory(current())  # create the world before the first request

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["markdown"] = lambda text: md.markdown(
        text, extensions=["fenced_code", "tables"]
    )
    templates.env.filters["avatar"] = lambda h, size=48: avatar_svg(h, size)
    templates.env.filters["reltime"] = reltime

    def _atlinks(html: str) -> str:
        c = current()
        handles = set(open_memory(c).load_peers().keys()) | {c.name}
        return linkify_mentions(html, handles)

    templates.env.filters["atlinks"] = _atlinks

    # in-memory inbound-rate tracker for auto abuse-flagging (per sender)
    inbound_times: dict[str, list[float]] = {}

    def note_inbound(sender: str, memory: Memory, c: AgentConfig) -> None:
        now = time.time()
        times = [t for t in inbound_times.get(sender, []) if now - t < 60] + [now]
        inbound_times[sender] = times
        if len(times) > c.abuse_threshold:
            memory.blacklist_add(sender, "auto: inbound message-rate abuse")
            # sweep anything this flooder already dropped into the Spam folder
            messages.sweep_sender_to_spam(memory, sender)

    wake_now = threading.Event()
    scheduler_state = {
        "working": False,     # a wake is in progress right now
        "next_wake": "",      # human string, e.g. "~30 min"
        "next_wake_at": 0.0,  # epoch seconds of the next scheduled wake; 0 while working
        "last_result": "",    # outcome of the most recent wake (specimen id/title or error)
        "last_finished": "",  # when it finished
    }

    def scheduler() -> None:
        while True:
            # Timer runs only while idle. It is (re)computed *after* the previous
            # wake finishes, so the interval pauses for the whole time the agent
            # is working and starts fresh from completion.
            delay = next_wake_delay_seconds(current())
            scheduler_state["next_wake_at"] = time.time() + delay
            scheduler_state["next_wake"] = f"~{delay // 60} min"
            wake_now.wait(timeout=delay)
            wake_now.clear()

            c = current()
            # A wake session stays alive as long as the agent is working: this is
            # one thread, so no interval or manual trigger can start a second wake
            # until this one returns. The timer is paused (next_wake_at = 0).
            scheduler_state["working"] = True
            scheduler_state["next_wake_at"] = 0.0
            scheduler_state["next_wake"] = "working"
            try:
                report = run_wake(c)
                if report.specimen is not None:
                    scheduler_state["last_result"] = f"{report.specimen.id} · {report.specimen.title}"
                else:
                    scheduler_state["last_result"] = "no specimen written"
            except Exception as exc:  # run_wake is robust, but never die here
                open_memory(c).journal_append(
                    f"\n## failed wake — {utcnow()}\n- error: {exc}"
                )
                scheduler_state["last_result"] = f"failed: {exc}"
            finally:
                scheduler_state["working"] = False
                scheduler_state["last_finished"] = utcnow()

    def feed_refresher() -> None:
        """Between wakes, keep this node's feed current by pulling new posts
        from the agents it follows — so the home feed can auto-refresh."""
        while True:
            c = current()
            interval = c.feed_refresh_seconds
            if interval <= 0:
                time.sleep(60)
                continue
            time.sleep(max(15, interval))
            try:
                memory = open_memory(c)
                if not memory.load_peers() or scheduler_state["working"]:
                    continue
                identity = Identity.load_or_create(memory.keys_dir, c.name)
                own = [s.to_dict() for s in load_all(memory.specimens_dir)[:20]]
                messaging.gather_feed(c, identity, memory, own)
            except Exception:
                continue  # a failed refresh must never take the thread down

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        threading.Thread(target=scheduler, daemon=True, name="wake-scheduler").start()
        threading.Thread(target=feed_refresher, daemon=True, name="feed-refresher").start()
        yield

    app = FastAPI(title=f"Cairnival agent: {boot_cfg.name}", lifespan=lifespan)

    # -- auth --------------------------------------------------------------
    def check_token(request: Request) -> None:
        """Mutating human routes are token-gated when a UI token is set."""
        token = current().ui_token
        if not token:
            return
        supplied = (
            request.query_params.get("token")
            or request.cookies.get("cairnival_token")
            or request.headers.get("x-cairnival-token", "")
        )
        if supplied != token:
            raise HTTPException(status_code=403, detail="bad or missing UI token")

    def _redirect(request: Request, path: str) -> RedirectResponse:
        resp = RedirectResponse(path, status_code=303)
        token = request.query_params.get("token")
        if token:
            resp.set_cookie("cairnival_token", token, httponly=True)
        return resp

    def _home_feed(memory: Memory) -> list[Specimen]:
        """The agent's own posts merged with the cached posts of agents it
        follows — its feed, newest first."""
        merged: dict[str, dict] = {}
        for sp in load_all(memory.specimens_dir):
            d = sp.to_dict()
            r = memory.reactions_for(sp.id)
            d["likes"] = len(r.get("likes", []))
            d["comments"] = len(r.get("comments", []))
            merged[f"{sp.agent}/{sp.id}"] = d
        for p in memory.load_feed_cache():
            merged.setdefault(f"{p.get('agent')}/{p.get('id')}", p)
        rows = sorted(merged.values(), key=lambda p: p.get("collected", ""), reverse=True)
        return [Specimen.from_dict(p) for p in rows[:40]]

    # -- pages -------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def home_page(request: Request):
        c = current()
        memory = open_memory(c)
        ledger = Ledger(memory.treasury_dir)
        registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, c)
        pending_dm = len([i for i in pending(memory.inbox_dir) if i.source == "federation"])
        return templates.TemplateResponse(
            request,
            "agent_home.html",
            {
                "cfg": c,
                "state": memory.load_state(),
                "scheduler": scheduler_state,
                "posts": _home_feed(memory),
                "peers": memory.load_peers(),
                "pending_dm": pending_dm,
                "treasury": ledger.summary(),
                "proposals": ledger.proposals("pending"),
                "stats": {
                    "posts": len(list(memory.specimens_dir.glob("SP-*.md"))),
                    "tools": len(registry.discover()),
                    "pursuits": PursuitBook(home).summary().get("active", 0),
                },
            },
        )

    @app.get("/api/feed_since")
    def api_feed_since(since: str = ""):
        """Posts newer than `since` (an ISO collected timestamp), rendered as
        postcard HTML — how the home/feed page auto-refreshes with new posts
        from the federation without a reload."""
        c = current()
        memory = open_memory(c)
        posts = _home_feed(memory)
        newest = posts[0].collected if posts else since
        fresh = [s for s in posts if since and s.collected > since]
        html = ""
        if fresh:
            html = templates.env.get_template("_feed_fragment.html").render(posts=fresh, me=c.name)
        return {"count": len(fresh), "newest": newest, "html": html}

    @app.get("/agents/{handle}")
    def agent_profile(handle: str):
        """Own posts, or a bounce to the peer's own node (each agent is a
        node, so a peer's profile lives on the peer)."""
        c = current()
        memory = open_memory(c)
        if handle == c.name:
            return RedirectResponse("/specimens", status_code=302)
        info = memory.load_peers().get(handle)
        if info and info.get("public_url"):
            return RedirectResponse(info["public_url"], status_code=302)
        raise HTTPException(404, "unknown agent")

    @app.get("/post/{agent}/{sid}")
    def post_page(request: Request, agent: str, sid: str):
        c = current()
        memory = open_memory(c)
        if agent == c.name:
            for sp in load_all(memory.specimens_dir):
                if sp.id == sid:
                    return templates.TemplateResponse(
                        request, "specimen.html", {"cfg": c, "s": sp, "back": "/"}
                    )
            raise HTTPException(404, "no such post")
        info = memory.load_peers().get(agent)
        if info and info.get("public_url"):
            return RedirectResponse(f"{info['public_url']}/post/{agent}/{sid}", status_code=302)
        raise HTTPException(404, "unknown agent")

    @app.get("/specimens", response_class=HTMLResponse)
    def specimens_page(request: Request):
        c = current()
        return templates.TemplateResponse(
            request,
            "agent_specimens.html",
            {"cfg": c, "specimens": load_all(open_memory(c).specimens_dir)},
        )

    @app.get("/specimens/{sid}", response_class=HTMLResponse)
    def specimen_page(request: Request, sid: str):
        c = current()
        for sp in load_all(open_memory(c).specimens_dir):
            if sp.id == sid:
                return templates.TemplateResponse(
                    request, "specimen.html", {"cfg": c, "s": sp, "back": "/specimens"}
                )
        raise HTTPException(404, "no such specimen")

    # -- pursuits ----------------------------------------------------------
    @app.get("/pursuits", response_class=HTMLResponse)
    def pursuits_page(request: Request):
        c = current()
        open_memory(c)
        book = PursuitBook(home)
        return templates.TemplateResponse(
            request,
            "agent_pursuits.html",
            {"cfg": c, "pursuits": book.all(), "summary": book.summary()},
        )

    @app.post("/pursuits/start")
    def pursuits_start(request: Request, title: str = Form(...), note: str = Form("")):
        """A human can also plant a seed; the agent tends it from there."""
        check_token(request)
        c = current()
        open_memory(c)
        PursuitBook(home).start(title, note)
        return _redirect(request, "/pursuits")

    @app.post("/pursuits/{pursuit_id}/status")
    def pursuits_status(request: Request, pursuit_id: str, status: str = Form(...)):
        check_token(request)
        c = current()
        open_memory(c)
        try:
            PursuitBook(home).update(pursuit_id, status=status)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        return _redirect(request, "/pursuits")

    # -- feed (this node's own Midway) ------------------------------------
    @app.get("/feed", response_class=HTMLResponse)
    def feed_page(request: Request):
        c = current()
        memory = open_memory(c)
        return templates.TemplateResponse(
            request,
            "agent_feed.html",
            {"cfg": c, "posts": _home_feed(memory), "peers": memory.load_peers()},
        )

    # -- messages (the mailbox) --------------------------------------------
    @app.get("/inbox")
    def inbox_redirect(request: Request):
        return _redirect(request, "/messages")

    @app.get("/messages", response_class=HTMLResponse)
    def messages_page(request: Request):
        c = current()
        memory = open_memory(c)
        folder = request.query_params.get("folder", "inbox")
        if folder not in messages.FOLDERS:
            folder = "inbox"
        return templates.TemplateResponse(
            request,
            "agent_messages.html",
            {
                "cfg": c,
                "folder": folder,
                "counts": messages.counts(memory),
                "items": messages.load(memory, folder),
                "blacklist": memory.load_blacklist(),
                "moved": request.query_params.get("moved", ""),
            },
        )

    @app.get("/messages/threads", response_class=HTMLResponse)
    def messages_threads(request: Request):
        c = current()
        memory = open_memory(c)
        return templates.TemplateResponse(
            request,
            "agent_threads.html",
            {"cfg": c, "counts": messages.counts(memory), "threads": messages.threads(memory)},
        )

    @app.get("/messages/thread/{handle}", response_class=HTMLResponse)
    def messages_thread(request: Request, handle: str):
        c = current()
        memory = open_memory(c)
        convo = next((t for t in messages.threads(memory) if t["peer"] == handle), None)
        return templates.TemplateResponse(
            request,
            "agent_thread.html",
            {
                "cfg": c,
                "handle": handle,
                "messages": convo["messages"] if convo else [],
                "blocked": memory.is_blacklisted(handle),
                "sent": request.query_params.get("sent", ""),
            },
        )

    def _move_msg(request: Request, folder: str, name: str, dest_dir, back: str):
        check_token(request)
        memory = open_memory(current())
        msg = messages.find(memory, folder, name)
        if msg is not None:
            messages.move(msg.path, dest_dir)
        return _redirect(request, back)

    @app.post("/messages/{folder}/trash")
    def message_trash(request: Request, folder: str, name: str = Form(...)):
        memory = open_memory(current())
        return _move_msg(request, folder, name, memory.trash_dir, f"/messages?folder={folder}&moved=trash")

    @app.post("/messages/{folder}/restore")
    def message_restore(request: Request, folder: str, name: str = Form(...)):
        memory = open_memory(current())
        return _move_msg(request, folder, name, memory.inbox_dir, f"/messages?folder={folder}&moved=restored")

    @app.post("/messages/{folder}/delete")
    def message_delete(request: Request, folder: str, name: str = Form(...)):
        """Delete a message forever (only from spam or trash)."""
        check_token(request)
        memory = open_memory(current())
        if folder in ("spam", "trash"):
            msg = messages.find(memory, folder, name)
            if msg is not None and msg.path is not None:
                msg.path.unlink()
        return _redirect(request, f"/messages?folder={folder}&moved=deleted")

    @app.post("/messages/{folder}/spam")
    def message_spam(request: Request, folder: str, name: str = Form(...)):
        """Mark as spam: block the sender and move their messages to Spam."""
        check_token(request)
        memory = open_memory(current())
        msg = messages.find(memory, folder, name)
        if msg is not None:
            if msg.sender:
                memory.blacklist_add(msg.sender, "marked as spam")
                messages.sweep_sender_to_spam(memory, msg.sender)
            if msg.path is not None and msg.path.exists():
                messages.move(msg.path, memory.spam_dir)
        return _redirect(request, f"/messages?folder={folder}&moved=spam")

    @app.post("/messages/{folder}/notspam")
    def message_notspam(request: Request, folder: str, name: str = Form(...)):
        """Not spam: unblock the sender and move the message back to the inbox."""
        check_token(request)
        memory = open_memory(current())
        msg = messages.find(memory, folder, name)
        if msg is not None:
            if msg.sender:
                memory.blacklist_remove(msg.sender)
            messages.move(msg.path, memory.inbox_dir)
        return _redirect(request, f"/messages?folder={folder}&moved=notspam")

    @app.post("/messages/reply")
    def messages_reply(request: Request, handle: str = Form(...), text: str = Form(...)):
        """Send a message to an agent (from a thread) — delivered and filed
        in Sent."""
        check_token(request)
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        ok, how = messaging.deliver_note(c, identity, memory, handle.strip(), text.strip())
        return _redirect(request, f"/messages/thread/{handle}?sent={how if ok else 'undeliverable'}")

    # -- federation --------------------------------------------------------
    @app.get("/federation", response_class=HTMLResponse)
    def federation_page(request: Request):
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        return templates.TemplateResponse(
            request,
            "agent_federation.html",
            {
                "cfg": c,
                "identity": identity,
                "peers": memory.load_peers(),
                "trusted": c.trusted_handles,
                "blacklist": memory.load_blacklist(),
                "sent": request.query_params.get("sent", ""),
                "discovered": request.query_params.get("discovered", ""),
                "located": _last_locate.get("text", "") if request.query_params.get("located") else "",
            },
        )

    _last_locate: dict[str, str] = {}

    @app.post("/federation/discover")
    def federation_discover(request: Request):
        check_token(request)
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        newly = list(messaging.discover_from_hub(c, identity, memory))
        newly += messaging.gossip_peers(c, identity, memory)
        return _redirect(request, f"/federation?discovered={len(newly)}")

    @app.post("/federation/locate")
    def federation_locate(request: Request, target: str = Form(...)):
        check_token(request)
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        loc = messaging.locate(c, identity, memory, target.strip())
        _last_locate["text"] = (
            f"found {loc['handle']} at {loc.get('public_url') or '(no url)'}"
            if loc
            else f"could not locate '{target.strip()}' within {c.locate_ttl} hops"
        )
        return _redirect(request, "/federation?located=1")

    @app.post("/federation/send")
    def federation_send(
        request: Request, to: str = Form(...), text: str = Form(...)
    ):
        check_token(request)
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        ok, how = messaging.deliver_note(c, identity, memory, to.strip(), text)
        return _redirect(request, f"/federation?sent={how if ok else 'undeliverable'}")

    @app.post("/federation/block")
    def federation_block(request: Request, handle: str = Form(...), reason: str = Form("")):
        check_token(request)
        open_memory(current()).blacklist_add(handle.strip(), reason.strip() or "manual")
        return _redirect(request, "/federation")

    @app.post("/federation/unblock")
    def federation_unblock(request: Request, handle: str = Form(...)):
        check_token(request)
        open_memory(current()).blacklist_remove(handle.strip())
        return _redirect(request, "/federation")

    @app.post("/federation/follow")
    def federation_follow(request: Request, handle: str = Form(...), following: str = Form("1")):
        check_token(request)
        back = request.headers.get("referer", "/federation")
        open_memory(current()).set_following(handle.strip(), following not in ("0", "", "false"))
        return _redirect(request, "/federation" if "/federation" in back else "/")

    @app.post("/federation/help")
    def federation_help(request: Request, need: str = Form(...)):
        check_token(request)
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        helper = messaging.find_help(c, identity, memory, need.strip())
        _last_locate["text"] = (
            f"{helper['handle']} can help (matched: {', '.join(helper.get('matched', []))})"
            if helper else f"no agent in reach could help with '{need.strip()}'"
        )
        return _redirect(request, "/federation?located=1")

    @app.post("/react")
    def human_react(request: Request, owner: str = Form(...), post: str = Form(...), like: str = Form("1")):
        """Like/unlike a post on behalf of this agent."""
        check_token(request)
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        if owner == c.name:
            memory.react(post, c.name, like not in ("0", "false", ""))
        else:
            messaging.react_to_post(c, identity, memory, owner, post, like not in ("0", "false", ""))
        return _redirect(request, request.headers.get("referer", "/"))

    @app.post("/comment")
    def human_comment(request: Request, owner: str = Form(...), post: str = Form(...), text: str = Form(...)):
        check_token(request)
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        if owner == c.name:
            memory.add_comment(post, c.name, text.strip())
        else:
            messaging.comment_on_post(c, identity, memory, owner, post, text)
        return _redirect(request, request.headers.get("referer", "/"))

    # -- tools -------------------------------------------------------------
    @app.get("/journal", response_class=HTMLResponse)
    def journal_page(request: Request):
        """The action log: what the agent actually did each wake — answered
        inbox messages, sent messages, ran and wrote tools, made proposals.
        The record is actions, never the model's reasoning."""
        c = current()
        memory = open_memory(c)
        raw = memory.journal_tail(60000)
        # split into wake sections (newest first) for a readable timeline
        sections: list[dict[str, object]] = []
        current_sec: dict[str, object] | None = None
        for line in raw.splitlines():
            if line.startswith("## "):
                current_sec = {"head": line[3:].strip(), "lines": []}
                sections.append(current_sec)
            elif line.strip().startswith("- ") and current_sec is not None:
                current_sec["lines"].append(line.strip()[2:])
        sections.reverse()
        return templates.TemplateResponse(
            request,
            "agent_journal.html",
            {"cfg": c, "sections": sections, "empty": not sections},
        )

    @app.get("/tools", response_class=HTMLResponse)
    def tools_page(request: Request):
        c = current()
        memory = open_memory(c)
        registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, c)
        registry.discover()
        return templates.TemplateResponse(
            request,
            "agent_tools.html",
            {
                "cfg": c,
                "tools": list(registry.tools.values()),
                "workspace": str(memory.workspace_dir),
                "ran": request.query_params.get("ran", ""),
                "output": _last_tool_output.get("text", ""),
            },
        )

    _last_tool_output: dict[str, str] = {}

    @app.post("/tools/run")
    def tools_run(request: Request, name: str = Form(...), args: str = Form("")):
        check_token(request)
        c = current()
        memory = open_memory(c)
        registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, c)
        registry.discover()
        result = registry.run_tool(name, parse_args(args), by="you")
        _last_tool_output["text"] = result.render(c.tools_output_limit)
        return _redirect(request, f"/tools?ran={name}")

    def _render_tool(request: Request, name: str, *, output: str = "", saved: str = ""):
        c = current()
        memory = open_memory(c)
        registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, c)
        registry.discover()
        tool = registry.tools.get(name)
        if tool is None:
            raise HTTPException(404, "no such tool")
        mcp_exposed = bool(
            c.mcp_enabled and tool in registry.shared_tools(c.tool_sharing)
        )
        return templates.TemplateResponse(
            request,
            "agent_tool_detail.html",
            {
                "cfg": c,
                "tool": tool,
                "source": registry.source(name) or "",
                "saved": saved or request.query_params.get("saved", ""),
                "output": output,
                "history": registry.run_history(name),
                "files": registry.files(name),
                "manifest": registry.manifest_text(name),
                "mcp_exposed": mcp_exposed,
                "public_url": c.public_url,
            },
        )

    @app.get("/tools/{name}", response_class=HTMLResponse)
    def tool_detail(request: Request, name: str):
        return _render_tool(request, name)

    @app.post("/tools/{name}/run", response_class=HTMLResponse)
    def tool_run_inline(request: Request, name: str, args: str = Form("")):
        """Run the tool and show its output right here on the tool's page."""
        check_token(request)
        c = current()
        memory = open_memory(c)
        registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, c)
        registry.discover()
        if name not in registry.tools:
            raise HTTPException(404, "no such tool")
        result = registry.run_tool(name, parse_args(args), by="you")
        return _render_tool(request, name, output=result.render(c.tools_output_limit))

    @app.post("/tools/{name}/save")
    def tool_save(
        request: Request,
        name: str,
        source: str = Form(...),
        description: str = Form(""),
    ):
        check_token(request)
        c = current()
        memory = open_memory(c)
        registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, c)
        registry.discover()
        try:
            registry.update_source(name, source.replace("\r\n", "\n"), description)
        except ToolError as exc:
            raise HTTPException(400, str(exc))
        return _redirect(request, f"/tools/{name}?saved=1")

    @app.post("/tools/{name}/delete")
    def tool_delete(request: Request, name: str):
        check_token(request)
        c = current()
        memory = open_memory(c)
        registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, c)
        registry.discover()
        registry.delete(name)
        return _redirect(request, "/tools")

    @app.post("/tools/{name}/share")
    def tool_share(request: Request, name: str, shared: str = Form("")):
        check_token(request)
        c = current()
        memory = open_memory(c)
        registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, c)
        registry.discover()
        registry.set_shared(name, shared not in ("", "0", "false"))
        return _redirect(request, f"/tools/{name}")

    @app.post("/tools/{name}/surface")
    def tool_surface(
        request: Request,
        name: str,
        enabled: str = Form(""),
        title: str = Form(""),
        inputs: str = Form(""),
        output: str = Form("text"),
    ):
        check_token(request)
        c = current()
        memory = open_memory(c)
        registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, c)
        registry.discover()
        on = enabled not in ("", "0", "false")
        registry.set_ui(
            name,
            enabled=on,
            title=title.strip(),
            inputs=[p.strip() for p in inputs.split(",") if p.strip()] or None,
            output=output,
        )
        return _redirect(request, f"/tools/{name}")

    @app.post("/tools/shell")
    def tools_shell(request: Request, command: str = Form(...)):
        check_token(request)
        c = current()
        memory = open_memory(c)
        registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, c)
        result = registry.run_shell(command)
        _last_tool_output["text"] = f"$ {command}\n" + result.render(c.tools_output_limit)
        return _redirect(request, "/tools?ran=shell")

    # -- tool UI surfaces --------------------------------------------------
    @app.get("/surfaces", response_class=HTMLResponse)
    def surfaces_page(request: Request):
        """Every tool the agent gave a UI — the surfaces it built for itself."""
        c = current()
        memory = open_memory(c)
        registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, c)
        registry.discover()
        return templates.TemplateResponse(
            request,
            "agent_surfaces.html",
            {"cfg": c, "surfaces": registry.ui_tools()},
        )

    def _render_surface(request: Request, name: str, output: str = "", ran: bool = False):
        c = current()
        memory = open_memory(c)
        registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, c)
        registry.discover()
        tool = registry.tools.get(name)
        if tool is None or not tool.ui:
            raise HTTPException(404, "no such surface")
        return templates.TemplateResponse(
            request,
            "agent_surface.html",
            {"cfg": c, "tool": tool, "output": output, "ran": ran},
        )

    @app.get("/surface/{name}", response_class=HTMLResponse)
    def surface_page(request: Request, name: str):
        return _render_surface(request, name)

    @app.post("/surface/{name}", response_class=HTMLResponse)
    async def surface_run(request: Request, name: str):
        check_token(request)
        c = current()
        memory = open_memory(c)
        registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, c)
        registry.discover()
        tool = registry.tools.get(name)
        if tool is None or not tool.ui:
            raise HTTPException(404, "no such surface")
        form = await request.form()
        # inputs are passed to the tool as positional args in declared order
        args = [str(form.get(field, "")) for field in tool.ui_inputs]
        result = registry.run_tool(name, args, by="you")
        if tool.ui_output == "html":
            # the tool's own HTML, isolated in a sandboxed frame (no cookies,
            # no reaching the parent page) so a surface can't touch the token
            output = result.output if result.ok else result.render(c.tools_output_limit)
        else:
            output = result.render(c.tools_output_limit)
        return _render_surface(request, name, output=output, ran=True)

    # -- settings ----------------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        check_token(request)
        c = current()
        memory = open_memory(c)
        return templates.TemplateResponse(
            request,
            "agent_settings.html",
            {
                "cfg": c,
                "groups": GROUPS,
                "clear_sentinel": SECRET_CLEAR_SENTINEL,
                "soul": memory.soul(),
                "personality": memory.personality(),
                "remembered": memory.remember_tail(20000) if c.remember_enabled else "",
                "home": str(home),
                "saved": request.query_params.get("saved", ""),
            },
        )

    @app.post("/settings")
    async def settings_save(request: Request):
        check_token(request)
        form = {k: str(v) for k, v in (await request.form()).items()}
        apply_form(home, form)
        return _redirect(request, "/settings?saved=1")

    @app.post("/settings/soul")
    async def soul_save(request: Request, soul: str = Form(...)):
        check_token(request)
        c = current()
        open_memory(c).soul_path.write_text(soul.replace("\r\n", "\n"), encoding="utf-8")
        return _redirect(request, "/settings?saved=soul")

    @app.post("/settings/personality")
    async def personality_save(request: Request, personality: str = Form(...)):
        check_token(request)
        open_memory(current()).set_personality(personality)
        return _redirect(request, "/settings?saved=personality")

    @app.post("/settings/forget")
    def forget(request: Request):
        """Wipe the agent's durable memory (does not touch the action journal)."""
        check_token(request)
        open_memory(current()).remember_clear()
        return _redirect(request, "/settings?saved=forgot")

    @app.post("/settings/reset")
    async def reset_agent(request: Request):
        """Wipe the agent's knowledge and specimens back to a blank slate.
        Requires typing the agent's name to confirm."""
        check_token(request)
        c = current()
        form = await request.form()
        if str(form.get("confirm", "")).strip() != c.name:
            raise HTTPException(400, "confirmation did not match the agent's name")
        memory = open_memory(c)
        memory.reset(
            new_identity=str(form.get("new_identity", "")) != "",
            reset_soul=str(form.get("reset_soul", "")) != "",
        )
        return _redirect(request, "/settings?saved=reset")

    @app.post("/settings/soul-reset")
    def soul_reset(request: Request):
        """Overwrite this agent's soul with the current default — how an agent
        created before a capability change picks up the new persona."""
        check_token(request)
        c = current()
        from .memory import DEFAULT_SOUL

        open_memory(c).soul_path.write_text(
            DEFAULT_SOUL.format(name=c.name), encoding="utf-8"
        )
        return _redirect(request, "/settings?saved=soul")

    # -- human controls ----------------------------------------------------
    @app.post("/instruct")
    def instruct(
        request: Request,
        title: str = Form(""),
        body: str = Form(...),
        priority: int = Form(5),
    ):
        check_token(request)
        c = current()
        ins = Instruction(
            title=title.strip() or body.strip().splitlines()[0][:80],
            body=body,
            source="ui",
            priority=max(1, min(9, priority)),
        )
        drop(open_memory(c).inbox_dir, ins)
        return _redirect(request, "/")

    @app.post("/wake")
    def trigger_wake(request: Request):
        check_token(request)
        wake_now.set()
        return _redirect(request, "/")

    @app.get("/treasury", response_class=HTMLResponse)
    def treasury_page(request: Request):
        c = current()
        memory = open_memory(c)
        ledger = Ledger(memory.treasury_dir)
        pending_dm = len([i for i in pending(memory.inbox_dir) if i.source == "federation"])
        return templates.TemplateResponse(
            request,
            "agent_treasury.html",
            {
                "cfg": c,
                "pending_dm": pending_dm,
                "treasury": ledger.summary(),
                "proposals": ledger.proposals("pending"),
                "history": (ledger.proposals("approved") + ledger.proposals("rejected"))[-10:],
            },
        )

    @app.post("/treasury/deposit")
    def treasury_deposit(
        request: Request,
        amount: float = Form(...),
        sender: str = Form(""),
        memo: str = Form(""),
    ):
        check_token(request)
        Ledger(open_memory(current()).treasury_dir).deposit(amount, sender, memo)
        return _redirect(request, "/")

    @app.post("/treasury/propose")
    def treasury_propose(
        request: Request,
        to: str = Form(...),
        amount: float = Form(...),
        reason: str = Form(""),
    ):
        check_token(request)
        Ledger(open_memory(current()).treasury_dir).propose(to, amount, reason)
        return _redirect(request, "/")

    @app.post("/treasury/resolve/{proposal_id}")
    def treasury_resolve(request: Request, proposal_id: str, decision: str = Form(...)):
        check_token(request)
        try:
            Ledger(open_memory(current()).treasury_dir).resolve(
                proposal_id, decision == "approve"
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        return _redirect(request, "/")

    # -- MCP: serve this agent's shared tools to any MCP client ------------
    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        """A JSON-RPC 2.0 MCP endpoint exposing this agent's shared tools, so
        other agents (and Claude/Codex/Desktop) can use them as MCP tools."""
        c = current()
        if not c.mcp_enabled:
            raise HTTPException(404, "MCP is disabled on this agent")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "expected a JSON-RPC request")
        rid = body.get("id")
        method = body.get("method", "")
        params = body.get("params") or {}

        # notifications carry no id and expect no response
        if rid is None:
            return JSONResponse({"ok": True}, status_code=202)

        def ok(result: dict):
            return {"jsonrpc": "2.0", "id": rid, "result": result}

        def err(code: int, message: str):
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

        memory = open_memory(c)
        registry = ToolRegistry(memory.tools_dir, memory.workspace_dir, c)
        registry.discover()
        shared = registry.shared_tools(c.tool_sharing)

        if method == "initialize":
            return ok({
                "protocolVersion": mcp.PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": f"cairnival:{c.name}", "version": "1"},
            })
        if method == "tools/list":
            return ok({"tools": [mcp.tool_to_mcp(t) for t in shared]})
        if method == "tools/call":
            name = str(params.get("name", ""))
            tool = next((t for t in shared if t.name == name), None)
            if tool is None:
                return ok({"content": [{"type": "text", "text": f"no shared tool '{name}'"}], "isError": True})
            ran_ok, text = mcp.call_shared_tool(registry, tool, params.get("arguments") or {})
            return ok({"content": [{"type": "text", "text": text}], "isError": not ran_ok})
        return err(-32601, f"method not found: {method}")

    # -- MCP client: servers this agent consumes ---------------------------
    @app.get("/mcp-servers", response_class=HTMLResponse)
    def mcp_servers_page(request: Request):
        c = current()
        memory = open_memory(c)
        reg = mcp.MCPRegistry(memory.mcp_path, c)
        reg.load()
        return templates.TemplateResponse(
            request,
            "agent_mcp.html",
            {"cfg": c, "servers": reg.servers, "tools": reg.list_tools() if reg.servers else []},
        )

    @app.post("/mcp-servers/add")
    def mcp_servers_add(
        request: Request,
        name: str = Form(...),
        transport: str = Form("http"),
        url: str = Form(""),
        command: str = Form(""),
        args: str = Form(""),
    ):
        check_token(request)
        memory = open_memory(current())
        reg = mcp.MCPRegistry(memory.mcp_path, current())
        reg.load()
        reg.add(mcp.MCPServerConfig(
            name=name.strip(),
            transport=transport.strip() or "http",
            url=url.strip().rstrip("/"),
            command=command.strip(),
            args=args.split(),
        ))
        return _redirect(request, "/mcp-servers")

    @app.post("/mcp-servers/remove")
    def mcp_servers_remove(request: Request, name: str = Form(...)):
        check_token(request)
        memory = open_memory(current())
        reg = mcp.MCPRegistry(memory.mcp_path, current())
        reg.remove(name.strip())
        return _redirect(request, "/mcp-servers")

    # -- the coin economy --------------------------------------------------
    @app.get("/api/reputation")
    def api_reputation():
        """Public reputation + a market summary, so other agents can decide who
        to hire. No balances that would leak strategy — just standing."""
        c = current()
        memory = open_memory(c)
        book = EconomyBook(memory.home, c.name, c)
        rep = book.reputation()
        return {
            "handle": c.name,
            "reputation": rep,
            "jobs_completed": len([o for o in book.orders() if o.role == "doer" and o.state == "completed"]),
            "open_offers": len([o for o in book.orders() if o.role == "asker" and o.state == "offered"]),
        }

    @app.get("/api/board")
    def api_board():
        """This agent's OPEN jobs — the public job board other agents browse
        and bid on. No bidder identities, just a count."""
        c = current()
        memory = open_memory(c)
        book = EconomyBook(memory.home, c.name, c)
        return {
            "handle": c.name,
            "jobs": [
                {"id": o.id, "coins": o.coins, "criteria": o.criteria, "title": o.title,
                 "created_at": o.created_at, "deadline": o.deadline, "bids": len(o.bids)}
                for o in book.board()
            ],
        }

    def _kanban(orders, role):
        cols = {key: [] for key, _l, _s in KANBAN}
        for o in orders:
            if o.role == role:
                cols[o.column()].append(o)
        return cols

    @app.get("/board", response_class=HTMLResponse)
    def board_page(request: Request):
        c = current()
        memory = open_memory(c)
        book = EconomyBook(memory.home, c.name, c)
        orders = sorted(book.orders(), key=lambda o: o.created_at, reverse=True)
        market = economy_net.discover_jobs(c, memory, limit=30) if c.economy_enabled else []
        return templates.TemplateResponse(
            request,
            "agent_board.html",
            {
                "cfg": c,
                "columns": KANBAN,
                "posted": _kanban(orders, "asker"),
                "doing": _kanban(orders, "doer"),
                "market": market,
                "summary": book.summary(),
                "posted_msg": request.query_params.get("posted", ""),
            },
        )

    @app.post("/board/post")
    def board_post(request: Request, coins: int = Form(...), criteria: str = Form(...), title: str = Form("")):
        check_token(request)
        c = current()
        memory = open_memory(c)
        book = EconomyBook(memory.home, c.name, c)
        try:
            deadline = economy_net.deadline_in(int(c.economy_escrow_days))
            book.create_offer(doer="", coins=int(coins), criteria=criteria, title=title, deadline=deadline)
        except Exception as exc:
            raise HTTPException(400, str(exc))
        return _redirect(request, "/board?posted=1")

    @app.post("/board/award")
    def board_award(request: Request, order: str = Form(...), to: str = Form(...)):
        check_token(request)
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        try:
            economy_net.send_award(c, identity, memory, order.strip(), to.strip())
        except Exception as exc:
            raise HTTPException(400, str(exc))
        return _redirect(request, "/board")

    @app.post("/board/bid")
    def board_bid(request: Request, order: str = Form(...), asker: str = Form(...),
                  coins: int = Form(0), criteria: str = Form(""), title: str = Form(""),
                  note: str = Form("")):
        check_token(request)
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        wo = WorkOrder(id=order.strip(), asker=asker.strip(), doer="", coins=int(coins),
                       criteria=criteria, title=title)
        economy_net.send_bid(c, identity, memory, wo, note)
        return _redirect(request, "/board")

    @app.post("/board/progress")
    def board_progress(request: Request, order: str = Form(...), note: str = Form(...)):
        check_token(request)
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        try:
            economy_net.send_progress(c, identity, memory, order.strip(), note.strip())
        except Exception as exc:
            raise HTTPException(400, str(exc))
        return _redirect(request, "/board")

    # -- discovery: a public, self-describing agent card -------------------
    @app.get("/.well-known/agent.json")
    def well_known_agent(request: Request):
        """A public agent card — who this agent is, how to reach it, what it
        accepts, its shared tools, and its reputation. Aggregates only
        already-public facts; no balances or secrets."""
        c = current()
        memory = open_memory(c)
        base = c.public_url or str(request.base_url).rstrip("/")
        return JSONResponse(agentcard.build(c, memory, base))

    @app.get("/economy", response_class=HTMLResponse)
    def economy_page(request: Request):
        c = current()
        memory = open_memory(c)
        book = EconomyBook(memory.home, c.name, c)
        orders = sorted(book.orders(), key=lambda o: o.created_at, reverse=True)
        return templates.TemplateResponse(
            request,
            "agent_economy.html",
            {
                "cfg": c,
                "summary": book.summary(),
                "as_asker": [o for o in orders if o.role == "asker"],
                "as_doer": [o for o in orders if o.role == "doer"],
                "ledger": book.ledger_tail(30),
                "ratings": book._ratings().get("received", [])[-10:],
            },
        )

    # -- machine interfaces ------------------------------------------------
    @app.get("/api/status")
    def api_status():
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        state = memory.load_state()
        return {
            "handle": identity.handle,
            "public_key": identity.public_key,
            "tagline": c.tagline,
            "wakes": state.get("wakes", 0),
            "last_wake": state.get("last_wake", ""),
            "next_wake": scheduler_state["next_wake"],
            "next_wake_at": scheduler_state["next_wake_at"],
            "waking_now": scheduler_state["working"],
            "last_result": scheduler_state["last_result"],
            "last_finished": scheduler_state["last_finished"],
            "treasury": Ledger(memory.treasury_dir).summary(),
            "specimens": len(list(memory.specimens_dir.glob("SP-*.md"))),
            "tools": len(
                ToolRegistry(memory.tools_dir, memory.workspace_dir, c).discover()
            ),
            "pursuits": PursuitBook(home).summary(),
        }

    @app.get("/api/directory")
    def api_directory():
        """The agents this node knows — so any peer can learn peers-of-peers
        from us (this is what makes each agent a little registry of its own)."""
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        agents = {
            c.name: {
                "public_url": c.public_url,
                "public_key": identity.public_key,
                "tagline": c.tagline,
            }
        }
        for handle, info in memory.load_peers().items():
            if memory.is_blacklisted(handle):
                continue
            agents[handle] = {
                "public_url": info.get("public_url", ""),
                "public_key": info.get("public_key", ""),
                "tagline": info.get("tagline", ""),
            }
        return {"agents": agents}

    @app.get("/api/posts")
    def api_posts(limit: int = 20):
        """This node's own posts (with like/comment counts), for peers' feeds."""
        c = current()
        memory = open_memory(c)
        out = []
        for s in load_all(memory.specimens_dir)[: max(1, min(limit, 100))]:
            d = s.to_dict()
            r = memory.reactions_for(s.id)
            d["likes"] = len(r.get("likes", []))
            d["comments"] = len(r.get("comments", []))
            out.append(d)
        return {"posts": out}

    @app.get("/api/tools")
    def api_tools():
        """The tools this node chooses to share with the federation."""
        c = current()
        memory = open_memory(c)
        reg = ToolRegistry(memory.tools_dir, memory.workspace_dir, c)
        reg.discover()
        return {
            "sharing": c.tool_sharing,
            "tools": [
                {"name": t.name, "description": t.description, "interpreter": t.interpreter}
                for t in reg.shared_tools(c.tool_sharing)
            ],
        }

    @app.post("/api/help")
    def api_help(payload: dict):
        """Answer 'can anyone help with X?' from our own shared tooling, or
        forward the call onward — each agent is a relay for capability."""
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        try:
            env = Envelope.from_dict(payload)
        except (KeyError, TypeError):
            raise HTTPException(400, "malformed envelope")
        pinned = memory.load_peers().get(env.sender, {}).get("public_key") or None
        if not verify(env, pinned):
            raise HTTPException(403, "bad signature")
        if memory.is_blacklisted(env.sender):
            raise HTTPException(403, "you are blacklisted by this agent")
        note_inbound(env.sender, memory, c)
        need = str(env.body.get("need", ""))
        try:
            ttl = int(env.body.get("ttl", 0))
        except (TypeError, ValueError):
            ttl = 0
        visited = [str(v).lower() for v in env.body.get("visited", [])]
        helper = messaging.resolve_help(c, identity, memory, need, ttl, visited)
        # let the agent see the call it was asked about (as feedback/awareness)
        drop(
            memory.inbox_dir,
            Instruction(
                title=f"Call for help from {env.sender}",
                body=f"{env.sender} asked the federation for help with: {need}\n\n"
                + ("You matched — they may reach out." if helper and helper.get("handle") == c.name
                   else "You passed the call along."),
                source="federation",
                sender=env.sender,
                priority=6,
            ),
        )
        return {"found": helper is not None, "helper": helper}

    @app.post("/api/react")
    def api_react(payload: dict):
        """Receive a like/unlike on one of this node's posts."""
        c = current()
        memory = open_memory(c)
        try:
            env = Envelope.from_dict(payload)
        except (KeyError, TypeError):
            raise HTTPException(400, "malformed envelope")
        pinned = memory.load_peers().get(env.sender, {}).get("public_key") or None
        if not verify(env, pinned):
            raise HTTPException(403, "bad signature")
        if memory.is_blacklisted(env.sender):
            raise HTTPException(403, "blacklisted")
        note_inbound(env.sender, memory, c)
        post = str(env.body.get("post", ""))
        like = bool(env.body.get("like", True))
        count = memory.react(post, env.sender, like)
        return {"ok": True, "likes": count}

    @app.post("/api/comment")
    def api_comment(payload: dict):
        """Receive a comment on one of this node's posts — and let the agent
        read it as feedback it can fold into its pursuits or personality."""
        c = current()
        memory = open_memory(c)
        try:
            env = Envelope.from_dict(payload)
        except (KeyError, TypeError):
            raise HTTPException(400, "malformed envelope")
        pinned = memory.load_peers().get(env.sender, {}).get("public_key") or None
        if not verify(env, pinned):
            raise HTTPException(403, "bad signature")
        if memory.is_blacklisted(env.sender):
            raise HTTPException(403, "blacklisted")
        note_inbound(env.sender, memory, c)
        post = str(env.body.get("post", ""))
        text = str(env.body.get("text", "")).strip()
        if not text:
            raise HTTPException(400, "empty comment")
        memory.add_comment(post, env.sender, text)
        drop(
            memory.inbox_dir,
            Instruction(
                title=f"{env.sender} commented on {post}",
                body=f"On your post {post}, {env.sender} said:\n\n{text}\n\n"
                "This is feedback — you may fold it into a pursuit or your "
                "personality, or reply.",
                source="federation",
                sender=env.sender,
                reply_to="",
                priority=6,
            ),
        )
        return {"ok": True}

    @app.post("/api/ping")
    def api_ping(payload: dict):
        """Receive a progress/completion ping from an agent we're collaborating
        with. It lands in the inbox as a terminal notification (no reply owed),
        so the agent sees on its next wake how shared work is coming along."""
        c = current()
        memory = open_memory(c)
        try:
            env = Envelope.from_dict(payload)
        except (KeyError, TypeError):
            raise HTTPException(400, "malformed envelope")
        pinned = memory.load_peers().get(env.sender, {}).get("public_key") or None
        if not verify(env, pinned):
            raise HTTPException(403, "bad signature")
        if memory.is_blacklisted(env.sender):
            raise HTTPException(403, "blacklisted")
        note_inbound(env.sender, memory, c)
        phase = str(env.body.get("phase", "progress"))
        text = str(env.body.get("text", "")).strip()
        if not text:
            raise HTTPException(400, "empty ping")
        icon = {"start": "▶", "progress": "…", "done": "✓", "blocked": "⚠"}.get(phase, "•")
        drop(
            memory.inbox_dir,
            Instruction(
                title=f"{icon} {phase} · {env.sender}",
                body=f"{env.sender} pinged you ({phase}):\n\n{text}",
                source="federation",
                sender=env.sender,
                reply_to="",  # a ping is a notification, not a question
                priority=5,
            ),
        )
        return {"ok": True, "phase": phase}

    @app.post("/api/locate")
    def api_locate(payload: dict):
        """Answer 'do you know where AGENT is?' — from our own directory, or by
        forwarding the question onward (bounded by the query's TTL)."""
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        try:
            env = Envelope.from_dict(payload)
        except (KeyError, TypeError):
            raise HTTPException(400, "malformed envelope")
        pinned = memory.load_peers().get(env.sender, {}).get("public_key") or None
        if not verify(env, pinned):
            raise HTTPException(403, "bad signature")
        if memory.is_blacklisted(env.sender):
            raise HTTPException(403, "you are blacklisted by this agent")
        note_inbound(env.sender, memory, c)
        target = str(env.body.get("target", ""))
        try:
            ttl = int(env.body.get("ttl", 0))
        except (TypeError, ValueError):
            ttl = 0
        visited = [str(v).lower() for v in env.body.get("visited", [])]
        loc = messaging.resolve_location(c, identity, memory, target, ttl, visited)
        return {"found": loc is not None, "location": loc}

    @app.post("/api/federation/inbox")
    def federation_inbox(payload: dict):
        """Receive a signed envelope from a peer (or via the hub relay)."""
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        try:
            env = Envelope.from_dict(payload)
        except (KeyError, TypeError):
            raise HTTPException(400, "malformed envelope")
        peers = memory.load_peers()
        pinned = peers.get(env.sender, {}).get("public_key") or None
        if not verify(env, pinned):
            raise HTTPException(403, "bad signature")
        if memory.is_blacklisted(env.sender):
            raise HTTPException(403, "you are blacklisted by this agent")
        note_inbound(env.sender, memory, c)

        # Trust on first use: pin an unknown sender's key on first contact of
        # any kind, so later impostors reusing the handle are caught.
        if env.sender not in peers:
            peers[env.sender] = {
                "public_key": env.public_key,
                "public_url": str(env.body.get("public_url", "")),
                "tagline": str(env.body.get("tagline", "")),
                "discovered": "message",
                "last_seen": utcnow(),
            }
            memory.save_peers(peers)

        if env.kind == "hello":
            was_following = peers.get(env.sender, {}).get("following", True)
            peers[env.sender] = {
                "public_url": env.body.get("public_url", ""),
                "public_key": env.public_key,
                "tagline": env.body.get("tagline", ""),
                # a direct hello is an introduction — follow by default; the
                # agent can unfollow later on its own determination
                "following": was_following,
                "last_seen": utcnow(),
            }
            memory.save_peers(peers)
            return {
                "ok": True,
                "handle": identity.handle,
                "public_key": identity.public_key,
                "tagline": c.tagline,
            }

        if env.kind == "note":
            is_reply = bool(env.body.get("reply"))
            drop(
                memory.inbox_dir,
                Instruction(
                    title=(
                        f"Reply from {env.sender}"
                        if is_reply
                        else f"Message from {env.sender}"
                    ),
                    body=str(env.body.get("text", "")),
                    source="federation",
                    sender=env.sender,
                    # a reply is terminal; a fresh message earns one answer back
                    reply_to="" if is_reply else env.sender,
                    respond_with=str(env.body.get("respond_with", "")),
                    priority=6,
                ),
            )
            return {"ok": True, "received_by": identity.handle}

        if env.kind == "instruct":
            if env.sender not in c.trusted_handles:
                raise HTTPException(403, f"{env.sender} is not a trusted handle")
            drop(
                memory.inbox_dir,
                Instruction(
                    title=str(env.body.get("title", f"Instruction from {env.sender}")),
                    body=str(env.body.get("text", "")),
                    source="federation",
                    sender=env.sender,
                    reply_to=env.sender,
                    priority=4,
                ),
            )
            return {"ok": True, "received_by": identity.handle}

        if env.kind.startswith("work_"):
            if not c.economy_enabled:
                return {"ok": True, "ignored": "economy disabled"}
            note = economy_net.handle_work_envelope(c, memory, env)
            if note is not None:
                drop(memory.inbox_dir, note)
            return {"ok": True, "received_by": identity.handle}

        return {"ok": True, "ignored": env.kind}

    @app.post("/api/hook/{hook_name}")
    async def webhook(hook_name: str, request: Request):
        """The webhook connector: outside systems push instructions in."""
        c = current()
        if c.webhook_token:
            if request.headers.get("x-webhook-token", "") != c.webhook_token:
                raise HTTPException(403, "bad webhook token")
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        text = str(payload.get("text", "")).strip()
        if not text:
            raise HTTPException(400, "payload needs a 'text' field")
        drop(
            open_memory(c).inbox_dir,
            Instruction(
                title=str(payload.get("title", f"webhook:{hook_name}"))[:120],
                body=text,
                source=f"connector:webhook:{hook_name}",
                priority=int(payload.get("priority", 5)),
            ),
        )
        return JSONResponse({"ok": True})

    return app
