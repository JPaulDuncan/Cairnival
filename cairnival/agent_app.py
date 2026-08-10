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
from contextlib import asynccontextmanager
from pathlib import Path

import markdown as md
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import AgentConfig
from .federation import Envelope, Identity, verify
from .instructions import Instruction, drop
from .memory import Memory, utcnow
from .settings import GROUPS, SECRET_CLEAR_SENTINEL, apply_form, load_agent_config
from .specimens import load_all
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

    wake_now = threading.Event()
    scheduler_state = {"next_wake": "", "running": False}

    def scheduler() -> None:
        while True:
            delay = next_wake_delay_seconds(current())
            scheduler_state["next_wake"] = f"in ~{delay // 60} min"
            wake_now.wait(timeout=delay)
            wake_now.clear()
            c = current()
            scheduler_state["running"] = True
            try:
                run_wake(c)
            except Exception as exc:  # a bad wake must not kill the agent
                open_memory(c).journal_append(
                    f"\n## failed wake — {utcnow()}\n- error: {exc}"
                )
            finally:
                scheduler_state["running"] = False

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        thread = threading.Thread(target=scheduler, daemon=True, name="wake-scheduler")
        thread.start()
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

    # -- pages -------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        c = current()
        memory = open_memory(c)
        identity = Identity.load_or_create(memory.keys_dir, c.name)
        ledger = Ledger(memory.treasury_dir)
        state = memory.load_state()
        return templates.TemplateResponse(
            request,
            "agent_dashboard.html",
            {
                "cfg": c,
                "identity": identity,
                "state": state,
                "scheduler": scheduler_state,
                "specimens": load_all(memory.specimens_dir)[:8],
                "inbox_count": len(sorted(memory.inbox_dir.glob("*.md"))),
                "treasury": ledger.summary(),
                "proposals": ledger.proposals("pending"),
                "peers": memory.load_peers(),
                "journal_tail": memory.journal_tail(3000),
            },
        )

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

    # -- settings ----------------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        check_token(request)
        c = current()
        return templates.TemplateResponse(
            request,
            "agent_settings.html",
            {
                "cfg": c,
                "groups": GROUPS,
                "clear_sentinel": SECRET_CLEAR_SENTINEL,
                "soul": open_memory(c).soul(),
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
            "waking_now": scheduler_state["running"],
            "treasury": Ledger(memory.treasury_dir).summary(),
            "specimens": len(list(memory.specimens_dir.glob("SP-*.md"))),
        }

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

        if env.kind == "hello":
            peers[env.sender] = {
                "public_url": env.body.get("public_url", ""),
                "public_key": env.public_key,
                "tagline": env.body.get("tagline", ""),
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
            drop(
                memory.inbox_dir,
                Instruction(
                    title=f"Mail from {env.sender}",
                    body=str(env.body.get("text", ""))
                    + "\n\n(Reply is optional; fold anything worth keeping into the specimen.)",
                    source="federation",
                    sender=env.sender,
                    priority=6,
                ),
            )
            return {"ok": True}

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
                    priority=4,
                ),
            )
            return {"ok": True}

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
