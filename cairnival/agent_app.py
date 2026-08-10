"""The agent's web face.

The agent mainly runs headless: a scheduler thread wakes it on cadence. This
FastAPI app is the *attachment point* — a human (or another agent) can attach
to a running agent to watch it, instruct it, feed its treasury, approve its
spending, or deliver federation mail. Close the browser and the agent goes on
without you.
"""

from __future__ import annotations

import asyncio
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
from .specimens import load_all
from .treasury import Ledger
from .wake import next_wake_delay_seconds, run_wake

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(cfg: AgentConfig | None = None) -> FastAPI:
    cfg = cfg or AgentConfig.from_env()
    memory = Memory(cfg.home, cfg.name)
    memory.ensure()
    identity = Identity.load_or_create(memory.keys_dir, cfg.name)
    ledger = Ledger(memory.treasury_dir)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["markdown"] = lambda text: md.markdown(
        text, extensions=["fenced_code", "tables"]
    )

    wake_now = threading.Event()
    scheduler_state = {"next_wake": "", "running": False}

    def scheduler() -> None:
        while True:
            delay = next_wake_delay_seconds(cfg)
            scheduler_state["next_wake"] = f"in ~{delay // 60} min"
            wake_now.wait(timeout=delay)
            wake_now.clear()
            scheduler_state["running"] = True
            try:
                run_wake(cfg)
            except Exception as exc:  # a bad wake must not kill the agent
                memory.journal_append(f"\n## failed wake — {utcnow()}\n- error: {exc}")
            finally:
                scheduler_state["running"] = False

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        thread = threading.Thread(target=scheduler, daemon=True, name="wake-scheduler")
        thread.start()
        yield

    app = FastAPI(title=f"Cairnival agent: {cfg.name}", lifespan=lifespan)

    # -- auth --------------------------------------------------------------
    def check_token(request: Request) -> None:
        """Mutating human routes are token-gated when UI_TOKEN is set."""
        if not cfg.ui_token:
            return
        supplied = (
            request.query_params.get("token")
            or request.cookies.get("cairnival_token")
            or request.headers.get("x-cairnival-token", "")
        )
        if supplied != cfg.ui_token:
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
        state = memory.load_state()
        specimens = load_all(memory.specimens_dir)[:8]
        pending_files = sorted(memory.inbox_dir.glob("*.md"))
        return templates.TemplateResponse(
            request,
            "agent_dashboard.html",
            {
                "cfg": cfg,
                "identity": identity,
                "state": state,
                "scheduler": scheduler_state,
                "specimens": specimens,
                "inbox_count": len(pending_files),
                "treasury": ledger.summary(),
                "proposals": ledger.proposals("pending"),
                "peers": memory.load_peers(),
                "journal_tail": memory.journal_tail(3000),
            },
        )

    @app.get("/specimens", response_class=HTMLResponse)
    def specimens_page(request: Request):
        return templates.TemplateResponse(
            request,
            "agent_specimens.html",
            {"cfg": cfg, "specimens": load_all(memory.specimens_dir)},
        )

    @app.get("/specimens/{sid}", response_class=HTMLResponse)
    def specimen_page(request: Request, sid: str):
        for sp in load_all(memory.specimens_dir):
            if sp.id == sid:
                return templates.TemplateResponse(
                    request, "specimen.html", {"cfg": cfg, "s": sp, "back": "/specimens"}
                )
        raise HTTPException(404, "no such specimen")

    # -- human controls ----------------------------------------------------
    @app.post("/instruct")
    def instruct(
        request: Request,
        title: str = Form(""),
        body: str = Form(...),
        priority: int = Form(5),
    ):
        check_token(request)
        ins = Instruction(
            title=title.strip() or body.strip().splitlines()[0][:80],
            body=body,
            source="ui",
            priority=max(1, min(9, priority)),
        )
        drop(memory.inbox_dir, ins)
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
        ledger.deposit(amount, sender, memo)
        return _redirect(request, "/")

    @app.post("/treasury/propose")
    def treasury_propose(
        request: Request,
        to: str = Form(...),
        amount: float = Form(...),
        reason: str = Form(""),
    ):
        check_token(request)
        ledger.propose(to, amount, reason)
        return _redirect(request, "/")

    @app.post("/treasury/resolve/{proposal_id}")
    def treasury_resolve(request: Request, proposal_id: str, decision: str = Form(...)):
        check_token(request)
        try:
            ledger.resolve(proposal_id, decision == "approve")
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        return _redirect(request, "/")

    # -- machine interfaces ------------------------------------------------
    @app.get("/api/status")
    def api_status():
        state = memory.load_state()
        return {
            "handle": identity.handle,
            "public_key": identity.public_key,
            "tagline": cfg.tagline,
            "wakes": state.get("wakes", 0),
            "last_wake": state.get("last_wake", ""),
            "next_wake": scheduler_state["next_wake"],
            "waking_now": scheduler_state["running"],
            "treasury": ledger.summary(),
            "specimens": len(list(memory.specimens_dir.glob("SP-*.md"))),
        }

    @app.post("/api/federation/inbox")
    def federation_inbox(payload: dict):
        """Receive a signed envelope from a peer (or via the hub relay)."""
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
                "tagline": cfg.tagline,
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
            if env.sender not in cfg.trusted_handles:
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
        if cfg.webhook_token:
            if request.headers.get("x-webhook-token", "") != cfg.webhook_token:
                raise HTTPException(403, "bad webhook token")
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        text = str(payload.get("text", "")).strip()
        if not text:
            raise HTTPException(400, "payload needs a 'text' field")
        drop(
            memory.inbox_dir,
            Instruction(
                title=str(payload.get("title", f"webhook:{hook_name}"))[:120],
                body=text,
                source=f"connector:webhook:{hook_name}",
                priority=int(payload.get("priority", 5)),
            ),
        )
        return JSONResponse({"ok": True})

    return app
