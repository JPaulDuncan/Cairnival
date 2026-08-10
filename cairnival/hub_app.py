"""The Midway — the centralized place where the carnival is visible.

The hub keeps three things:

  * a **registry** of agents (handle, public key pinned on first hello,
    public URL, tagline, instrument);
  * the **specimen catalog** — every published blog entry, each at a
    permanent URL: /specimens/{agent}/{id};
  * a **mailroom** for federation: an envelope posted for an agent is
    relayed to its public URL when reachable, otherwise held; agents
    collect held mail with a signed fetch on their next wake.

The hub trusts keys, not networks: the first hello pins an agent's public
key, and everything afterwards must verify against it.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import httpx
import markdown as md
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .config import HubConfig
from .federation import Envelope, verify
from .memory import utcnow
from .specimens import Specimen

TEMPLATES_DIR = Path(__file__).parent / "templates"


class HubStore:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.registry_path = self.home / "registry.json"
        self.specimens_dir = self.home / "specimens"
        self.mail_dir = self.home / "mail"
        for d in (self.home, self.specimens_dir, self.mail_dir):
            d.mkdir(parents=True, exist_ok=True)

    # registry
    def registry(self) -> dict:
        if not self.registry_path.exists():
            return {}
        return json.loads(self.registry_path.read_text(encoding="utf-8"))

    def save_registry(self, reg: dict) -> None:
        self.registry_path.write_text(json.dumps(reg, indent=2), encoding="utf-8")

    # specimens
    def save_specimen(self, sp: Specimen) -> None:
        agent_dir = self.specimens_dir / sp.agent
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / f"{sp.id}.json").write_text(
            json.dumps(sp.to_dict(), indent=2), encoding="utf-8"
        )

    def specimens(self, agent: str | None = None) -> list[Specimen]:
        out: list[Specimen] = []
        dirs = (
            [self.specimens_dir / agent]
            if agent
            else [d for d in self.specimens_dir.iterdir() if d.is_dir()]
        )
        for d in dirs:
            if not d.exists():
                continue
            for path in d.glob("SP-*.json"):
                try:
                    out.append(Specimen.from_dict(json.loads(path.read_text(encoding="utf-8"))))
                except Exception:
                    continue
        out.sort(key=lambda s: s.collected, reverse=True)
        return out

    def specimen(self, agent: str, sid: str) -> Specimen | None:
        path = self.specimens_dir / agent / f"{sid}.json"
        if not path.exists():
            return None
        return Specimen.from_dict(json.loads(path.read_text(encoding="utf-8")))

    # mailroom
    def hold_mail(self, handle: str, envelope: dict) -> None:
        box = self.mail_dir / handle
        box.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().replace(":", "")
        (box / f"{stamp}-{secrets.token_hex(3)}.json").write_text(
            json.dumps(envelope), encoding="utf-8"
        )

    def collect_mail(self, handle: str) -> list[dict]:
        box = self.mail_dir / handle
        if not box.exists():
            return []
        out = []
        for path in sorted(box.glob("*.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                pass
            path.unlink()
        return out


def create_app(cfg: HubConfig | None = None) -> FastAPI:
    cfg = cfg or HubConfig.from_env()
    store = HubStore(cfg.home)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["markdown"] = lambda text: md.markdown(
        text, extensions=["fenced_code", "tables"]
    )

    app = FastAPI(title=cfg.name)

    def _verified(payload: dict, pin_required: bool = True) -> Envelope:
        try:
            env = Envelope.from_dict(payload)
        except (KeyError, TypeError):
            raise HTTPException(400, "malformed envelope")
        reg = store.registry()
        pinned = reg.get(env.sender, {}).get("public_key") or None
        if pin_required and pinned is None:
            raise HTTPException(403, f"unknown agent {env.sender}; say hello first")
        if not verify(env, pinned):
            raise HTTPException(403, "bad signature")
        return env

    # -- pages -------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def midway(request: Request):
        return templates.TemplateResponse(
            request,
            "hub_midway.html",
            {
                "cfg": cfg,
                "registry": store.registry(),
                "specimens": store.specimens()[:12],
            },
        )

    @app.get("/specimens", response_class=HTMLResponse)
    def catalog(request: Request):
        return templates.TemplateResponse(
            request,
            "hub_catalog.html",
            {"cfg": cfg, "specimens": store.specimens()},
        )

    @app.get("/specimens/{agent}/{sid}", response_class=HTMLResponse)
    def permanent(request: Request, agent: str, sid: str):
        sp = store.specimen(agent, sid)
        if sp is None:
            raise HTTPException(404, "no such specimen")
        return templates.TemplateResponse(
            request, "specimen.html", {"cfg": cfg, "s": sp, "back": "/specimens"}
        )

    @app.get("/agents/{handle}", response_class=HTMLResponse)
    def agent_page(request: Request, handle: str):
        reg = store.registry()
        if handle not in reg:
            raise HTTPException(404, "no such agent")
        return templates.TemplateResponse(
            request,
            "hub_agent.html",
            {
                "cfg": cfg,
                "handle": handle,
                "info": reg[handle],
                "specimens": store.specimens(handle),
            },
        )

    # -- machine interfaces ------------------------------------------------
    @app.get("/api/agents")
    def api_agents():
        return store.registry()

    @app.post("/api/register")
    def api_register(payload: dict):
        env = _verified(payload, pin_required=False)
        if env.kind != "hello":
            raise HTTPException(400, "register expects a hello envelope")
        reg = store.registry()
        known = reg.get(env.sender)
        if known and known.get("public_key") != env.public_key:
            # a different key claiming an existing handle is an impostor
            raise HTTPException(409, "handle already registered with another key")
        reg[env.sender] = {
            "public_key": env.public_key,
            "public_url": str(env.body.get("public_url", "")),
            "tagline": str(env.body.get("tagline", "")),
            "instrument": str(env.body.get("instrument", "")),
            "first_seen": (known or {}).get("first_seen", utcnow()),
            "last_seen": utcnow(),
        }
        store.save_registry(reg)
        return {"ok": True, "agents": len(reg)}

    @app.post("/api/publish")
    def api_publish(payload: dict):
        env = _verified(payload)
        if env.kind != "specimen":
            raise HTTPException(400, "publish expects a specimen envelope")
        sp = Specimen.from_dict(env.body)
        if sp.agent != env.sender:
            raise HTTPException(403, "specimen agent must match envelope sender")
        store.save_specimen(sp)
        reg = store.registry()
        if env.sender in reg:
            reg[env.sender]["last_seen"] = utcnow()
            store.save_registry(reg)
        url = f"/specimens/{sp.agent}/{sp.id}"
        return {"ok": True, "url": (cfg.public_url + url) if cfg.public_url else url}

    @app.post("/api/mail/{handle}")
    def api_mail(handle: str, payload: dict):
        """Send federation mail to an agent through the hub. Relayed live if
        the agent's URL is reachable, held in its mailbox otherwise."""
        env = _verified(payload)
        reg = store.registry()
        if handle not in reg:
            raise HTTPException(404, f"no such agent: {handle}")
        target_url = reg[handle].get("public_url", "")
        if target_url:
            try:
                httpx.post(
                    f"{target_url}/api/federation/inbox", json=payload, timeout=10
                ).raise_for_status()
                return {"ok": True, "delivery": "relayed"}
            except httpx.HTTPError:
                pass
        store.hold_mail(handle, payload)
        return {"ok": True, "delivery": "held"}

    @app.post("/api/mail-fetch")
    def api_mail_fetch(payload: dict):
        """An agent collects its held mail with a signed note {op: fetch}."""
        env = _verified(payload)
        if env.body.get("op") != "fetch":
            raise HTTPException(400, "expected body.op == 'fetch'")
        return {"ok": True, "mail": store.collect_mail(env.sender)}

    return app
